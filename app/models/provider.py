"""Provider 模型：模型提供商定义，包含多模型条目"""

import enum
import re

from pydantic import (
    BaseModel,
    ConfigDict,
    model_validator,
)
from pydantic import (
    Field as PydanticField,
)
from sqlmodel import (
    JSON,
    Column,
    Field,
    SQLModel,
)


class ModelUsage(enum.StrEnum):
    CHAT = "CHAT"
    EMBEDDING = "EMBEDDING"
    RERANK = "RERANK"


class ProviderType(enum.StrEnum):
    OPENAI = "OPENAI"


class ProviderModelItem(BaseModel):
    """Provider 下单个模型条目的完整配置"""

    model_id: str = PydanticField(..., min_length=1, description="模型唯一标识符")
    usage: ModelUsage = PydanticField(..., description="模型用途：CHAT/EMBEDDING/RERANK")
    image_understanding: bool = PydanticField(False, description="是否支持图像理解")
    audio_understanding: bool = PydanticField(False, description="是否支持音频理解")
    video_understanding: bool = PydanticField(False, description="是否支持视频理解")
    context_window_k: int | None = PydanticField(None, ge=1, description="上下文窗口（K Tokens），CHAT 专属")
    temperature: float | None = PydanticField(None, ge=0, le=2.0, description="采样温度，CHAT 专属")
    top_p: float | None = PydanticField(None, ge=0, le=1.0, description="核采样阈值，CHAT 专属")
    max_tokens: int | None = PydanticField(None, ge=0, description="单次生成最大 Token 数，CHAT 专属")
    embedding_dimensions: int | None = PydanticField(None, gt=0, description="向量输出维度，EMBEDDING 专属")
    description: str | None = PydanticField(None, description="模型描述")


def validate_provider_model_ids(model_ids: list[dict] | None) -> None:
    """校验模型条目列表：每项符合 ProviderModelItem，且同一 usage 下 model_id 不重复。"""
    if not model_ids:
        return

    seen_model_keys: set[tuple[str, str]] = set()
    for i, item in enumerate(model_ids):
        try:
            validated = ProviderModelItem.model_validate(item)
        except Exception as e:
            raise ValueError(f"model_ids[{i}] 校验失败: {e}")

        model_key = (validated.usage.value, validated.model_id)
        if model_key in seen_model_keys:
            raise ValueError(f"同一用途下模型 ID 不能重复: {validated.usage.value}/{validated.model_id}")
        seen_model_keys.add(model_key)


class ProviderBase(SQLModel):
    name: str = Field(index=True, unique=True, nullable=False, min_length=1, max_length=100)
    provider_type: ProviderType = Field(nullable=False)
    api_key: str = Field(nullable=False, min_length=1)
    base_url: str | None = Field(default=None)
    is_active: bool = Field(default=True)
    model_ids: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="模型条目列表，每项符合 ProviderModelItem 结构",
    )

    @model_validator(mode="after")
    def validate_model_ids(self) -> "ProviderBase":
        """校验 model_ids：每一项符合 ProviderModelItem 结构，且同一 usage 下 model_id 不重复"""
        validate_provider_model_ids(self.model_ids)
        return self

    @model_validator(mode="after")
    def validate_base_url(self) -> "ProviderBase":
        """base_url 格式校验；含 RERANK 的 model_ids 中的任一模型要求 base_url 必填"""
        if self.base_url and not re.match(r"^https?://", self.base_url):
            raise ValueError("base_url must start with http:// or https://")
        if self.model_ids:
            has_rerank = any(item.get("usage") == ModelUsage.RERANK for item in self.model_ids)
            if has_rerank and not self.base_url:
                raise ValueError("含 RERANK 用途的模型时 base_url 必须配置")
        return self


class ModelProvider(ProviderBase, table=True):
    __tablename__ = "provider"
    id: int | None = Field(default=None, primary_key=True, index=True)


class ProviderCreate(ProviderBase):
    pass


class ProviderUpdate(SQLModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    provider_type: ProviderType | None = None
    api_key: str | None = Field(None, min_length=1)
    base_url: str | None = None
    is_active: bool | None = None
    model_ids: list[dict] | None = None

    @model_validator(mode="after")
    def validate_model_ids(self) -> "ProviderUpdate":
        """校验部分更新传入的 model_ids，避免绕过 API 层手动校验。"""
        validate_provider_model_ids(self.model_ids)
        return self

    @model_validator(mode="after")
    def validate_base_url(self) -> "ProviderUpdate":
        if self.base_url and not re.match(r"^https?://", self.base_url):
            raise ValueError("base_url must start with http:// or https://")
        return self


class ProviderResponse(ProviderBase):
    id: int
    model_config = ConfigDict(from_attributes=True)
