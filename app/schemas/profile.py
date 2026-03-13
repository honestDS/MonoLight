from pydantic import BaseModel
from typing import Optional, Dict, Any

class ProfileBase(BaseModel):
    name: str
    provider_id: int
    model_id: str
    temperature: Optional[float] = 0.7
    top_p: Optional[float] = 1.0
    max_tokens: Optional[int] = 2048
    stream: Optional[bool] = False
    extra_config: Optional[Dict[str, Any]] = None

class ProfileCreate(ProfileBase):
    pass

class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    provider_id: Optional[int] = None
    model_id: Optional[str] = None
    temperature: Optional[float] = None
    is_active: Optional[bool] = None

class ProfileResponse(ProfileBase):
    id: int
    is_active: bool

    class Config:
        from_attributes = True