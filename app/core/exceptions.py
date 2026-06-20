from typing import Any


class BaseBusinessException(Exception):
    def __init__(self, code: int = 500, message: str = "业务异常", data: Any = None, **kwargs):
        self.code = code
        self.message = message
        self.data = data
        self.kwargs = kwargs
        super().__init__(self.message)


class AuthException(BaseBusinessException):
    def __init__(self, message: str = "认证失败", code: int = 401, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ForbiddenException(BaseBusinessException):
    def __init__(self, message: str = "权限不足", code: int = 403, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ResourceNotFoundException(BaseBusinessException):
    def __init__(self, message: str = "资源不存在", code: int = 404, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ParameterException(BaseBusinessException):
    def __init__(self, message: str = "参数错误", code: int = 400, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ServerException(BaseBusinessException):
    def __init__(self, message: str = "系统内部错误", code: int = 500, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class ApiKeyException(BaseBusinessException):
    def __init__(self, message: str = "API Key 加解密异常", code: int = 500, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class LLMException(BaseBusinessException):
    def __init__(self, message: str = "大模型调用异常", code: int = 502, **kwargs):
        super().__init__(code=code, message=message, **kwargs)


class EmbeddingException(LLMException):
    """向量模型调用异常。

    属于非主线（对话）流程的异常类型，便于在调用方按类型区分处理：
    通常仅记录日志并降级，不应中断对话主线调用。
    继承自 LLMException 以兼容既有 `except LLMException` 捕获逻辑。
    """

    def __init__(self, message: str = "向量模型调用异常", code: int = 502, **kwargs):
        super().__init__(message=message, code=code, **kwargs)


class RerankException(LLMException):
    """重排（Rerank）模型调用异常。

    属于非主线（对话）流程的异常类型，便于在调用方按类型区分处理：
    通常仅记录日志并降级，不应中断对话主线调用。
    继承自 LLMException 以兼容既有 `except LLMException` 捕获逻辑。
    """

    def __init__(self, message: str = "重排模型调用异常", code: int = 502, **kwargs):
        super().__init__(message=message, code=code, **kwargs)
