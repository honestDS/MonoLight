import os

import tiktoken
from dotenv import load_dotenv

load_dotenv()


def _estimate_tokens_by_chars(text: str) -> int:
    chinese_count = sum(1 for character in text if "\u4e00" <= character <= "\u9fff")
    other_count = len(text) - chinese_count
    chinese_token_coefficient = float(os.getenv("TOKEN_COEFF_CHINESE", 1.5))
    other_token_coefficient = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
    return int(chinese_count * chinese_token_coefficient + other_count * other_token_coefficient)


def estimate_tokens(text: str) -> int:
    """
    估算文本的 Token 数量。使用 tiktoken 进行更准确的估算，如果没有安装则回退到字符估算。
    默认使用 gpt-3.5-turbo 的编码方式 (cl100k_base)。
    """
    if not text:
        return 0

    try:
        # 使用 tiktoken 计算更准确的 token 数量 (基于 OpenAI 的 cl100k_base)
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text, disallowed_special=()))
    except Exception:
        # 降级：如果 tiktoken 解析失败或未安装，使用更符合实际的预估值
        # 一个汉字通常占 1~2 个 Token，一个英文单词通常占 1.3 个 Token（英文字母约为 0.25~0.3）
        return _estimate_tokens_by_chars(text)


def truncate_text_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    """Return an original-text prefix that fits within the token budget."""
    if not text:
        return text, False
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        return "", True

    try:
        encoding = tiktoken.get_encoding("cl100k_base")
        token_ids = encoding.encode(text, disallowed_special=())
        if len(token_ids) <= max_tokens:
            return text, False

        prefix_bytes = encoding.decode_bytes(token_ids[:max_tokens])
        try:
            prefix = prefix_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            prefix = prefix_bytes[: exc.start].decode("utf-8")
        while prefix and len(encoding.encode(prefix, disallowed_special=())) > max_tokens:
            prefix = prefix[:-1]
        return prefix, True
    except Exception:
        if _estimate_tokens_by_chars(text) <= max_tokens:
            return text, False

        low = 0
        high = len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if _estimate_tokens_by_chars(text[:middle]) <= max_tokens:
                low = middle
            else:
                high = middle - 1
        return text[:low], True
