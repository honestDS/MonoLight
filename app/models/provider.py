import enum
import re

from pydantic import (
    ConfigDict,
    model_validator,
)
from sqlmodel import (
    Field,
    SQLModel,
)


class ModelUsage(enum.StrEnum):
    CHAT = "CHAT"
    EMBEDDING = "EMBEDDING"
    RERANK = "RERANK"


class ProviderType(enum.StrEnum):
    OPENAI = "OPENAI"
    # GEMINI = "GEMINI"


class ProviderBase(SQLModel):
    name: str = Field(index=True, unique=True, nullable=False, min_length=1, max_length=100)
    provider_type: ProviderType = Field(nullable=False)
    usage: ModelUsage = Field(default=ModelUsage.CHAT, nullable=False)
    api_key: str = Field(nullable=False, min_length=1)
    base_url: str | None = Field(default=None)
    is_active: bool = Field(default=True)

    @model_validator(mode="after")
    def validate_base_url(self) -> "ProviderBase":
        # 合并 base_url 的格式校验与 RERANK 必填校验：
        # 1. 若填写了 base_url，必须以 http:// 或 https:// 开头；
        # 2. RERANK 类型为远程调用，base_url 必填。
        if self.base_url and not re.match(r"^https?://", self.base_url):
            raise ValueError("base_url must start with http:// or https://")
        if self.usage == ModelUsage.RERANK and not self.base_url:
            raise ValueError("usage 为 RERANK 时 base_url 必须配置")
        return self


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

    @model_validator(mode="after")
    def validate_base_url(self) -> "ProviderUpdate":
        # base_url 格式校验（部分更新模型，RERANK 必填约束在 API 层结合库内既有记录判断）
        if self.base_url and not re.match(r"^https?://", self.base_url):
            raise ValueError("base_url must start with http:// or https://")
        return self


class ProviderResponse(ProviderBase):
    id: int
    model_config = ConfigDict(from_attributes=True)
