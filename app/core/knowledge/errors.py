from __future__ import annotations

from typing import Any

from app.core.constants import ERR_MANAGED_KNOWLEDGE_CONTENT_TOO_LONG, ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND, ERR_MANAGED_KNOWLEDGE_VERSION_CONFLICT, MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS
from app.core.exceptions import ParameterException, ResourceNotFoundException


class ManagedKnowledgeValidationError(ParameterException):
    def __init__(self, message: str = ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID, code: int = 400, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


class ManagedKnowledgeContentTooLongError(ManagedKnowledgeValidationError):
    def __init__(self, actual_tokens: int, max_tokens: int = MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS) -> None:
        super().__init__(
            message=ERR_MANAGED_KNOWLEDGE_CONTENT_TOO_LONG,
            code=400,
            data={"status": "content_too_long", "actual_tokens": actual_tokens, "max_tokens": max_tokens, "retryable": True},
            params={"actual_tokens": actual_tokens, "max_tokens": max_tokens},
        )


class ManagedKnowledgeConflictError(ParameterException):
    def __init__(self, message: str = ERR_MANAGED_KNOWLEDGE_VERSION_CONFLICT, code: int = 409, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


class ManagedKnowledgeNotFoundError(ResourceNotFoundException):
    def __init__(self, message: str = ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND, code: int = 404, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


__all__ = ["ManagedKnowledgeConflictError", "ManagedKnowledgeContentTooLongError", "ManagedKnowledgeNotFoundError", "ManagedKnowledgeValidationError"]
