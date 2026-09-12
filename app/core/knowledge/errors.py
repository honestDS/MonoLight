from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_KNOWLEDGE_ORGANIZATION_CONTEXT_EXCEEDED,
    ERR_KNOWLEDGE_ORGANIZATION_FAILED,
    ERR_KNOWLEDGE_ORGANIZATION_MODEL_CONFIG_INVALID,
    ERR_KNOWLEDGE_ORGANIZATION_MODEL_EXECUTION_FAILED,
    ERR_KNOWLEDGE_ORGANIZATION_NOT_CONVERGED,
    ERR_MANAGED_KNOWLEDGE_CONTAINER_CONFLICT,
    ERR_MANAGED_KNOWLEDGE_CONTENT_TOO_LONG,
    ERR_MANAGED_KNOWLEDGE_FIELD_TYPE_INVALID,
    ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND,
    ERR_MANAGED_KNOWLEDGE_RUNTIME_UNAVAILABLE,
    ERR_MANAGED_KNOWLEDGE_VERSION_CONFLICT,
    MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS,
)
from app.core.exceptions import LLMException, ParameterException, ResourceNotFoundException, ServerException


def _organization_error_data(
    *,
    status: str,
    retryable: bool,
    data: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {"status": status, "retryable": retryable}
    if data:
        payload.update(data)
    return payload


class KnowledgeOrganizationExecutionError(ServerException):
    def __init__(
        self,
        message: str = ERR_KNOWLEDGE_ORGANIZATION_FAILED,
        code: int = 500,
        *,
        status: str = "knowledge_organization_failed",
        retryable: bool = False,
        data: dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            data=_organization_error_data(status=status, retryable=retryable, data=data),
            **kwargs,
        )


class KnowledgeOrganizationContextExceededError(ParameterException):
    def __init__(
        self,
        message: str = ERR_KNOWLEDGE_ORGANIZATION_CONTEXT_EXCEEDED,
        code: int = 400,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            data=_organization_error_data(status="organization_context_exceeded", retryable=False),
            **kwargs,
        )


class KnowledgeOrganizationConfigurationError(ParameterException):
    def __init__(
        self,
        message: str = ERR_KNOWLEDGE_ORGANIZATION_MODEL_CONFIG_INVALID,
        code: int = 400,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            data=_organization_error_data(status="organization_model_config_invalid", retryable=False),
            **kwargs,
        )


class KnowledgeOrganizationModelFailedError(LLMException):
    def __init__(
        self,
        message: str = ERR_KNOWLEDGE_ORGANIZATION_MODEL_EXECUTION_FAILED,
        code: int = 502,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            data=_organization_error_data(status="organization_model_execution_failed", retryable=True),
            **kwargs,
        )


class KnowledgeOrganizationNotConvergedError(ServerException):
    def __init__(
        self,
        message: str = ERR_KNOWLEDGE_ORGANIZATION_NOT_CONVERGED,
        code: int = 500,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            message=message,
            code=code,
            data=_organization_error_data(status="organization_not_converged", retryable=False),
            **kwargs,
        )


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


class ManagedKnowledgeRuntimeUnavailableError(ManagedKnowledgeConflictError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            message=ERR_MANAGED_KNOWLEDGE_RUNTIME_UNAVAILABLE,
            code=409,
            data={"status": "runtime_unavailable", "retryable": True},
            **kwargs,
        )


class ManagedKnowledgeContainerConflictError(ManagedKnowledgeConflictError):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(
            message=ERR_MANAGED_KNOWLEDGE_CONTAINER_CONFLICT,
            code=409,
            data={"status": "container_conflict", "retryable": True},
            **kwargs,
        )


class ManagedKnowledgeNotFoundError(ResourceNotFoundException):
    def __init__(self, message: str = ERR_MANAGED_KNOWLEDGE_ITEM_NOT_FOUND, code: int = 404, **kwargs: Any) -> None:
        super().__init__(message=message, code=code, **kwargs)


__all__ = [
    "KnowledgeOrganizationConfigurationError",
    "KnowledgeOrganizationContextExceededError",
    "KnowledgeOrganizationExecutionError",
    "KnowledgeOrganizationModelFailedError",
    "KnowledgeOrganizationNotConvergedError",
    "ManagedKnowledgeConflictError",
    "ManagedKnowledgeContainerConflictError",
    "ManagedKnowledgeContentTooLongError",
    "ManagedKnowledgeNotFoundError",
    "ManagedKnowledgeRuntimeUnavailableError",
    "ManagedKnowledgeValidationError",
]
