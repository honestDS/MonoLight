import os
from dataclasses import dataclass

import tiktoken

TRUNCATION_NOTICE = "\n\n[工具响应内容过大，已省略后半部分。请基于以上部分结果作答，或调整工具调用参数后重新查询]"


@dataclass(frozen=True)
class ToolResultTruncation:
    content: str
    truncated: bool
    original_tokens: int
    final_tokens: int
    removed_chars: int


def _estimate_tokens_by_chars(text: str) -> int:
    c_coeff = float(os.getenv("TOKEN_COEFF_CHINESE", 1.5))
    o_coeff = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
    chinese_count = sum(1 for character in text if "\u4e00" <= character <= "\u9fff")
    other_count = len(text) - chinese_count
    return int(chinese_count * c_coeff + other_count * o_coeff)


def truncate_tool_result_with_stats(content: str, context_window_k: int) -> ToolResultTruncation:
    """对单条工具响应做 token 级截断，并返回截断统计信息。

    当工具响应的 token 数超过模型上下文限制（context_window_k * 1024）的一半时，
    将内容截断至该上限的一半，并在末尾追加提示，引导 LLM 基于部分信息作答或调整参数后重新查询。
    """
    if not content:
        return ToolResultTruncation(content=content, truncated=False, original_tokens=0, final_tokens=0, removed_chars=0)

    limit_tokens = max(1, (context_window_k * 1024) // 2)

    try:
        encoding = tiktoken.get_encoding("cl100k_base")
        token_ids = encoding.encode(content, disallowed_special=())
        original_tokens = len(token_ids)
        if original_tokens <= limit_tokens:
            return ToolResultTruncation(content=content, truncated=False, original_tokens=original_tokens, final_tokens=original_tokens, removed_chars=0)

        truncated_body = encoding.decode(token_ids[:limit_tokens])
        truncated_content = truncated_body + TRUNCATION_NOTICE
        final_tokens = len(encoding.encode(truncated_content, disallowed_special=()))
        return ToolResultTruncation(
            content=truncated_content,
            truncated=True,
            original_tokens=original_tokens,
            final_tokens=final_tokens,
            removed_chars=max(len(content) - len(truncated_body), 0),
        )
    except Exception:
        # 降级：tiktoken 不可用时按字符估算系数反推字符上限
        c_coeff = float(os.getenv("TOKEN_COEFF_CHINESE", 1.5))
        o_coeff = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
        avg_coeff = max((c_coeff + o_coeff) / 2, 0.1)
        char_limit = max(1, int(limit_tokens / avg_coeff))
        original_tokens = _estimate_tokens_by_chars(content)
        if len(content) <= char_limit:
            return ToolResultTruncation(content=content, truncated=False, original_tokens=original_tokens, final_tokens=original_tokens, removed_chars=0)

        truncated_body = content[:char_limit]
        truncated_content = truncated_body + TRUNCATION_NOTICE
        return ToolResultTruncation(
            content=truncated_content,
            truncated=True,
            original_tokens=original_tokens,
            final_tokens=_estimate_tokens_by_chars(truncated_content),
            removed_chars=max(len(content) - len(truncated_body), 0),
        )


def truncate_tool_result(content: str, context_window_k: int) -> tuple[str, bool]:
    """对单条工具响应做 token 级截断。

    返回 (处理后的内容, 是否发生截断)。
    """
    result = truncate_tool_result_with_stats(content, context_window_k)
    return result.content, result.truncated
