from typing import Any


class BaseBusinessException(Exception):
    def __init__(self, code: int = 500, message: str = "业务异常", data: Any = None):
        self.code = code
        self.message = message
        self.data = data
        super().__init__(self.message)


class AuthException(BaseBusinessException):
    def __init__(self, message: str = "认证失败", code: int = 401):
        super().__init__(code=code, message=message)


class ForbiddenException(BaseBusinessException):
    def __init__(self, message: str = "权限不足", code: int = 403):
        super().__init__(code=code, message=message)


class ResourceNotFoundException(BaseBusinessException):
    def __init__(self, message: str = "资源不存在", code: int = 404):
        super().__init__(code=code, message=message)


class ParameterException(BaseBusinessException):
    def __init__(self, message: str = "参数错误", code: int = 400):
        super().__init__(code=code, message=message)


class ServerException(BaseBusinessException):
    def __init__(self, message: str = "系统内部错误", code: int = 500):
        super().__init__(code=code, message=message)


class LLMException(BaseBusinessException):
    def __init__(self, message: str = "大模型调用异常", code: int = 502):
        super().__init__(code=code, message=message)
