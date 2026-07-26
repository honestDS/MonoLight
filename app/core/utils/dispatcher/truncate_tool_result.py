import os
from dataclasses import dataclass

import tiktoken

from app.core.constants import CONTEXT_WINDOW_TOKENS_PER_K
from app.core.i18n import t
from app.core.log import get_logger
from app.models.message import InternalMessage

logger = get_logger(__name__)


def _get_truncation_notice() -> str:
    return t("MSG_TOOL_RESULT_TRUNCATED")


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


def _estimate_tokens_by_chars(text: str) -> int:
    c_coeff = float(os.getenv("TOKEN_COEFF_CHINESE", 1.5))
    o_coeff = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
    chinese_count = sum(1 for character in text if "\u4e00" <= character <= "\u9fff")
    other_count = len(text) - chinese_count
    return int(chinese_count * c_coeff + other_count * o_coeff)


def truncate_tool_result_with_stats(content: str, context_window_k: int, limit_tokens: int | None = None) -> ToolResultTruncation:
    """对单条工具响应做 token 级截断，并返回截断统计信息。

    默认按上下文窗口一半截断；传入 limit_tokens 时按显式预算截断。
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

        truncation_notice = _get_truncation_notice()
        notice_tokens = len(encoding.encode(truncation_notice, disallowed_special=()))
        if notice_tokens < limit_tokens:
            body_limit_tokens = max(1, limit_tokens - notice_tokens)
            truncated_body = encoding.decode(token_ids[:body_limit_tokens])
            truncated_content = truncated_body + truncation_notice
        else:
            truncated_body = encoding.decode(token_ids[:limit_tokens])
            truncated_content = truncated_body
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
        truncation_notice = _get_truncation_notice()
        notice_tokens = _estimate_tokens_by_chars(truncation_notice)
        original_tokens = _estimate_tokens_by_chars(content)
        if notice_tokens < limit_tokens:
            char_limit = max(1, int((limit_tokens - notice_tokens) / avg_coeff))
            append_notice = True
        else:
            char_limit = max(1, int(limit_tokens / avg_coeff))
            append_notice = False

        if len(content) <= char_limit:
            return ToolResultTruncation(content=content, truncated=False, original_tokens=original_tokens, final_tokens=original_tokens, removed_chars=0)

        truncated_body = content[:char_limit]
        truncated_content = truncated_body + truncation_notice if append_notice else truncated_body
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
) -> ToolMessagesTruncationStats:
    if not tool_msgs:
        return ToolMessagesTruncationStats(truncated_count=0, removed_chars=0)

    per_tool_budget = max(1, budget_tokens // len(tool_msgs))
    truncated_count = 0
    removed_chars = 0
    for msg in tool_msgs:
        truncation = truncate_tool_result_with_stats(msg.content or "", context_window_k, limit_tokens=per_tool_budget)
        msg.content = truncation.content
        if truncation.truncated:
            truncated_count += 1
            removed_chars += truncation.removed_chars

    if truncated_count:
        logger.bind(uid=uid, session_id=session_id).info(
            t(
                "LOG_CONTEXT_TOOL_RESULTS_TRUNCATED_SCANNED",
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
