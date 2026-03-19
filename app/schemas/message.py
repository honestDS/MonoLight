from datetime import datetime
from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatCompletionRequest(BaseModel):
    message: str
    session_id: Optional[str] = Field(
        None, description="会话唯一标识，若不传则自动生成"
    )
    stream: Optional[bool] = False


class UniversalMessageModel(BaseModel):
    platform: str
    sender_id: str
    sender_name: Optional[str] = None
    room_id: Optional[str] = None
    content: str
    raw_data: Optional[Any] = None
    timestamp: datetime = Field(default_factory=datetime.now)
