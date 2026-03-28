from datetime import datetime
import time
from enum import Enum
from typing import (
    Optional,
    List,
    Dict,
    Any,
)
from sqlmodel import (
    SQLModel,
    Field,
    Column,
    DateTime,
)
from pydantic import (
    field_validator,
    ConfigDict,
    BaseModel,
    Field as PyField
)


class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"
    ERR = "err"


class MessageType(str, Enum):
    TEXT = "text"
    TOOL_CALL = "tool_call"
    TOOL_RESULT = "tool_result"


class InternalToolCall(BaseModel):
    id: str
    name: str
    arguments: Dict[str, Any]


class InternalMessage(BaseModel):
    role: MessageRole
    content: Optional[str] = None
    tool_calls: Optional[List[InternalToolCall]] = None
    tool_call_id: Optional[str] = None
    created_at: float = PyField(default_factory=lambda: time.time())


class InternalResponse(BaseModel):
    message: InternalMessage
    model: str
    usage: Dict[str, Any] = {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
    }


class MessageBase(SQLModel):
    session_id: str = Field(index=True, max_length=100)
    uid: str = Field(index=True, max_length=100)
    role: MessageRole = Field(max_length=20)
    type: MessageType = Field(default=MessageType.TEXT, max_length=20)
    content: Optional[str] = Field(default=None)


class Message(MessageBase, table=True):
    __tablename__ = "message"
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    profile_id: int = Field(foreign_key="profile.id")
    created_at: datetime = Field(
        default_factory=datetime.now, sa_column=Column(DateTime)
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
    session_id: Optional[str] = None
    stream: Optional[bool] = False
