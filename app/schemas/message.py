from pydantic import BaseModel
from typing import Optional, Any
from datetime import datetime

class UniversalMessageModel(BaseModel):
    platform: str
    sender_id: str
    sender_name: Optional[str] = None
    room_id: Optional[str] = None
    content: str
    raw_data: Optional[Any] = None
    timestamp: datetime = datetime.now()
