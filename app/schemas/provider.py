from pydantic import ConfigDict, BaseModel
from typing import Optional
from app.models.provider import ProviderType


class ProviderBase(BaseModel):
    name: Optional[str] = None
    provider_type: Optional[ProviderType] = None
    api_key: Optional[str] = None
    base_url: Optional[str] = None
    is_active: Optional[bool] = None


class ProviderCreate(BaseModel):
    name: str
    provider_type: ProviderType
    api_key: str
    base_url: Optional[str] = None
    is_active: bool = True


class ProviderUpdate(ProviderBase):
    pass


class ProviderRead(BaseModel):
    id: int
    name: str
    provider_type: ProviderType
    api_key: str
    base_url: Optional[str] = None
    is_active: bool = True
    model_config = ConfigDict(from_attributes=True)
