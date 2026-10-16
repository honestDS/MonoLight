from dataclasses import dataclass
from typing import Any

from app.core.constants import (
    ERR_API_KEY_CRYPTO_FAILED,
    ERR_GENERIC_ERROR,
    ERR_INTERNAL_SERVER_ERROR,
    ERR_KB_NOT_FOUND,
    ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH,
    ERR_LLM_UNEXPECTED_ERROR,
    ERR_PROFILE_EMBEDDING_CALL_FAILED,
    ERR_PROFILE_RERANK_CALL_FAILED,
    ERR_SESSION_NO_PERMISSION,
    ERR_UNAUTHORIZED,
    ERR_VALIDATION_FAILED,
)
from app.core.i18n import t


@dataclass(slots=True)
class MessageSpec:
    key: str
    params: dict[str, Any] | None = None
    default: str | None = None

    def render(self) -> str:
        return t(self.key, default=self.default or self.key, **(self.params or {}))


class BaseBusinessException(Exception):
    def __init__(
        self,
        message: str = ERR_GENERIC_ERROR,
        code: int = 500,
        data: Any = None,
        params: dict[str, Any] | None = None,
        default_message: str | None = None,
        cause: str | None = None,
        **kwargs,
    ):
        merged_params = dict(params or {})
        if kwargs:
            merged_params.update(kwargs)

        self.code = code
        self.data = data
        self.message_spec = MessageSpec(key=message, params=merged_params or None, default=default_message)
        self.message = self.message_spec.key
        self.kwargs = self.message_spec.params or {}
        self.default_message = default_message
        self.cause = cause
        super().__init__(self.render_message())

    def render_message(self) -> str:
        return self.message_spec.render()

    def to_response(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "message": self.render_message(),
            "data": self.data,
        }


class AuthException(BaseBusinessException):
    def __init__(self, message: str = ERR_UNAUTHORIZED, code: int = 401, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ForbiddenException(BaseBusinessException):
    def __init__(self, message: str = ERR_SESSION_NO_PERMISSION, code: int = 403, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ResourceNotFoundException(BaseBusinessException):
    def __init__(self, message: str = ERR_KB_NOT_FOUND, code: int = 404, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ParameterException(BaseBusinessException):
    def __init__(self, message: str = ERR_VALIDATION_FAILED, code: int = 400, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ServerException(BaseBusinessException):
    def __init__(self, message: str = ERR_INTERNAL_SERVER_ERROR, code: int = 500, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ApiKeyException(BaseBusinessException):
    def __init__(self, message: str = ERR_API_KEY_CRYPTO_FAILED, code: int = 500, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class LLMException(BaseBusinessException):
    def __init__(self, message: str = ERR_LLM_UNEXPECTED_ERROR, code: int = 502, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class LLMRequestRejectedException(LLMException):
    def __init__(
        self,
        message: str,
        *,
        source: str,
        reason: str,
        allow_channel_fallback: bool = False,
        upstream_status: int | None = None,
        upstream_detail: Any = None,
        code: int = 400,
        data: dict[str, Any] | None = None,
        **kwargs,
    ):
        self.source = source
        self.reason = reason
        self.allow_channel_fallback = allow_channel_fallback
        self.upstream_status = upstream_status
        self.upstream_detail = upstream_detail

        merged_data = dict(data or {})
        merged_data.update(source=source, reason=reason)
        if upstream_status is not None:
            merged_data["upstream_status"] = upstream_status

        super().__init__(message=message, code=code, data=merged_data, **kwargs)


def find_llm_request_rejection(exc: BaseException) -> LLMRequestRejectedException | None:
    seen: set[int] = set()
    first_fallback: LLMRequestRejectedException | None = None

    def visit(current: BaseException) -> LLMRequestRejectedException | None:
        nonlocal first_fallback

        current_id = id(current)
        if current_id in seen:
            return None
        seen.add(current_id)

        if isinstance(current, LLMRequestRejectedException):
            if not current.allow_channel_fallback:
                return current
            if first_fallback is None:
                first_fallback = current

        if isinstance(current, BaseExceptionGroup):
            for nested in current.exceptions:
                rejection = visit(nested)
                if rejection is not None:
                    return rejection

        cause = current.__cause__
        if cause is not None:
            rejection = visit(cause)
            if rejection is not None:
                return rejection
        elif not current.__suppress_context__ and current.__context__ is not None:
            rejection = visit(current.__context__)
            if rejection is not None:
                return rejection

        return None

    return visit(exc) or first_fallback


class LLMModelCapabilityException(LLMException):
    """The selected model repeatedly violated a required output contract."""


class LLMContextLengthException(LLMException):
    """模型供应商明确返回请求上下文长度超限。"""

    def __init__(
        self,
        message: str = ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH,
        code: int = 400,
        *,
        provider_message: str | None = None,
        **kwargs,
    ):
        self.provider_message = provider_message
        super().__init__(message=message, code=code, **kwargs)


class EmbeddingException(LLMException):
    def __init__(self, message: str = ERR_PROFILE_EMBEDDING_CALL_FAILED, code: int = 502, **kwargs):
        super().__init__(message=message, code=code, **kwargs)


class RerankException(LLMException):
    def __init__(self, message: str = ERR_PROFILE_RERANK_CALL_FAILED, code: int = 502, **kwargs):
        super().__init__(message=message, code=code, **kwargs)
