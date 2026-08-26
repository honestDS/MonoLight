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
