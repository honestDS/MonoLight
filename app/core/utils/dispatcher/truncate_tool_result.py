import json
import os
from dataclasses import dataclass

import tiktoken

from app.core.constants import CONTEXT_WINDOW_TOKENS_PER_K
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.context_budget import build_context_request_budget, measure_context_request_usage
from app.models.message import InternalMessage

logger = get_logger(__name__)


def _get_truncation_notice() -> str:
    return t("MSG_TOOL_RESULT_TRUNCATED")


_COMPACT_TRUNCATION_NOTICE = "truncated"
_MINIMAL_TRUNCATION_NOTICE = "cut"


def _fit_truncation_notice_to_token_budget(encoding, limit_tokens: int) -> tuple[str, int]:
    for notice in (
        _get_truncation_notice(),
        _COMPACT_TRUNCATION_NOTICE,
        _MINIMAL_TRUNCATION_NOTICE,
    ):
        notice_token_ids = encoding.encode(notice, disallowed_special=())
        if len(notice_token_ids) <= limit_tokens:
            return notice, len(notice_token_ids)

    minimal_token_ids = encoding.encode(_MINIMAL_TRUNCATION_NOTICE, disallowed_special=())
    fitted_token_ids = minimal_token_ids[:limit_tokens]
    return encoding.decode(fitted_token_ids), len(fitted_token_ids)


