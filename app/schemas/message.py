from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


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
    tool_call_id: Optional[str] = None  # 用于回复具体的工具调用结果


class InternalResponse(BaseModel):
    message: InternalMessage
    model: str
    usage: Dict[str, Any] = Field(
        default_factory=lambda: {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
    )


class ChatCompletionRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    stream: Optional[bool] = False
