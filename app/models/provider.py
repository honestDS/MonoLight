import enum
import re

from pydantic import (
    ConfigDict,
    field_validator,
)
from sqlmodel import (
    Field,
    SQLModel,
)


class ModelUsage(enum.StrEnum):
    CHAT = "CHAT"
    EMBEDDING = "EMBEDDING"


class ProviderType(enum.StrEnum):
    OPENAI = "OPENAI"
    #GEMINI = "GEMINI"


class ProviderBase(SQLModel):
    name: str = Field(
        index=True, unique=True, nullable=False, min_length=1, max_length=100
    )
    provider_type: ProviderType = Field(nullable=False)
    usage: ModelUsage = Field(default=ModelUsage.CHAT, nullable=False)
    api_key: str = Field(nullable=False, min_length=1)
    base_url: str | None = Field(default=None)
    is_active: bool = Field(default=True)

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v and not re.match(r"^https?://", v):
            raise ValueError("base_url must start with http:// or https://")
        return v


class ModelProvider(ProviderBase, table=True):
    __tablename__ = "provider"
    id: int | None = Field(default=None, primary_key=True, index=True)


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(SQLModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    provider_type: ProviderType | None = None
    usage: ModelUsage | None = None
    api_key: str | None = Field(None, min_length=1)
    base_url: str | None = None
    is_active: bool | None = None

    @field_validator("base_url")
    @classmethod
    def validate_url(cls, v: str | None) -> str | None:
        if v and not re.match(r"^https?://", v):
            raise ValueError("base_url must start with http:// or https://")
        return v


class ProviderResponse(ProviderBase):
    id: int
    model_config = ConfigDict(from_attributes=True)