def _fit_truncation_notice_to_estimated_budget(limit_tokens: int) -> tuple[str, int]:
    for notice in (
        _get_truncation_notice(),
        _COMPACT_TRUNCATION_NOTICE,
        _MINIMAL_TRUNCATION_NOTICE,
    ):
        notice_tokens = max(1, _estimate_tokens_by_chars(notice))
        if notice_tokens <= limit_tokens:
            return notice, notice_tokens
    return _MINIMAL_TRUNCATION_NOTICE, 1


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
) -> int:
    if isinstance(required_input_tokens_override, int) and not isinstance(required_input_tokens_override, bool) and required_input_tokens_override >= 0:
        budget = build_context_request_budget(
            context_window_k=context_window_k,
            max_tokens=max_tokens,
        )
        required_input_tokens = required_input_tokens_override
    else:
        usage = measure_context_request_usage(
            messages=messages,
            context_window_k=context_window_k,
            max_tokens=max_tokens,
            tools=tools,
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
    include_notice: bool = True,
) -> ToolResultTruncation:
    """对单条工具响应做 token 级截断，并返回截断统计信息。

    默认按上下文窗口一半截断；传入 limit_tokens 时按显式预算截断，截断提示本身也必须计入该预算。
    """
    if not content:
        return ToolResultTruncation(content=content, truncated=False, original_tokens=0, final_tokens=0, removed_chars=0)

    limit_tokens = max(1, limit_tokens if limit_tokens is not None else (context_window_k * CONTEXT_WINDOW_TOKENS_PER_K) // 2)

    try:
        encoding = tiktoken.get_encoding("cl100k_base")
        token_ids = encoding.encode(content, disallowed_special=())
        original_tokens = len(token_ids)
        if original_tokens <= limit_tokens:
            return ToolResultTruncation(content=content, truncated=False, original_tokens=original_tokens, final_tokens=original_tokens, removed_chars=0)

        truncation_notice, notice_tokens = _fit_truncation_notice_to_token_budget(
            encoding,
            limit_tokens,
        )
        if not include_notice:
            truncated_body = encoding.decode(token_ids[:limit_tokens])
            truncated_content = truncated_body
        elif notice_tokens < limit_tokens:
            body_limit_tokens = limit_tokens - notice_tokens
            truncated_body = encoding.decode(token_ids[:body_limit_tokens])
            truncated_content = truncated_body + truncation_notice
        else:
            truncated_body = ""
            truncated_content = truncation_notice
        final_tokens = len(encoding.encode(truncated_content, disallowed_special=()))
        return ToolResultTruncation(
            content=truncated_content,
            truncated=True,
            original_tokens=original_tokens,
            final_tokens=final_tokens,
            removed_chars=max(len(content) - len(truncated_body), 0),
        )
    except Exception:
        c_coeff = float(os.getenv("TOKEN_COEFF_CHINESE", 1.5))
        o_coeff = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
        avg_coeff = max((c_coeff + o_coeff) / 2, 0.1)
        truncation_notice, notice_tokens = _fit_truncation_notice_to_estimated_budget(limit_tokens)
        original_tokens = _estimate_tokens_by_chars(content)
        if not include_notice:
            char_limit = max(1, int(limit_tokens / avg_coeff))
            if len(content) <= char_limit:
                return ToolResultTruncation(content=content, truncated=False, original_tokens=original_tokens, final_tokens=original_tokens, removed_chars=0)
            truncated_body = content[:char_limit]
            truncated_content = truncated_body
        elif notice_tokens < limit_tokens:
            char_limit = max(1, int((limit_tokens - notice_tokens) / avg_coeff))
            if len(content) <= char_limit:
                return ToolResultTruncation(content=content, truncated=False, original_tokens=original_tokens, final_tokens=original_tokens, removed_chars=0)
            truncated_body = content[:char_limit]
            truncated_content = truncated_body + truncation_notice
        else:
            truncated_body = ""
            truncated_content = truncation_notice
        return ToolResultTruncation(
            content=truncated_content,
            truncated=True,
            original_tokens=original_tokens,
            final_tokens=min(
                limit_tokens,
                max(1, _estimate_tokens_by_chars(truncated_content)),
            ),
            removed_chars=max(len(content) - len(truncated_body), 0),
        )


def truncate_longterm_memory_recall_result_for_budget(
    result: str,
    *,
    context_window_k: int,
    budget_tokens: int,
) -> tuple[str, ToolMessagesTruncationStats]:
    overall = truncate_tool_result_with_stats(
        result,
        context_window_k,
        limit_tokens=budget_tokens,
    )
    if not overall.truncated:
        return result, ToolMessagesTruncationStats(truncated_count=0, removed_chars=0)

    try:
        payload = json.loads(result)
    except (TypeError, ValueError):
        return overall.content, ToolMessagesTruncationStats(
            truncated_count=1,
            removed_chars=overall.removed_chars,
        )
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return overall.content, ToolMessagesTruncationStats(
            truncated_count=1,
            removed_chars=overall.removed_chars,
        )

    safe_payload = dict(payload)
    safe_payload["items"] = [dict(item) for item in payload.get("items", []) if isinstance(item, dict)]
    for section in ("knowledge_base", "chat_history"):
        value = payload.get(section)
        if isinstance(value, list):
            safe_payload[section] = [dict(item) for item in value if isinstance(item, dict)]
    safe_payload["truncated"] = True

    original_contents: dict[tuple[str, int], str] = {}
    for section in ("items", "knowledge_base", "chat_history"):
        section_items = safe_payload.get(section)
        if not isinstance(section_items, list):
            continue
        for index, item in enumerate(section_items):
            content = item.get("content")
            if isinstance(content, str) and content:
                original_contents[(section, index)] = content
                item["content"] = ""

    def compact() -> str:
        return json.dumps(
            safe_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        )

    def fits(value: str) -> bool:
        return not truncate_tool_result_with_stats(
            value,
            context_window_k,
            limit_tokens=budget_tokens,
        ).truncated

    def remove_last(section: str) -> bool:
        section_items = safe_payload.get(section)
        if not isinstance(section_items, list) or not section_items:
            return False
        section_items.pop()
        count_field = f"{section}_omitted_count"
        safe_payload[count_field] = int(safe_payload.get(count_field, 0)) + 1
        return True

    base = compact()
    while not fits(base):
        if remove_last("chat_history"):
            pass
        elif remove_last("knowledge_base"):
            pass
        else:
            break
        base = compact()

    base_stats = truncate_tool_result_with_stats(
        base,
        context_window_k,
        limit_tokens=budget_tokens,
    )
    surviving_contents = [(section, index, content) for (section, index), content in original_contents.items() if isinstance(safe_payload.get(section), list) and index < len(safe_payload[section])]
    remaining_tokens = max(budget_tokens - base_stats.original_tokens - 4, 0)
    per_content_budget = max(1, remaining_tokens // len(surviving_contents)) if surviving_contents and remaining_tokens else 0

    if per_content_budget:
        while True:
            for section, index, content in surviving_contents:
                item = safe_payload[section][index]
                content_stats = truncate_tool_result_with_stats(
                    content,
                    context_window_k,
                    limit_tokens=per_content_budget,
                    include_notice=False,
                )
                item["content"] = content_stats.content
                if content_stats.truncated:
                    item["truncated"] = True
                    if section == "knowledge_base" and item.get("source_type") == "managed_knowledge":
                        item["llm_maintainable"] = False
                        item.pop("knowledge_id", None)
                        item.pop("knowledge_expected_version", None)
            candidate = compact()
            if fits(candidate):
                return candidate, ToolMessagesTruncationStats(
                    truncated_count=1,
                    removed_chars=max(len(result) - len(candidate), 0),
                )
            if per_content_budget == 1:
                break
            per_content_budget = max(1, per_content_budget // 2)

    if surviving_contents and not per_content_budget:
        for section, index, _content in surviving_contents:
            item = safe_payload[section][index]
            item["truncated"] = True
            if section == "knowledge_base" and item.get("source_type") == "managed_knowledge":
                item["llm_maintainable"] = False
                item.pop("knowledge_id", None)
                item.pop("knowledge_expected_version", None)

    final_value = compact()
    if not fits(final_value):
        return overall.content, ToolMessagesTruncationStats(
            truncated_count=1,
            removed_chars=overall.removed_chars,
        )
    return final_value, ToolMessagesTruncationStats(
        truncated_count=1,
        removed_chars=max(len(result) - len(final_value), 0),
    )


def truncate_tool_messages_for_budget(
    tool_msgs: list[InternalMessage],
    context_window_k: int,
    budget_tokens: int,
    uid: str,
    session_id: str,
    structured_recall_tool_call_ids: set[str] | None = None,
) -> ToolMessagesTruncationStats:
    if not tool_msgs:
        return ToolMessagesTruncationStats(truncated_count=0, removed_chars=0)

    per_tool_budget = max(1, budget_tokens // len(tool_msgs))
    truncated_count = 0
    removed_chars = 0
    for msg in tool_msgs:
        if structured_recall_tool_call_ids and msg.tool_call_id in structured_recall_tool_call_ids:
            msg.content, stats = truncate_longterm_memory_recall_result_for_budget(
                msg.content or "",
                context_window_k=context_window_k,
                budget_tokens=per_tool_budget,
            )
            truncated_count += stats.truncated_count
            removed_chars += stats.removed_chars
            continue
        truncation = truncate_tool_result_with_stats(
            msg.content or "",
            context_window_k,
            limit_tokens=per_tool_budget,
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


def truncate_tool_result(content: str, context_window_k: int) -> tuple[str, bool]:
    """对单条工具响应做 token 级截断。

    返回 (处理后的内容, 是否发生截断)。
    """
    result = truncate_tool_result_with_stats(content, context_window_k)
    return result.content, result.truncated
