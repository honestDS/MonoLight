"""Profile 配置模型：含渠道管理的 Profile"""

import os
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
from app.models.channel import ChannelConfig

if TYPE_CHECKING:
    from app.models.prompt import PromptLibrary


class ChannelGroupConfig(BaseModel):
    """渠道管理组配置（渠道管理架构）"""

    chat_channel: ChannelConfig | None = PydanticField(None, description="对话渠道配置")
    embedding_channel: ChannelConfig | None = PydanticField(None, description="嵌入渠道配置")
    rerank_channel: ChannelConfig | None = PydanticField(None, description="重排渠道配置")


class SecurityConfig(BaseModel):
    """安全审计系统参数配置"""

    audit_channel_id: int | None = PydanticField(None, gt=0, description="执行安全审计的渠道 ID")
    audit_model_id: str | None = PydanticField(None, description="用于审计的具体模型 ID")
    audit_threshold: int = PydanticField(5, ge=0, le=7, description="触发审计拦截的风险评分阈值（0-7）")


class ToolConfig(BaseModel):
    """Agent 工具调用参数配置"""

    shell_timeout: float = PydanticField(30.0, gt=0, description="Shell 指令执行的超时时间（秒）")
    max_parallel_tools: int = PydanticField(5, ge=1, le=20, description="允许的最大并行工具调用数量")
    max_turns: int = PydanticField(5, ge=1, le=20, description="允许的最大连续工具调用轮数")
    firecrawl_api_key: str | None = PydanticField(None, description="Firecrawl API Key")
    executor_max_workers: int = PydanticField(
        default_factory=lambda: (os.cpu_count() or 1) * 5,
        ge=1,
        le=100,
        description="工具执行器线程池最大线程数",
    )


class OtherConfig(BaseModel):
    """杂项系统参数配置"""

    pass


class ProfileConfig(BaseModel):
    """Profile 详细配置模型，负责数据的标准化与校验"""

    channel: ChannelGroupConfig
    security: SecurityConfig
    tool: ToolConfig
    other: OtherConfig

    @model_validator(mode="before")
    @classmethod
    def data_pump(cls, data: Any) -> Any:
        """数据泵：将扁平或非标准的字典数据映射到结构化的嵌套配置模型中"""
        schema_map = {
            "channel": [
                "chat_channel",
                "embedding_channel",
                "rerank_channel",
            ],
            "security": ["audit_channel_id", "audit_model_id", "audit_threshold"],
            "tool": [
                "shell_timeout",
                "max_parallel_tools",
                "max_turns",
                "firecrawl_api_key",
                "executor_max_workers",
            ],
            "other": [],
        }
        return standardize_config(data, schema_map)


# 共享的 OpenAPI 示例
PROFILE_EXAMPLE = {
    "name": "test2",
    "prompt_id": 1,
    "configs": {
        "channel": {
            "chat_channel": {
                "chat_timeout": 60.0,
                "rules": [
                    {"channel_id": 1, "model_id": "gpt-4o", "priority": 1, "weight": 100},
                ],
            },
            "embedding_channel": {
                "embedding_timeout": 30.0,
                "rules": [
                    {"channel_id": 1, "model_id": "text-embedding-3-small", "priority": 1, "weight": 100},
                ],
            },
            "rerank_channel": {
                "rerank_timeout": 15.0,
                "rerank_candidate_k": 20,
                "kb_query_top_k": 5,
                "rules": [
                    {"channel_id": 1, "model_id": "bge-reranker-large", "priority": 1, "weight": 100},
                ],
            },
        },
        "security": {
            "audit_channel_id": 1,
            "audit_model_id": "gemini-3-flash-preview",
            "audit_threshold": 5,
        },
        "tool": {
            "shell_timeout": 30,
            "max_parallel_tools": 5,
            "max_turns": 5,
            "firecrawl_api_key": "",
            "executor_max_workers": 10,
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
    model_config = ConfigDict(from_attributes=True)
