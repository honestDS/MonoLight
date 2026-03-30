from typing import (
    Any,
    TypeVar,
)

from pydantic import BaseModel

T = TypeVar("T")


class StandardResponse[T](BaseModel):
    code: int = 200
    message: str = "成功"
    data: T | None = None

    @classmethod
    def success(cls, data: Any = None, message: str = "成功"):
        return cls(code=200, message=message, data=data)

    @classmethod
    def error(cls, code: int = 500, message: str = "错误"):
        return cls(code=code, message=message, data=None)
