from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_PUBLICATION_CONFLICT,
    ERR_MEMORY_RECORD_NOT_FOUND,
)
from app.core.exceptions import ParameterException, ResourceNotFoundException


class MemoryValidationError(ParameterException):
    def __init__(self, message: str = ERR_MEMORY_FIELD_TYPE_INVALID, code: int = 400, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


class MemoryConflictError(ParameterException):
    def __init__(self, message: str = ERR_MEMORY_PUBLICATION_CONFLICT, code: int = 409, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


class MemoryNotFoundError(ResourceNotFoundException):
    def __init__(self, message: str = ERR_MEMORY_RECORD_NOT_FOUND, code: int = 404, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


__all__ = [
    "MemoryConflictError",
    "MemoryNotFoundError",
    "MemoryValidationError",
]
