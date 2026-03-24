import enum
import re
from typing import Optional
from pydantic import field_validator, ConfigDict
from sqlmodel import SQLModel, Field


class ProviderType(str, enum.Enum):
    OPENAI = "OPENAI"
    GEMINI = "GEMINI"


class ProviderBase(SQLModel):
    name: str = Field(
        index=True, unique=True, nullable=False, min_length=1, max_length=100
    )
    provider_type: ProviderType = Field(nullable=False)
    api_key: str = Field(nullable=False, min_length=1)
    base_url: Optional[str] = Field(default=None)
    is_active: bool = Field(default=True)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.match(r"^https?://", v):
            raise ValueError("base_url must start with http:// or https://")
        return v


class ModelProvider(ProviderBase, table=True):
    __tablename__ = "provider"
    id: Optional[int] = Field(default=None, primary_key=True, index=True)


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(SQLModel):
    name: Optional[str] = Field(None, min_length=1, max_length=100)
    provider_type: Optional[ProviderType] = None
    api_key: Optional[str] = Field(None, min_length=1)
    base_url: Optional[str] = None
    is_active: Optional[bool] = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: Optional[str]) -> Optional[str]:
        if v and not re.match(r"^https?://", v):
            raise ValueError("base_url must start with http:// or https://")
        return v


class ProviderResponse(ProviderBase):
    id: int
    model_config = ConfigDict(from_attributes=True)
