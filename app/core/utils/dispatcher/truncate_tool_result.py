import json
import os
from dataclasses import dataclass

import tiktoken

from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    TOOL_RESULT_COMPACT_TRUNCATION_NOTICE,
    TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE,
)
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.context_budget import build_context_request_budget, measure_context_request_usage
from app.core.utils.tokenizer import resolve_token_encoding_name
from app.models.message import InternalMessage

logger = get_logger(__name__)


def _get_truncation_notice() -> str:
    return t("MSG_TOOL_RESULT_TRUNCATED")


def _get_truncation_notices() -> tuple[str, ...]:
    return (
        _get_truncation_notice(),
        TOOL_RESULT_COMPACT_TRUNCATION_NOTICE,
        TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE,
    )


def _fit_truncation_notice_to_token_budget(encoding, limit_tokens: int) -> tuple[str, int]:
    for notice in _get_truncation_notices():
        notice_token_ids = encoding.encode(notice, disallowed_special=())
        if len(notice_token_ids) <= limit_tokens:
            return notice, len(notice_token_ids)

    minimal_token_ids = encoding.encode(TOOL_RESULT_MINIMAL_TRUNCATION_NOTICE, disallowed_special=())
    fitted_token_ids = minimal_token_ids[:limit_tokens]
    return encoding.decode(fitted_token_ids), len(fitted_token_ids)


def _serialize_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _json_structure_placeholder(value: object) -> str:
    if isinstance(value, str):
        return _serialize_json("")
    if isinstance(value, list):
        return "[]"
    if isinstance(value, dict):
        return "{}"
    return _serialize_json(value)


def _serialize_json_object(parts: list[tuple[str, str]]) -> str:
    return "{" + ",".join(f"{key}:{item}" for key, item in parts) + "}"


def _fit_json_child(
    value: object,
    prefix: str,
    suffix: str,
    limit_tokens: int,
    count_tokens,
    notices: tuple[str, ...],
) -> str | None:
    serialized = _serialize_json(value)
    if count_tokens(prefix + serialized + suffix) <= limit_tokens:
        return serialized

    low = 1
    high = limit_tokens
    best: str | None = None
    while low <= high:
        child_limit = (low + high) // 2
        child = _truncate_json_value(value, child_limit, count_tokens, notices)
        if count_tokens(prefix + child + suffix) <= limit_tokens:
            best = child
            low = child_limit + 1
        else:
            high = child_limit - 1
    return best


def _truncate_json_string(
    value: str,
    limit_tokens: int,
    count_tokens,
    notices: tuple[str, ...],
) -> str:
    for notice in notices:
        best = _serialize_json(notice)
        if count_tokens(best) > limit_tokens:
            continue

        low = 0
        high = len(value)
        while low <= high:
            length = (low + high) // 2
            current = _serialize_json(value[:length] + notice)
            if count_tokens(current) <= limit_tokens:
                best = current
                low = length + 1
            else:
                high = length - 1
        return best
    return _serialize_json("")


