from datetime import datetime
from enum import Enum
from typing import Optional, List, Dict, Any
from sqlmodel import SQLModel, Field, Column, DateTime, String, ForeignKey
from pydantic import ConfigDict, BaseModel

class MessageRole(str, Enum):
    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

class InternalToolCall(BaseModel):
    id: str
    name: str
    arguments: Dict[str, Any]

class InternalMessage(BaseModel):
    role: MessageRole
    content: Optional[str] = None
    tool_calls: Optional[List[InternalToolCall]] = None
    tool_call_id: Optional[str] = None

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
    content: Optional[str] = Field(default=None)

class Message(MessageBase, table=True):
    __tablename__ = "message"
    id: Optional[int] = Field(default=None, primary_key=True, index=True)
    profile_id: int = Field(foreign_key="profile.id")
    created_at: datetime = Field(
        default_factory=datetime.now,
        sa_column=Column(DateTime)
    )

class MessageCreate(MessageBase):
    profile_id: int

class MessageResponse(MessageBase):
    id: int
    profile_id: int
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
    
class ChatCompletionRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    stream: Optional[bool] = False
