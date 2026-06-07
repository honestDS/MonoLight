from typing import (
    TYPE_CHECKING,
    Any,
    Optional,
)

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
    Relationship,
    SQLModel,
)

from app.core.utils.config import standardize_config

if TYPE_CHECKING:
    from app.models.prompt import PromptLibrary
    from app.models.provider import ModelProvider


class ProviderConfig(BaseModel):
    """LLM 提供商核心参数配置"""

    provider_id: int | None = PydanticField(None, ge=-1, description="对话模型提供商 ID")
    model_id: str = PydanticField(..., min_length=1, description="模型唯一标识符，如 gpt-4o")
    embedding_provider_id: int | None = PydanticField(None, ge=-1, description="向量模型提供商 ID")
    embedding_model_id: str | None = PydanticField(None, description="用于知识库向量化的专属模型 ID")
    embedding_dimensions: int | None = PydanticField(None, gt=0, description="向量输出维度（如 1024，仅部分模型支持动态维度）")
    temperature: float = PydanticField(0.7, ge=0, le=2.0, description="采样温度，控制生成内容的随机性")
    top_p: float = PydanticField(1.0, ge=0, le=1.0, description="核采样阈值")
    max_tokens: int = PydanticField(2048, ge=0, description="单次生成最大 Token 数量")
    multimodal: bool = PydanticField(False, description="启用多模态支持")
    context_window_k: int = PydanticField(4, ge=1, description="短期上下文关联的历史消息轮数")


class SecurityConfig(BaseModel):
    """安全审计系统参数配置"""

    audit_provider_id: int | None = PydanticField(None, gt=0, description="执行安全审计的提供商 ID")
    audit_model_id: str | None = PydanticField(None, description="用于审计的具体模型 ID")
    audit_threshold: int = PydanticField(5, ge=0, le=7, description="触发审计拦截的风险评分阈值（0-7）")


class ToolConfig(BaseModel):
    """Agent 工具调用参数配置"""

    shell_timeout: float = PydanticField(30.0, gt=0, description="Shell 指令执行的超时时间（秒）")
    max_parallel_tools: int = PydanticField(5, ge=1, le=20, description="允许的最大并行工具调用数量")
    max_turns: int = PydanticField(5, ge=1, le=20, description="允许的最大连续工具调用轮数")
    firecrawl_api_key: str | None = PydanticField(None, description="Firecrawl API Key")


class OtherConfig(BaseModel):
    """杂项系统参数配置"""

    pass


class ProfileConfig(BaseModel):
    """Profile 详细配置模型，负责数据的标准化与校验"""

    provider: ProviderConfig
    security: SecurityConfig
    tool: ToolConfig
    other: OtherConfig

    @model_validator(mode="before")
    @classmethod
    def data_pump(cls, data: Any) -> Any:
        """数据泵：将扁平或非标准的字典数据映射到结构化的嵌套配置模型中"""
        schema_map = {
            "provider": [
                "provider_id",
                "model_id",
                "embedding_provider_id",
                "embedding_model_id",
                "embedding_dimensions",
                "temperature",
                "top_p",
                "max_tokens",
                "multimodal",
                "context_window_k",
            ],
            "security": ["audit_provider_id", "audit_model_id", "audit_threshold"],
            "tool": ["shell_timeout", "max_parallel_tools", "max_turns", "firecrawl_api_key"],
            "other": [],
        }
        return standardize_config(data, schema_map)


# 共享的 OpenAPI 示例
PROFILE_EXAMPLE = {
    "name": "test2",
    "prompt_id": 1,
    "configs": {
        "provider": {
            "provider_id": 1,
            "model_id": "gemini-3-flash-preview",
            "embedding_provider_id": 1,
            "embedding_model_id": "text-embedding-3-small",
            "embedding_dimensions": 1024,
            "temperature": 0.7,
            "top_p": 1,
            "max_tokens": 0,
            "multimodal": False,
            "context_window_k": 1024,
        },
        "security": {
            "audit_provider_id": 1,
            "audit_model_id": "gemini-3-flash-preview",
            "audit_threshold": 5,
        },
        "tool": {
            "shell_timeout": 30,
            "max_parallel_tools": 5,
            "max_turns": 5,
            "firecrawl_api_key": "",
        },
        "other": {},
    },
}


class ProfileBase(SQLModel):
    """Profile 基础模型，包含 OpenAPI 示例文档"""

    name: str = Field(index=True, unique=True, nullable=False, min_length=1, max_length=100)
    prompt_id: int | None = Field(default=None, foreign_key="prompt.id", gt=0)
    configs: dict[str, Any] = Field(
        default={},
        sa_column=Column(JSON),
        description="Profile 详细配置对象",
    )

    model_config = ConfigDict(json_schema_extra={"example": PROFILE_EXAMPLE})


class Profile(ProfileBase, table=True):
    """Profile 数据库实体模型"""

    __tablename__ = "profile"
    id: int | None = Field(default=None, primary_key=True, index=True)
    is_active: bool = Field(default=False)
    prompt: Optional["PromptLibrary"] = Relationship(back_populates="profiles")


class ProfileCreate(ProfileBase):
    """Profile 创建模型"""

    pass


class ProfileUpdate(SQLModel):
    """Profile 更新模型"""

    name: str | None = Field(None, min_length=1, max_length=100)
    prompt_id: int | None = Field(None, gt=0)
    configs: dict[str, Any] | None = None

    model_config = ConfigDict(json_schema_extra={"example": PROFILE_EXAMPLE})


class ProfileResponse(ProfileBase):
    """Profile 响应模型"""

    id: int
    is_active: bool
    provider_name: str | None = None
    model_config = ConfigDict(from_attributes=True)
