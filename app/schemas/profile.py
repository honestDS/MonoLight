from pydantic import BaseModel, Field, field_validator
from typing import Optional, Dict, Any

class ProfileBase(BaseModel):
    name: str
    provider_id: int
    model_id: str
    temperature: Optional[float] = Field(0.7, ge=0, le=2.0)
    top_p: Optional[float] = Field(1.0, ge=0, le=1.0)
    max_tokens: Optional[int] = 2048
    stream: Optional[bool] = False
    extra_config: Optional[Dict[str, Any]] = None
    context_window_k: Optional[int] = Field(4, ge=1)
    prompt_id: Optional[int] = None

class ProfileCreate(ProfileBase):
    pass

class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    provider_id: Optional[int] = None
    model_id: Optional[str] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: Optional[bool] = None
    context_window_k: Optional[int] = None
    is_active: Optional[bool] = None
    prompt_id: Optional[int] = None

class ProfileResponse(ProfileBase):
    id: int
    is_active: bool
    provider_name: Optional[str] = None
    class Config:
        from_attributes = True
