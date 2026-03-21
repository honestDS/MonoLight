from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator
import re
from app.models.provider import ProviderType

class ProviderBase(BaseModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    provider_type: Optional[ProviderType] = None
    api_key: Optional[str] = Field(None, min_length=1)
    base_url: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.match(r'^https?://', v):
            raise ValueError("base_url must start with http:// or https://")
        return v

class ProviderCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100, examples=["OpenAI"])
    provider_type: ProviderType = Field(..., examples=[ProviderType.OPENAI])
    api_key: str = Field(..., min_length=1, examples=["sk-xxx"])
    base_url: Optional[str] = Field(None, examples=["https://api.openai.com/v1"])
    is_active: bool = Field(True)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.match(r'^https?://', v):
            raise ValueError("base_url must start with http:// or https://")
        return v

class ProviderUpdate(ProviderBase):
    pass

class ProviderRead(BaseModel):
    id: int
    name: str
    provider_type: ProviderType
    api_key: str
    base_url: Optional[str] = None
    is_active: bool
    model_config = ConfigDict(from_attributes=True)