def _truncate_json_value(value: object, limit_tokens: int, count_tokens, notices: tuple[str, ...]) -> str:
    serialized = _serialize_json(value)
    if count_tokens(serialized) <= limit_tokens:
        return serialized

    if isinstance(value, str):
        return _truncate_json_string(value, limit_tokens, count_tokens, notices)

    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            prefix = "[" + ",".join(parts) + ("," if parts else "")
            full_item = _serialize_json(item)
            if count_tokens(prefix + full_item + "]") <= limit_tokens:
                parts.append(full_item)
                continue
            child = _fit_json_child(item, prefix, "]", limit_tokens, count_tokens, notices)
            if child is None:
                break
            parts.append(child)
            break
        return "[" + ",".join(parts) + "]"

    if isinstance(value, dict):
        entries = [
            (
                _serialize_json(key),
                item,
                _json_structure_placeholder(item),
                _serialize_json(item),
            )
            for key, item in value.items()
        ]
        parts = [(key, placeholder) for key, _item, placeholder, _full_item in entries]
        if count_tokens(_serialize_json_object(parts)) > limit_tokens:
            return "{}"

        expandable = []
        for index, (_key, _item, placeholder, full_item) in enumerate(entries):
            if full_item == placeholder:
                continue
            extra_tokens = max(count_tokens(full_item) - count_tokens(placeholder), 0)
            expandable.append((extra_tokens, index, full_item))

        for _extra_tokens, index, full_item in sorted(expandable):
            candidate_parts = list(parts)
            candidate_parts[index] = (candidate_parts[index][0], full_item)
            if count_tokens(_serialize_json_object(candidate_parts)) <= limit_tokens:
                parts = candidate_parts

        for index, (key, item, _placeholder, full_item) in enumerate(entries):
            if parts[index][1] == full_item:
                continue
            prefix_parts = parts[:index]
            suffix_parts = parts[index + 1 :]
            prefix = "{" + ",".join(f"{part_key}:{part_value}" for part_key, part_value in prefix_parts)
            if prefix_parts:
                prefix += ","
            prefix += key + ":"
            suffix = ""
            if suffix_parts:
                suffix = "," + ",".join(f"{part_key}:{part_value}" for part_key, part_value in suffix_parts)
            suffix += "}"
            child = _fit_json_child(item, prefix, suffix, limit_tokens, count_tokens, notices)
            if child is not None:
                parts[index] = (key, child)

        return _serialize_json_object(parts)

    return "null"


def _minimal_json_values(payload: object) -> tuple[str, ...]:
    if isinstance(payload, dict):
        return ("{}", "null", "[]", '""', "0", "false")
    if isinstance(payload, list):
        return ("[]", "null", "{}", '""', "0", "false")
    if isinstance(payload, str):
        return ('""', "null", "[]", "{}", "0", "false")
    return ("null", '""', "[]", "{}", "0", "false")


def _minimal_json_content(payload: object, limit_tokens: int, count_tokens) -> str:
    for minimal in _minimal_json_values(payload):
        try:
            if count_tokens(minimal) <= limit_tokens:
                return minimal
        except Exception:
            continue
    return "null"


def _truncate_json_content(
    payload: object,
    limit_tokens: int,
    count_tokens,
    notices: tuple[str, ...],
) -> str:
    compact = _serialize_json(payload)
    if count_tokens(compact) <= limit_tokens:
        return compact

    candidate = _truncate_json_value(payload, limit_tokens, count_tokens, notices)
    if count_tokens(candidate) <= limit_tokens:
        return candidate
    return _minimal_json_content(payload, limit_tokens, count_tokens)


def _truncate_json_result(payload: object, limit_tokens: int, count_tokens) -> tuple[str, int]:
    candidate: str | None = None
    try:
        notices = _get_truncation_notices()
        candidate = _truncate_json_content(payload, limit_tokens, count_tokens, notices)
    except Exception:
        pass

    candidates = ([candidate] if candidate is not None else []) + list(_minimal_json_values(payload))
    for candidate in candidates:
        try:
            final_tokens = count_tokens(candidate)
        except Exception:
            continue
        if final_tokens <= limit_tokens:
            return candidate, final_tokens
    return "null", count_tokens("null")


def _truncate_text_with_encoding(
    token_ids: list[int],
    encoding,
    limit_tokens: int,
) -> tuple[str, str]:
    truncation_notice, notice_tokens = _fit_truncation_notice_to_token_budget(
        encoding,
        limit_tokens,
    )
    if notice_tokens >= limit_tokens:
        return truncation_notice, ""

    def candidate(length: int) -> tuple[str, str]:
        body = encoding.decode(token_ids[:length])
        return body + truncation_notice, body

    low = 0
    high = len(token_ids)
    best_content, best_body = candidate(0)
    while low <= high:
        length = (low + high) // 2
        current_content, current_body = candidate(length)
        final_tokens = len(encoding.encode(current_content, disallowed_special=()))
        if final_tokens <= limit_tokens:
            best_content, best_body = current_content, current_body
            low = length + 1
        else:
            high = length - 1
    return best_content, best_body


