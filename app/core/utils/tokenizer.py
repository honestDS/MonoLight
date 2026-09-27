import os
import re

import tiktoken
from dotenv import load_dotenv

load_dotenv()

_DEFAULT_ENCODING = "cl100k_base"
_VERSIONED_GPT_ALIAS_PATTERN = re.compile(r"^(gpt-\d+)\.(\d+)(.*)$", re.IGNORECASE)


def _estimate_tokens_by_chars(text: str) -> int:
    chinese_count = sum(1 for character in text if "\u4e00" <= character <= "\u9fff")
    other_count = len(text) - chinese_count
    chinese_token_coefficient = float(os.getenv("TOKEN_COEFF_CHINESE", 1.5))
    other_token_coefficient = float(os.getenv("TOKEN_COEFF_OTHER", 0.3))
    return int(chinese_count * chinese_token_coefficient + other_count * other_token_coefficient)


def resolve_token_encoding_name(
    model_id: str | None = None,
    *,
    protocol: str | None = None,
) -> str:
    # 协议只描述请求格式，不能用于推断真实模型厂商（例如 NewAPI/OpenRouter）。
    del protocol
    normalized_model = model_id.strip() if isinstance(model_id, str) else ""
    if normalized_model:
        try:
            return tiktoken.encoding_name_for_model(normalized_model)
        except KeyError:
            try:
                return tiktoken.encoding_name_for_model(f"{normalized_model}-")
            except KeyError:
                pass

            match = _VERSIONED_GPT_ALIAS_PATTERN.match(normalized_model)
            if match is not None:
                compatibility_name = f"{match.group(1)}-{match.group(2)}{match.group(3)}"
                try:
                    return tiktoken.encoding_name_for_model(compatibility_name)
                except KeyError:
                    pass
    return _DEFAULT_ENCODING


def estimate_tokens(
    text: str,
    *,
    model_id: str | None = None,
    protocol: str | None = None,
    encoding_name: str | None = None,
) -> int:
    """Estimate text tokens with a model-aware tiktoken encoding when available."""
    if not text:
        return 0

    try:
        resolved_encoding_name = encoding_name or resolve_token_encoding_name(
            model_id,
            protocol=protocol,
        )
        encoding = tiktoken.get_encoding(resolved_encoding_name)
        return len(encoding.encode(text, disallowed_special=()))
    except Exception:
        return _estimate_tokens_by_chars(text)


def truncate_text_to_tokens(
    text: str,
    max_tokens: int,
    *,
    model_id: str | None = None,
    protocol: str | None = None,
    encoding_name: str | None = None,
) -> tuple[str, bool]:
    """Return an original-text prefix that fits within the token budget."""
    if not text:
        return text, False
    if not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0:
        return "", True

    try:
        resolved_encoding_name = encoding_name or resolve_token_encoding_name(
            model_id,
            protocol=protocol,
        )
        encoding = tiktoken.get_encoding(resolved_encoding_name)
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
