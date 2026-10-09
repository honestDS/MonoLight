from typing import Annotated

from pydantic import (
    BaseModel,
    Field,
    WithJsonSchema,
    field_validator,
    model_validator,
)

from app.core.constants import ERR_CHANNEL_MODEL_IDS_DUPLICATED
from app.core.i18n import t
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
    ReasoningEffort,
    validate_channel_api_key,
)
from app.models.user import UserCreate


class SetupAdminInput(UserCreate):
    """初始化管理员输入。"""


class SetupModelInput(BaseModel):
    """初始化聊天模型输入。"""

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
    context_window_k: int = Field(..., ge=1, description="上下文窗口大小（K）")
    temperature: float | None = Field(None, ge=0, le=2, description="采样温度")
    top_p: float | None = Field(None, ge=0, le=1, description="核采样概率")
    reasoning_effort: str | None = Field(None, min_length=1, max_length=64, description="思考等级")
    reasoning_efforts: list[ReasoningEffort] = Field(
        default_factory=list,
        description="思考等级候选数据，非默认值",
    )
    max_tokens: int | None = Field(None, ge=0, description="最大生成 Token 数")
    description: str | None = Field(None, description="模型描述")
    advanced_settings: ChannelModelAdvancedSettings = Field(
        default_factory=ChannelModelAdvancedSettings,
        description="模型高级设置",
    )

    @model_validator(mode="after")
    def validate_chat_model_fields(self) -> "SetupModelInput":
        self.model_id, self.protocol = validate_chat_model(self.model_id, self.protocol)
        return self


class SetupChannelInput(BaseModel):
    """初始化聊天渠道输入。"""

    name: str = Field(..., min_length=1, max_length=100, description="渠道名称")
    base_url: str = Field(..., max_length=2048, description="渠道 API 基础地址")
    api_key: str = Field(..., min_length=1, description="渠道 API 密钥")
    http_proxy: str | None = Field(None, description="渠道 HTTP 代理地址")
    model_ids: list[SetupModelInput] = Field(..., min_length=1, description="聊天模型配置")

    @model_validator(mode="before")
    @classmethod
    def normalize_legacy_model_input(cls, value: object) -> object:
        if not isinstance(value, dict) or "model_ids" in value:
            return value

        model_input = {field_name: value[field_name] for field_name in SetupModelInput.model_fields if field_name in value}
        return {**value, "model_ids": [model_input]}

    @field_validator("base_url")
    @classmethod
    def validate_base_url_field(cls, value: str) -> str:
        return validate_base_url(value, model_ids=[{"model_id": "setup"}])

    @field_validator("api_key")
    @classmethod
    def validate_api_key_field(cls, value: str) -> str:
        return validate_channel_api_key(value)

    @field_validator("http_proxy")
    @classmethod
    def normalize_http_proxy_field(cls, value: str | None) -> str | None:
        return normalize_http_proxy(value)

    @model_validator(mode="after")
    def validate_unique_model_ids(self) -> "SetupChannelInput":
        model_ids: set[str] = set()
        for model in self.model_ids:
            if model.model_id in model_ids:
                raise ValueError(
                    t(
                        ERR_CHANNEL_MODEL_IDS_DUPLICATED,
                        usage=ModelUsage.CHAT.value,
                        model_id=model.model_id,
                    )
                )
            model_ids.add(model.model_id)
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
