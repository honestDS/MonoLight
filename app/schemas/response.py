from enum import StrEnum
from typing import (
    Any,
    TypeVar,
)

from pydantic import BaseModel

from app.core.i18n import t

T = TypeVar("T")


class FinishReason(StrEnum):
    STOP = "stop"
    LENGTH = "length"
    TOOL_CALLS = "tool_calls"
    CONTENT_FILTER = "content_filter"
    ERROR = "error"


class StandardResponse[T](BaseModel):
    code: int = 200
    message: str = "MSG_GENERIC_SUCCESS"
    data: T | None = None

    @classmethod
    def success(cls, data: Any = None, message: str = "MSG_GENERIC_SUCCESS", raw_message: bool = False, **kwargs):
        if not raw_message:
            message = t(message, default=message, **kwargs)
        return cls(code=200, message=message, data=data)

    @classmethod
    def error(cls, code: int = 500, message: str = "ERR_GENERIC_ERROR", raw_message: bool = False, **kwargs):
        if not raw_message:
            message = t(message, default=message, **kwargs)
        return cls(code=code, message=message, data=None)


class PageData[T](BaseModel):
    items: list[T]
    total: int
    page: int
    size: int
    meta: dict[str, Any] | None = None


class SentFile(BaseModel):
    id: str
    name: str
    mime_type: str
    size: int
    download_url: str
    previewable: bool = False
    description: str | None = None


class LLMChoiceMessage(BaseModel):
    role: str
    content: str


class LLMChoice(BaseModel):
    message: LLMChoiceMessage
    finish_reason: bool | str | FinishReason | None = True
    created_at: float


class LLMResponse(BaseModel):
    choices: list[LLMChoice]
    history: list[dict] | None = None
    files: list[SentFile] | None = None
