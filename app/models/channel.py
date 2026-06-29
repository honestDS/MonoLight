"""渠道模型：渠道实体、渠道规则与渠道配置"""

import enum

from pydantic import (
    BaseModel,
    ConfigDict,
    model_validator,
)
from pydantic import (
    Field as PydanticField,
)
from sqlalchemy import event, inspect
from sqlmodel import (
    JSON,
    Column,
    Field,
    SQLModel,
)

from app.core.crypto import decrypt_api_key, encrypt_api_key

ENCRYPTED_API_KEY_PREFIX = "enc:v1:"


class ModelUsage(enum.StrEnum):
    CHAT = "CHAT"
    EMBEDDING = "EMBEDDING"
    RERANK = "RERANK"
    IMAGE_GENERATION = "IMAGE_GENERATION"


class ImageGenerationSize(enum.StrEnum):
    SIZE_1024X1024 = "1024x1024"
    SIZE_1024X1536 = "1024x1536"
    SIZE_1536X1024 = "1536x1024"


class ImageGenerationQuality(enum.StrEnum):
    AUTO = "auto"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class ChannelType(enum.StrEnum):
    OPENAI = "OPENAI"


class ChannelModelItem(BaseModel):
    """渠道下单个模型条目的完整配置"""

    model_id: str = PydanticField(..., min_length=1, description="模型唯一标识符")
    usage: ModelUsage = PydanticField(..., description="模型用途：CHAT/EMBEDDING/RERANK/IMAGE_GENERATION")
    image_understanding: bool = PydanticField(False, description="是否支持图像理解")
    audio_understanding: bool = PydanticField(False, description="是否支持音频理解")
    video_understanding: bool = PydanticField(False, description="是否支持视频理解")
    context_window_k: int | None = PydanticField(None, ge=1, description="上下文窗口（K Tokens），CHAT 专属")
    temperature: float | None = PydanticField(None, ge=0, le=2.0, description="采样温度，CHAT 专属")
    top_p: float | None = PydanticField(None, ge=0, le=1.0, description="核采样阈值，CHAT 专属")
    max_tokens: int | None = PydanticField(None, ge=0, description="单次生成最大 Token 数，CHAT 专属")
    embedding_dimensions: int | None = PydanticField(None, gt=0, description="向量输出维度，EMBEDDING 专属")
    size: ImageGenerationSize | None = PydanticField(None, description="生成图片尺寸，IMAGE_GENERATION 专属")
    quality: ImageGenerationQuality | None = PydanticField(None, description="生成图片质量，IMAGE_GENERATION 专属")
    embedding_timeout: float | None = PydanticField(None, gt=0, le=600, description="嵌入模型调用超时（秒），EMBEDDING 专属")
    rerank_timeout: float | None = PydanticField(None, gt=0, le=120, description="重排模型调用超时（秒），RERANK 专属")
    is_enabled: bool = PydanticField(True, description="是否启用该模型条目")
    description: str | None = PydanticField(None, description="模型描述")

    @model_validator(mode="after")
    def validate_image_generation_fields(self):
        if self.usage != ModelUsage.IMAGE_GENERATION and (self.size is not None or self.quality is not None):
            raise ValueError("size and quality are only allowed for IMAGE_GENERATION model usage")
        return self


def validate_channel_model_ids(model_ids: list[dict] | None) -> tuple[str | None, dict]:
    """校验模型条目列表：每项符合 ChannelModelItem，且同一 usage 下 model_id 不重复。"""
    if not model_ids:
        return None, {}

    seen_model_keys: set[tuple[str, str]] = set()
    for i, item in enumerate(model_ids):
        try:
            validated = ChannelModelItem.model_validate(item)
        except Exception as e:
            return "ERR_CHANNEL_MODEL_IDS_ITEM_INVALID", {"index": i, "error": str(e)}

        model_key = (validated.usage.value, validated.model_id)
        if model_key in seen_model_keys:
            return "ERR_CHANNEL_MODEL_IDS_DUPLICATED", {"usage": validated.usage.value, "model_id": validated.model_id}
        seen_model_keys.add(model_key)

    return None, {}


class ChannelBase(SQLModel):
    name: str = Field(index=True, unique=True, nullable=False, min_length=1, max_length=100)
    channel_type: ChannelType = Field(nullable=False)
    api_key: str = Field(nullable=False, min_length=1)
    base_url: str | None = Field(default=None)
    is_active: bool = Field(default=True)
    model_ids: list[dict] = Field(
        default_factory=list,
        sa_column=Column(JSON),
        description="模型条目列表，每项符合 ChannelModelItem 结构",
    )


class ModelChannel(ChannelBase, table=True):
    __tablename__ = "channel"
    id: int | None = Field(default=None, primary_key=True, index=True)

    def get_decrypted_api_key(self) -> str:
        """获取解密后的API密钥"""
        return decrypt_api_key(_strip_encrypted_api_key_prefix(self.api_key))

    def set_api_key_plaintext(self, plaintext: str) -> None:
        """设置API密钥（自动加密）"""
        self.api_key = _format_encrypted_api_key(encrypt_api_key(plaintext))


