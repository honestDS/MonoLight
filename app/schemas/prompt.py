from pydantic import ConfigDict, BaseModel
from datetime import datetime
from typing import Optional


class PromptCreate(BaseModel):
    name: str
    content: str


class PromptUpdate(BaseModel):
    name: Optional[str] = None
    content: Optional[str] = None


class PromptResponse(BaseModel):
    id: int
    name: str
    content: str
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)
