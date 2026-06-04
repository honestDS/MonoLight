"""
Embedding 配置模型

定义 Embedding 模块的配置结构，支持从环境变量加载。
"""


from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings


class EmbeddingConfig(BaseModel):
    """Embedding 配置模型"""

    provider_type: str = Field(
        default="openai",
        description="Embedding 提供商类型：openai, local 等",
    )
    model_id: str = Field(
        default="text-embedding-3-small",
        description="模型标识符",
    )
    api_key: str | None = Field(
        default=None,
        description="API 密钥（线上服务需要）",
    )
    base_url: str | None = Field(
        default=None,
        description="API 基础 URL",
    )
    batch_size: int = Field(
        default=100,
        ge=1,
        le=2048,
        description="批处理大小",
    )
    dimensions: int | None = Field(
        default=None,
        ge=1,
        description="向量维度（某些模型支持自定义维度）",
    )
    timeout: int = Field(
        default=30,
        ge=1,
        description="API 请求超时时间（秒）",
    )
    model_cache_dir: str | None = Field(
        default=None,
        description="本地模型缓存目录",
    )

    class Config:
        json_schema_extra = {
            "example": {
                "provider_type": "openai",
                "model_id": "text-embedding-3-small",
                "api_key": "sk-xxx",
                "base_url": "https://api.openai.com/v1",
                "batch_size": 100,
                "dimensions": 1536,
                "timeout": 30,
            }
        }


class EmbeddingSettings(BaseSettings):
    """从环境变量加载 Embedding 配置"""

    embedding_provider: str = Field(default="openai", alias="EMBEDDING_PROVIDER")
    embedding_model_id: str = Field(
        default="text-embedding-3-small", alias="EMBEDDING_MODEL_ID"
    )
    embedding_api_key: str | None = Field(default=None, alias="EMBEDDING_API_KEY")
    embedding_base_url: str | None = Field(
        default=None, alias="EMBEDDING_BASE_URL"
    )
    embedding_batch_size: int = Field(default=100, alias="EMBEDDING_BATCH_SIZE")
    embedding_dimensions: int | None = Field(
        default=None, alias="EMBEDDING_DIMENSIONS"
    )
    embedding_timeout: int = Field(default=30, alias="EMBEDDING_TIMEOUT")
    embedding_model_cache_dir: str | None = Field(
        default=None, alias="EMBEDDING_MODEL_CACHE_DIR"
    )

    class Config:
        env_file = ".env"
        case_sensitive = False


def load_embedding_config() -> EmbeddingConfig:
    """从环境变量加载配置并转换为 EmbeddingConfig"""
    settings = EmbeddingSettings()
    return EmbeddingConfig(
        provider_type=settings.embedding_provider,
        model_id=settings.embedding_model_id,
        api_key=settings.embedding_api_key,
        base_url=settings.embedding_base_url,
        batch_size=settings.embedding_batch_size,
        dimensions=settings.embedding_dimensions,
        timeout=settings.embedding_timeout,
        model_cache_dir=settings.embedding_model_cache_dir,
    )