def _format_encrypted_api_key(encrypted_text: str) -> str:
    """为密文添加版本前缀。"""
    return f"{ENCRYPTED_API_KEY_PREFIX}{encrypted_text}"


def _strip_encrypted_api_key_prefix(text: str) -> str:
    """移除密文版本前缀，兼容历史无前缀密文。"""
    if text.startswith(ENCRYPTED_API_KEY_PREFIX):
        return text[len(ENCRYPTED_API_KEY_PREFIX) :]
    return text


def _is_encrypted_api_key(text: str) -> bool:
    """仅通过明确版本前缀判断API密钥是否已加密。"""
    return bool(text and text.startswith(ENCRYPTED_API_KEY_PREFIX))


# SQLAlchemy事件监听器：保存前加密API密钥
@event.listens_for(ModelChannel, "before_insert")
@event.listens_for(ModelChannel, "before_update")
def encrypt_api_key_before_save(mapper, connection, target):
    """在插入或更新前加密API密钥

    优化：仅在api_key字段实际变更时执行加密检测，避免不必要的解密尝试。
    使用SQLAlchemy的get_history()检测字段是否被修改。
    """
    if not target.api_key:
        return

    # 获取api_key字段的变更历史
    state = inspect(target)
    history = state.get_history("api_key", passive=True)

    # 如果字段未变更（update时），直接返回
    if history.unchanged:
        return

    # 字段有变更，检查是否需要加密
    if not _is_encrypted_api_key(target.api_key):
        target.api_key = _format_encrypted_api_key(encrypt_api_key(target.api_key))


class ChannelCreate(ChannelBase):
    pass


class ChannelUpdate(SQLModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    channel_type: ChannelType | None = None
    api_key: str | None = Field(None, min_length=1)
    base_url: str | None = None
    is_active: bool | None = None
    model_ids: list[dict] | None = None


def _safe_decrypt_api_key(api_key: str | None) -> str:
    """解密 API 密钥，解密失败时返回空字符串。"""
    if not api_key:
        return ""

    try:
        return decrypt_api_key(_strip_encrypted_api_key_prefix(api_key))
    except Exception:
        return ""


def _channel_response_data(obj) -> dict:
    """构造渠道响应数据，列表和详情均返回明文 API 密钥。"""
    return {
        "id": obj.id,
        "name": obj.name,
        "channel_type": obj.channel_type,
        "api_key": _safe_decrypt_api_key(getattr(obj, "api_key", None)),
        "base_url": obj.base_url,
        "is_active": obj.is_active,
        "model_ids": obj.model_ids or [],
    }


class ChannelResponse(BaseModel):
    id: int
    name: str
    channel_type: ChannelType
    api_key: str
    base_url: str | None
    is_active: bool
    model_ids: list[dict]
    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def model_validate(cls, obj, **kwargs):
        """构造详情响应，API 密钥解密失败时返回空字符串。"""
        if hasattr(obj, "api_key"):
            return super().model_validate(_channel_response_data(obj), **kwargs)
        return super().model_validate(obj, **kwargs)


class ChannelListResponse(ChannelResponse):
    @classmethod
    def model_validate(cls, obj, **kwargs):
        """构造列表响应，API 密钥解密失败时返回空字符串。"""
        if hasattr(obj, "api_key"):
            return super().model_validate(_channel_response_data(obj), **kwargs)
        return super().model_validate(obj, **kwargs)


class ChannelRule(BaseModel):
    """单条渠道路由规则"""

    channel_id: int = PydanticField(..., gt=0, description="渠道 ID")
    model_id: str = PydanticField(..., min_length=1, description="模型标识符")
    priority: int = PydanticField(..., ge=1, description="优先级分组，越小越优先；同组内失败会降级到下一组")
    weight: int = PydanticField(..., ge=0, description="同优先级组内的轮询配额：一个轮询周期内该渠道被使用的次数")
    is_enabled: bool = PydanticField(True, description="是否启用该路由规则")


class ChannelConfig(BaseModel):
    """渠道配置（按用途独立）"""

    retry_on_failure: bool = PydanticField(True, description="渠道调用失败后是否降级重试")
    chat_timeout: float = PydanticField(60.0, gt=0, le=600, description="对话渠道调用超时（秒）")
    rerank_timeout: float = PydanticField(15.0, gt=0, le=120, description="重排渠道调用超时（秒）")
    rerank_candidate_k: int = PydanticField(20, gt=0, le=50, description="送入远程 reranker 的候选数量")
    kb_query_top_k: int = PydanticField(5, gt=0, le=50, description="知识库检索最终返回的片段数量")
    rules: list[ChannelRule] = PydanticField(default_factory=list, description="路由规则列表")
