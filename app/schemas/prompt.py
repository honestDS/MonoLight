from datetime import datetime
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field


class PromptCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, examples=["General Assistant"])
    content: str = Field(..., min_length=1, examples=["You are a helpful assistant."])


class PromptUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    content: Optional[str] = Field(None, min_length=1)


class PromptResponse(BaseModel):
    id: int
    name: str
    content: str
    created_at: datetime
    model_config = ConfigDict(from_attributes=True)
