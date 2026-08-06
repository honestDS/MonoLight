from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_MEMORY_CONTENT_TOO_LONG,
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_PUBLICATION_CONFLICT,
    ERR_MEMORY_RECORD_NOT_FOUND,
    MEMORY_CONTENT_MAX_TOKENS,
)
from app.core.exceptions import ParameterException, ResourceNotFoundException


class MemoryValidationError(ParameterException):
    def __init__(self, message: str = ERR_MEMORY_FIELD_TYPE_INVALID, code: int = 400, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


class MemoryContentTooLongError(MemoryValidationError):
    def __init__(self, actual_tokens: int, max_tokens: int = MEMORY_CONTENT_MAX_TOKENS) -> None:
        super().__init__(
            message=ERR_MEMORY_CONTENT_TOO_LONG,
            code=400,
            data={
                "status": "content_too_long",
                "actual_tokens": actual_tokens,
                "max_tokens": max_tokens,
                "retryable": True,
            },
            params={"actual_tokens": actual_tokens, "max_tokens": max_tokens},
        )


class MemoryConflictError(ParameterException):
    def __init__(self, message: str = ERR_MEMORY_PUBLICATION_CONFLICT, code: int = 409, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


class MemoryNotFoundError(ResourceNotFoundException):
    def __init__(self, message: str = ERR_MEMORY_RECORD_NOT_FOUND, code: int = 404, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


__all__ = [
    "MemoryConflictError",
    "MemoryContentTooLongError",
    "MemoryNotFoundError",
    "MemoryValidationError",
]
