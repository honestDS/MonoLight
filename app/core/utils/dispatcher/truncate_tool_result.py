import os

import tiktoken

TRUNCATION_NOTICE = "\n\n[工具响应内容过大，已省略后半部分。请基于以上部分结果作答，或调整工具调用参数后重新查询]"


def truncate_tool_result(content: str, context_window_k: int) -> tuple[str, bool]:
    """对单条工具响应做 token 级截断。

    当工具响应的 token 数超过模型上下文限制（context_window_k * 1024）的一半时，
    将内容截断至该上限的一半，并在末尾追加提示，引导 LLM 基于部分信息作答或调整参数重查。

    返回 (处理后的内容, 是否发生截断)。
    """
    if not content:
        return content, False

    limit_tokens = max(1, (context_window_k * 1024) // 2)

    try:
        encoding = tiktoken.get_encoding("cl100k_base")
        token_ids = encoding.encode(content, disallowed_special=())
        if len(token_ids) <= limit_tokens:
            return content, False
        truncated = encoding.decode(token_ids[:limit_tokens])
        return truncated + TRUNCATION_NOTICE, True
    except Exception:
        # 降级：tiktoken 不可用时按字符估算系数反推字符上限
        c_coeff = float(os.getenv("TOKEN_COEFF_CHINESE", 1.5))
        o_coeff = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
        avg_coeff = max((c_coeff + o_coeff) / 2, 0.1)
        char_limit = max(1, int(limit_tokens / avg_coeff))
        if len(content) <= char_limit:
            return content, False
        return content[:char_limit] + TRUNCATION_NOTICE, True