def _truncate_text_with_estimate(
    content: str,
    limit_tokens: int,
) -> tuple[str, str]:
    truncation_notice = ""
    notice_tokens = limit_tokens
    for notice in _get_truncation_notices():
        estimated_tokens = _estimate_tokens_by_chars(notice)
        if max(1, estimated_tokens) <= limit_tokens:
            truncation_notice = notice
            notice_tokens = max(1, estimated_tokens)
            break
    if notice_tokens >= limit_tokens:
        return truncation_notice, ""

    def candidate(length: int) -> tuple[str, str]:
        body = content[:length]
        return body + truncation_notice, body

    low = 0
    high = len(content)
    best_content, best_body = candidate(0)
    while low <= high:
        length = (low + high) // 2
        current_content, current_body = candidate(length)
        if _estimate_tokens_by_chars(current_content) <= limit_tokens:
            best_content, best_body = current_content, current_body
            low = length + 1
        else:
            high = length - 1
    return best_content, best_body


@dataclass(frozen=True)
class ToolResultTruncation:
    content: str
    truncated: bool
    original_tokens: int
    final_tokens: int
    removed_chars: int


@dataclass(frozen=True)
class ToolMessagesTruncationStats:
    truncated_count: int
    removed_chars: int


def calculate_tool_result_round_budget_tokens(
    *,
    messages: list[InternalMessage],
    context_window_k: int,
    max_tokens: int,
    tools: list[dict] | None,
    required_input_tokens_override: int | None = None,
    fallback_to_local_usage_on_overflow: bool = False,
    model_id: str | None = None,
    protocol: str | None = None,
) -> int:
    if isinstance(required_input_tokens_override, int) and not isinstance(required_input_tokens_override, bool) and required_input_tokens_override >= 0:
        budget = build_context_request_budget(
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            model_id=model_id,
            protocol=protocol,
        )
        required_input_tokens = required_input_tokens_override
        hard_input_limit = budget.context_window_tokens - budget.output_tokens - budget.safety_margin_tokens
        if required_input_tokens >= hard_input_limit and fallback_to_local_usage_on_overflow:
            usage = measure_context_request_usage(
                messages=messages,
                context_window_k=context_window_k,
                max_tokens=max_tokens,
                tools=tools,
                model_id=model_id,
                protocol=protocol,
            )
            budget = usage.budget
            required_input_tokens = usage.required_input_tokens
    else:
        usage = measure_context_request_usage(
            messages=messages,
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            tools=tools,
            model_id=model_id,
            protocol=protocol,
        )
        budget = usage.budget
        required_input_tokens = usage.required_input_tokens

    hard_input_limit = budget.context_window_tokens - budget.output_tokens - budget.safety_margin_tokens
    remaining_input_tokens = max(hard_input_limit - required_input_tokens, 0)
    # 工具结果最多使用当前剩余上下文的一半，给模型下一轮继续分析、缩小查询范围或再次调用工具预留空间。
    return max(1, remaining_input_tokens // 2)


def _estimate_tokens_by_chars(text: str) -> int:
    c_coeff = float(os.getenv("TOKEN_COEFF_CHINESE", 1.5))
    o_coeff = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
    chinese_count = sum(1 for character in text if "\u4e00" <= character <= "\u9fff")
    other_count = len(text) - chinese_count
    return int(chinese_count * c_coeff + other_count * o_coeff)


def truncate_tool_result_with_stats(
    content: str,
    context_window_k: int,
    limit_tokens: int | None = None,
    *,
    model_id: str | None = None,
    protocol: str | None = None,
) -> ToolResultTruncation:
    """对单条工具响应做 token 级截断，并返回截断统计信息。

    默认按上下文窗口一半截断；传入 limit_tokens 时按显式预算截断，截断提示本身也必须计入该预算。
    """
    if not content:
        return ToolResultTruncation(content=content, truncated=False, original_tokens=0, final_tokens=0, removed_chars=0)

    limit_tokens = max(1, limit_tokens if limit_tokens is not None else (context_window_k * CONTEXT_WINDOW_TOKENS_PER_K) // 2)

    try:
        encoding = tiktoken.get_encoding(
            resolve_token_encoding_name(
                model_id,
                protocol=protocol,
            )
        )
        token_ids = encoding.encode(content, disallowed_special=())
        original_tokens = len(token_ids)
        if original_tokens <= limit_tokens:
            return ToolResultTruncation(content=content, truncated=False, original_tokens=original_tokens, final_tokens=original_tokens, removed_chars=0)

        try:
            payload = json.loads(content)
        except (TypeError, ValueError, RecursionError):
            pass
        else:
            try:
                truncated_content, final_tokens = _truncate_json_result(
                    payload,
                    limit_tokens,
                    lambda value: len(encoding.encode(value, disallowed_special=())),
                )
            except Exception:
                truncated_content = "null"
                final_tokens = len(encoding.encode(truncated_content, disallowed_special=()))
            return ToolResultTruncation(
                content=truncated_content,
                truncated=True,
                original_tokens=original_tokens,
                final_tokens=final_tokens,
                removed_chars=max(len(content) - len(truncated_content), 0),
            )

        truncated_content, truncated_body = _truncate_text_with_encoding(
            token_ids,
            encoding,
            limit_tokens,
        )
        final_tokens = len(encoding.encode(truncated_content, disallowed_special=()))
        return ToolResultTruncation(
            content=truncated_content,
            truncated=True,
            original_tokens=original_tokens,
            final_tokens=final_tokens,
            removed_chars=max(len(content) - len(truncated_body), 0),
        )
    except Exception:
        original_tokens = _estimate_tokens_by_chars(content)
        if original_tokens <= limit_tokens:
            return ToolResultTruncation(content=content, truncated=False, original_tokens=original_tokens, final_tokens=original_tokens, removed_chars=0)

        try:
            payload = json.loads(content)
        except (TypeError, ValueError, RecursionError):
            pass
        else:
            try:
                truncated_content, final_tokens = _truncate_json_result(
                    payload,
                    limit_tokens,
                    _estimate_tokens_by_chars,
                )
            except Exception:
                truncated_content = "null"
                final_tokens = _estimate_tokens_by_chars(truncated_content)
            return ToolResultTruncation(
                content=truncated_content,
                truncated=True,
                original_tokens=original_tokens,
                final_tokens=final_tokens,
                removed_chars=max(len(content) - len(truncated_content), 0),
            )

        truncated_content, truncated_body = _truncate_text_with_estimate(
            content,
            limit_tokens,
        )
        return ToolResultTruncation(
            content=truncated_content,
            truncated=True,
            original_tokens=original_tokens,
            final_tokens=_estimate_tokens_by_chars(truncated_content),
            removed_chars=max(len(content) - len(truncated_body), 0),
        )


def truncate_tool_messages_for_budget(
    tool_msgs: list[InternalMessage],
    context_window_k: int,
    budget_tokens: int,
    uid: str,
    session_id: str,
    model_id: str | None = None,
    protocol: str | None = None,
) -> ToolMessagesTruncationStats:
    if not tool_msgs:
        return ToolMessagesTruncationStats(truncated_count=0, removed_chars=0)

    per_tool_budget = max(1, budget_tokens // len(tool_msgs))
    truncated_count = 0
    removed_chars = 0
    for msg in tool_msgs:
        truncation = truncate_tool_result_with_stats(
            msg.content or "",
            context_window_k,
            limit_tokens=per_tool_budget,
            model_id=model_id,
            protocol=protocol,
        )
        msg.content = truncation.content
        if truncation.truncated:
            truncated_count += 1
            removed_chars += truncation.removed_chars

    if truncated_count:
        logger.bind(uid=uid, session_id=session_id).info(
            t(
                "LOG_TOOL_RESULTS_TRUNCATED_FOR_BUDGET",
                count=truncated_count,
                removed_chars=removed_chars,
                context_window_k=context_window_k,
            )
        )

    return ToolMessagesTruncationStats(truncated_count=truncated_count, removed_chars=removed_chars)


def truncate_tool_result(
    content: str,
    context_window_k: int,
    *,
    model_id: str | None = None,
    protocol: str | None = None,
) -> tuple[str, bool]:
    """对单条工具响应做 token 级截断。

    返回 (处理后的内容, 是否发生截断)。
    """
    result = truncate_tool_result_with_stats(
        content,
        context_window_k,
        model_id=model_id,
        protocol=protocol,
    )
    return result.content, result.truncated
