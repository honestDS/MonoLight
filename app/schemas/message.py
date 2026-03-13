from pydantic import BaseModel, Field
from typing import List, Optional, Any
from datetime import datetime

class ChatCompletionRequest(BaseModel):
    message: str
    stream: Optional[bool] = False

class UniversalMessageModel(BaseModel):
    platform: str
    sender_id: str
    sender_name: Optional[str] = None
    room_id: Optional[str] = None
    content: str
    raw_data: Optional[Any] = None
    timestamp: datetime = Field(default_factory=datetime.now)