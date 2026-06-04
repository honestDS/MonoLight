import time
from datetime import UTC, datetime
from enum import StrEnum
from typing import (
    Any,
)

from pydantic import BaseModel, ConfigDict, field_validator
from pydantic import Field as PyField
from sqlmodel import (
    Column,
    DateTime,
    Field,
    SQLModel,
)


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


class InternalToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class InternalMessage(BaseModel):
    id: int | None = None
    role: MessageRole
    content: str | None = None
    tool_calls: list[InternalToolCall] | None = None
    tool_call_id: str | None = None
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
    type: MessageType = Field(default=MessageType.TEXT, max_length=20)
    content: str | None = Field(default=None)


class Message(MessageBase, table=True):
    __tablename__ = "message"
    id: int | None = Field(default=None, primary_key=True, index=True)
    profile_id: int = Field(foreign_key="profile.id")
    created_at: datetime = Field(
        default_factory=lambda: datetime.now(UTC),
        sa_column=Column(DateTime(timezone=True)),
    )


class MessageCreate(MessageBase):
    profile_id: int


class MessageResponse(MessageBase):
    id: int
    profile_id: int
    created_at: Any
    model_config = ConfigDict(from_attributes=True)

    @field_validator("created_at", mode="before")
    @classmethod
    def convert_datetime_to_timestamp(cls, v):
        if isinstance(v, datetime):
            return v.timestamp()
        return v


class ChatCompletionRequest(BaseModel):
    message: str
    session_id: str | None = None
    stream: bool | None = False
