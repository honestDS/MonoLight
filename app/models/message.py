import json
import time
from datetime import datetime
from enum import StrEnum
from typing import (
    Any,
    Literal,
)

from pydantic import BaseModel, ConfigDict, ValidationInfo, field_validator
from pydantic import Field as PyField
from sqlalchemy import Text
from sqlmodel import (
    JSON,
    Column,
    DateTime,
    Field,
    SQLModel,
)

from app.core.utils.time import get_local_time


class MessageRole(StrEnum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    ERR = "err"


class MessageType(StrEnum):
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"
    AUDIT_CONFIRMATION = "audit_confirmation"
    BACKGROUND_TASK_RESULT = "background_result"
    SCHEDULED_TASK_TRIGGER = "scheduled_task_trigger"


class MessagePart(BaseModel):
    type: str


class TextPart(MessagePart):
    type: Literal["text"] = "text"
    text: str


class ImagePart(MessagePart):
    type: Literal["image_url"] = "image_url"
    image_url: dict[str, str]


class FilePart(MessagePart):
    type: Literal["file"] = "file"
    path: str


class InternalToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class InternalMessage(BaseModel):
    id: int | None = None
    role: MessageRole
    content: str | list[TextPart | ImagePart | FilePart | MessagePart] | None = None
    environment_prompt: str | None = None
    tool_calls: list[InternalToolCall] | None = None
    tool_call_id: str | None = None
    attachments: list[str] | None = None
    created_at: float = PyField(default_factory=lambda: time.time())


class InternalResponse(BaseModel):
    message: InternalMessage
    model: str
    usage: dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


class MessageBase(SQLModel):
    session_id: str = Field(index=True, max_length=100)
    uid: str = Field(index=True, max_length=100)
    role: MessageRole = Field(max_length=20)
    type: MessageType = Field(default=MessageType.TEXT, max_length=40)
    content: str | None = Field(default=None)
    attachments: list[str] | None = Field(default=None, sa_column=Column(JSON))
    is_processed: bool = Field(default=False)


class Message(MessageBase, table=True):
    __tablename__ = "message"
    id: int | None = Field(default=None, primary_key=True, index=True)
    profile_id: int = Field()
    environment_prompt: str | None = Field(default=None, sa_column=Column(Text))
    dedupe_key: str | None = Field(default=None, unique=True, max_length=64)
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )


class MessageCreate(MessageBase):
    profile_id: int
    environment_prompt: str | None = None


class MessageResponse(MessageBase):
    id: int
    profile_id: int
    created_at: Any
    content: str | list[Any] | dict[str, Any] | None = None
    model_config = ConfigDict(from_attributes=True)

    @field_validator("content", mode="before")
    @classmethod
    def parse_content(cls, v, info: ValidationInfo):
        if isinstance(v, str):
            try:
                parsed = json.loads(v)
                if isinstance(parsed, list):
                    return parsed
                if isinstance(parsed, dict) and info.data.get("type") == MessageType.AUDIT_CONFIRMATION:
                    return parsed
            except Exception:
                pass
        return v

    @field_validator("created_at", mode="before")
    @classmethod
    def convert_datetime_to_timestamp(cls, v):
        if isinstance(v, datetime):
            return v.timestamp()
        return v


class ChatCompletionRequest(BaseModel):
    message: str | list[TextPart | ImagePart | FilePart | MessagePart]
    attachments: list[str] | None = None
    session_id: str | None = None
    stream: bool | None = False
