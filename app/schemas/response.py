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


class PageData[T](BaseModel):
    items: list[T]
    total: int
    page: int
    size: int


class LLMChoiceMessage(BaseModel):
    role: str
    content: str


class LLMChoice(BaseModel):
    message: LLMChoiceMessage
    finish_reason: bool | str | None = True
    created_at: float


class LLMResponse(BaseModel):
    choices: list[LLMChoice]
    history: list[dict] | None = None
