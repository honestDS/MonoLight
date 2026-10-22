from app.core.constants import (
    ERR_KB_CONTENT_ENCODING_ERROR,
    ERR_KB_CONTENT_TOO_LARGE,
    ERR_KB_CONTENT_TOO_LONG,
    KNOWLEDGE_CONTENT_MAX_BYTES,
    KNOWLEDGE_CONTENT_MAX_CHARS,
)
from app.core.exceptions import ParameterException


def validate_knowledge_content(content: str) -> None:
    if len(content) > KNOWLEDGE_CONTENT_MAX_CHARS:
        raise ParameterException(ERR_KB_CONTENT_TOO_LONG, max_chars=KNOWLEDGE_CONTENT_MAX_CHARS)

    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError:
        raise ParameterException(ERR_KB_CONTENT_ENCODING_ERROR) from None

    if len(content_bytes) > KNOWLEDGE_CONTENT_MAX_BYTES:
        raise ParameterException(ERR_KB_CONTENT_TOO_LARGE, max_bytes=KNOWLEDGE_CONTENT_MAX_BYTES)
