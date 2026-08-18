from typing import Annotated

from pydantic import (
    BaseModel,
    Field,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.core.utils.http_proxy import normalize_http_proxy
from app.core.validation import (
    validate_base_url,
    validate_chat_model,
)
from app.models.channel import (
    MODEL_PROTOCOLS_BY_USAGE,
    ChannelModelAdvancedSettings,
    ModelProtocol,
    ModelUsage,
)
from app.models.user import UserCreate


class SetupAdminInput(UserCreate):
    """初始化管理员输入。"""


class SetupChannelInput(BaseModel):
    """初始化聊天渠道输入。"""

    name: str = Field(..., min_length=1, max_length=100, description="渠道名称")
    base_url: str = Field(..., max_length=2048, description="渠道 API 基础地址")
    api_key: str = Field(..., min_length=1, description="渠道 API 密钥")
    http_proxy: str | None = Field(None, description="渠道 HTTP 代理地址")
    model_id: str = Field(..., min_length=1, max_length=255, description="聊天模型标识符")
    protocol: Annotated[
        ModelProtocol,
        WithJsonSchema(
            {
                "type": "string",
                "enum": [protocol.value for protocol in MODEL_PROTOCOLS_BY_USAGE[ModelUsage.CHAT]],
            }
        ),
    ] = Field(..., description="聊天模型调用协议")
    image_understanding: bool = Field(False, description="是否支持图像理解")
    audio_understanding: bool = Field(False, description="是否支持音频理解")
    video_understanding: bool = Field(False, description="是否支持视频理解")
    context_window_k: int | None = Field(None, ge=1, description="上下文窗口大小（K）")
    temperature: float | None = Field(None, ge=0, le=2, description="采样温度")
    top_p: float | None = Field(None, ge=0, le=1, description="核采样概率")
    max_tokens: int | None = Field(None, ge=0, description="最大生成 Token 数")
    description: str | None = Field(None, description="模型描述")
    advanced_settings: ChannelModelAdvancedSettings = Field(
        default_factory=ChannelModelAdvancedSettings,
        description="模型高级设置",
    )

    @field_validator("base_url")
    @classmethod
    def validate_base_url_field(cls, value: str) -> str:
        return validate_base_url(value, model_ids=[{"model_id": "setup"}])

    @field_validator("http_proxy")
    @classmethod
    def normalize_http_proxy_field(cls, value: str | None) -> str | None:
        return normalize_http_proxy(value)

    @model_validator(mode="after")
    def validate_chat_model_fields(self) -> "SetupChannelInput":
        self.model_id, self.protocol = validate_chat_model(self.model_id, self.protocol)
        return self


class SetupProfileInput(BaseModel):
    """初始化 Profile 输入。"""

    name: str = Field(..., min_length=1, max_length=100, description="Profile 名称")


class SetupCompleteRequest(BaseModel):
    """完成初始化所需的管理员、渠道和 Profile 配置。"""

    admin: SetupAdminInput = Field(..., description="管理员配置")
    channel: SetupChannelInput = Field(..., description="聊天渠道配置")
    profile: SetupProfileInput = Field(..., description="Profile 配置")


class SetupStatusData(BaseModel):
    """初始化状态响应数据。"""

    required: bool = Field(..., description="是否需要完成初始化")


class SetupTokenData(BaseModel):
    """初始化完成后返回的认证令牌数据。"""

    access_token: str = Field(..., min_length=1, description="访问令牌")
    token_type: str = Field(..., min_length=1, description="令牌类型")
    profile_id: int = Field(..., gt=0, description="Profile 标识符")
    channel_id: int = Field(..., gt=0, description="渠道标识符")


class SetupCompleteResult(SetupTokenData):
    """初始化完成结果。"""
