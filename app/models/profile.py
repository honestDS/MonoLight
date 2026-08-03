"""Profile 配置模型：含渠道管理的 Profile"""

import copy
import os
from typing import (
    Any,
    Literal,
)

from pydantic import (
    BaseModel,
    ConfigDict,
    StrictInt,
    field_validator,
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

from app.core.constants import (
    ERR_PROFILE_AUDIT_REPORT_LANGUAGE_UNSUPPORTED,
    ERR_PROFILE_MEMORY_CANDIDATE_K_TOO_SMALL,
    ERR_PROFILE_MEMORY_EMBEDDING_SELECTION_INCOMPLETE,
)
from app.core.i18n import t
from app.core.i18n.locale import DEFAULT_LOCALE, SUPPORTED_LOCALES
from app.core.utils.config import standardize_config
from app.models.channel import ChannelConfig


class ChannelGroupConfig(BaseModel):
    """渠道管理组配置（渠道管理架构）"""

    chat_channel: ChannelConfig = PydanticField(default_factory=ChannelConfig, description="对话渠道配置")
    context_summary_channel: ChannelConfig = PydanticField(default_factory=ChannelConfig, description="上下文总结渠道配置")
    rerank_channel: ChannelConfig = PydanticField(default_factory=ChannelConfig, description="重排渠道配置")
    image_generation_channel: ChannelConfig = PydanticField(default_factory=ChannelConfig, description="图像生成渠道配置")

    @model_validator(mode="before")
    @classmethod
    def fallback_context_summary_channel(cls, data: Any) -> Any:
        """旧配置缺少总结渠道时，运行期使用对话渠道的独立副本。"""
        if not isinstance(data, dict) or "context_summary_channel" in data:
            return data

        normalized = dict(data)
        normalized["context_summary_channel"] = copy.deepcopy(data.get("chat_channel", {}))
        return normalized


class SecurityConfig(BaseModel):
    """安全审计系统参数配置"""

    audit_channel_id: int | None = PydanticField(None, gt=0, description="执行安全审计的渠道 ID")
    audit_model_id: str | None = PydanticField(None, description="用于审计的具体模型 ID")
    audit_threshold: int = PydanticField(5, ge=0, le=7, description="触发二次确认的风险评分阈值（1-7），0 表示关闭二次确认")
    audit_confirmation_timeout_seconds: StrictInt = PydanticField(600, ge=1, le=86400, description="审计二次确认卡片有效期（秒）")
    audit_report_language: str = PydanticField(DEFAULT_LOCALE, description="审计报告摘要使用的语言")

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_confirmation_timeout(cls, data: Any) -> Any:
        """兼容旧的分钟配置，规范化为秒配置。"""
        if not isinstance(data, dict) or "audit_confirmation_timeout_seconds" in data or "audit_confirmation_timeout_minutes" not in data:
            return data

        normalized = dict(data)
        legacy_value = data["audit_confirmation_timeout_minutes"]
        if isinstance(legacy_value, int) and not isinstance(legacy_value, bool):
            normalized["audit_confirmation_timeout_seconds"] = legacy_value * 60
        else:
            normalized["audit_confirmation_timeout_seconds"] = legacy_value
        return normalized

    @field_validator("audit_report_language")
    @classmethod
    def validate_audit_report_language(cls, value: str) -> str:
        if value not in SUPPORTED_LOCALES:
            raise ValueError(t(ERR_PROFILE_AUDIT_REPORT_LANGUAGE_UNSUPPORTED, language=value))
        return value


class ToolConfig(BaseModel):
    """Agent 工具调用参数配置"""

    tool_timeout: float = PydanticField(30.0, gt=0, description="工具执行超时时间（秒）")
    image_generation_timeout: float = PydanticField(60.0, gt=0, le=600, description="图像生成工具执行超时时间（秒）")
    max_parallel_tools: int = PydanticField(5, ge=1, le=20, description="允许的最大并行工具调用数量")
    max_turns: int = PydanticField(5, ge=1, le=20, description="允许的最大连续工具调用轮数")
    background_task_max_concurrency: int = PydanticField(2, ge=1, le=20, description="允许的最大后台任务并发数量")
    scheduled_task_max_concurrency: int = PydanticField(4, ge=1, le=20, description="允许的最大计划任务回复并发数量")
    firecrawl_api_key: str | None = PydanticField(None, description="Firecrawl API Key")
    enabled_tools: list[str] = PydanticField(
        default_factory=lambda: [
            "execute_shell",
            "write_file",
            "firecrawl_search",
            "firecrawl_scrape",
            "send_file_to_user",
            "list_background_tasks",
            "cancel_background_task",
            "generate_image",
            "query_knowledge_base",
            "read_multimodal_file",
        ],
        description="允许向 LLM 暴露的工具名称列表",
    )
    allowed_operation_dirs: list[str] = PydanticField(default_factory=list, description="send_file_to_user 和 read_multimodal_file 允许操作文件的安全目录白名单，目录必须使用绝对路径")
    file_send_max_count: int = PydanticField(10, ge=1, le=100, description="send_file_to_user 单次最多允许发送的文件数量")
    file_send_max_single_size_mb: int = PydanticField(50, ge=1, le=1024, description="send_file_to_user 单个文件大小上限（MB）")
    file_send_max_total_size_mb: int = PydanticField(100, ge=1, le=4096, description="send_file_to_user 单次发送总大小上限（MB）")
    file_send_blocked_extensions: list[str] = PydanticField(default_factory=list, description="send_file_to_user 禁止发送的文件后缀列表，例如 .pem、.key")
    executor_max_workers: int = PydanticField(
        default_factory=lambda: (os.cpu_count() or 1) * 5,
        ge=1,
        le=100,
        description="工具执行器线程池最大线程数",
    )


class OtherConfig(BaseModel):
    """杂项系统参数配置"""

    context_summary_threshold_percent: Literal[50, 60, 70, 80, 90] = PydanticField(
        90,
        description="触发上下文摘要的可用输入窗口占用百分比",
    )


class LongTermMemoryConfig(BaseModel):
    """长期记忆检索和嵌入模型配置。"""

    enabled: bool = PydanticField(False, description="是否启用长期记忆")
    embedding_channel_id: int | None = PydanticField(None, gt=0, description="长期记忆嵌入渠道 ID")
    embedding_model_id: str | None = PydanticField(None, min_length=1, description="长期记忆嵌入模型 ID")
    top_k: int = PydanticField(5, ge=1, le=50, description="长期记忆最终返回数量")
    candidate_k: int = PydanticField(10, ge=1, le=100, description="长期记忆候选数量")
    result_max_chars: int = PydanticField(4000, ge=256, le=50000, description="长期记忆结果最大字符数")

    @model_validator(mode="before")
    @classmethod
    def migrate_prefixed_flat_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for field in ("enabled", "embedding_channel_id", "embedding_model_id", "top_k", "candidate_k", "result_max_chars"):
            prefixed_name = f"memory_{field}"
            if field not in normalized and prefixed_name in normalized:
                normalized[field] = normalized[prefixed_name]
        return normalized

    @model_validator(mode="after")
    def validate_embedding_selection_pair(self) -> "LongTermMemoryConfig":
        if (self.embedding_channel_id is None) != (self.embedding_model_id is None):
            raise ValueError(t(ERR_PROFILE_MEMORY_EMBEDDING_SELECTION_INCOMPLETE))
        return self

    @model_validator(mode="after")
    def validate_candidate_k(self) -> "LongTermMemoryConfig":
        if self.candidate_k < self.top_k:
            raise ValueError(t(ERR_PROFILE_MEMORY_CANDIDATE_K_TOO_SMALL))
        return self


class ProfileConfig(BaseModel):
    """Profile 详细配置模型，负责数据的标准化与校验"""

    channel: ChannelGroupConfig
    security: SecurityConfig
    tool: ToolConfig
    other: OtherConfig
    memory: LongTermMemoryConfig = PydanticField(default_factory=LongTermMemoryConfig)

    @model_validator(mode="before")
    @classmethod
    def data_pump(cls, data: Any) -> Any:
        """数据泵：将扁平或非标准的字典数据映射到结构化的嵌套配置模型中"""
        schema_map = {
            "channel": [
                "chat_channel",
                "context_summary_channel",
                "rerank_channel",
                "image_generation_channel",
            ],
            "security": [
                "audit_channel_id",
                "audit_model_id",
                "audit_threshold",
                "audit_confirmation_timeout_seconds",
                "audit_confirmation_timeout_minutes",
                "audit_report_language",
            ],
            "tool": [
                "tool_timeout",
                "image_generation_timeout",
                "max_parallel_tools",
                "max_turns",
                "background_task_max_concurrency",
                "scheduled_task_max_concurrency",
                "firecrawl_api_key",
                "enabled_tools",
                "allowed_operation_dirs",
                "file_send_max_count",
                "file_send_max_single_size_mb",
                "file_send_max_total_size_mb",
                "file_send_blocked_extensions",
                "executor_max_workers",
            ],
            "other": ["context_summary_threshold_percent"],
            "memory": [
                "enabled",
                "embedding_channel_id",
                "embedding_model_id",
                "top_k",
                "candidate_k",
                "result_max_chars",
                "memory_enabled",
                "memory_embedding_channel_id",
                "memory_embedding_model_id",
                "memory_top_k",
                "memory_candidate_k",
                "memory_result_max_chars",
            ],
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
            "context_summary_channel": {
                "rules": [
                    {"channel_id": 1, "model_id": "gpt-4o-mini", "priority": 1, "weight": 100},
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
            "image_generation_channel": {
                "rules": [
                    {"channel_id": 1, "model_id": "gpt-image-1", "priority": 1, "weight": 100},
                ],
            },
        },
        "security": {
            "audit_channel_id": 1,
            "audit_model_id": "gemini-3-flash-preview",
            "audit_threshold": 5,
            "audit_confirmation_timeout_seconds": 600,
            "audit_report_language": "zh",
        },
        "tool": {
            "tool_timeout": 30,
            "image_generation_timeout": 60,
            "max_parallel_tools": 5,
            "max_turns": 5,
            "background_task_max_concurrency": 2,
            "scheduled_task_max_concurrency": 4,
            "firecrawl_api_key": "",
            "enabled_tools": ["execute_shell", "write_file", "firecrawl_search", "firecrawl_scrape", "send_file_to_user", "list_background_tasks", "cancel_background_task", "generate_image", "query_knowledge_base", "read_multimodal_file"],
            "allowed_operation_dirs": [],
            "file_send_max_count": 10,
            "file_send_max_single_size_mb": 50,
            "file_send_max_total_size_mb": 100,
            "file_send_blocked_extensions": [],
            "executor_max_workers": 10,
        },
        "other": {
            "context_summary_threshold_percent": 90,
        },
        "memory": {
            "enabled": False,
            "embedding_channel_id": None,
            "embedding_model_id": None,
            "top_k": 5,
            "candidate_k": 10,
            "result_max_chars": 4000,
        },
    },
}


class ProfileBase(SQLModel):
    """Profile 基础模型，包含 OpenAPI 示例文档"""

    uid: str | None = Field(default=None, index=True, max_length=50)
    name: str = Field(index=True, nullable=False, min_length=1, max_length=100)
    prompt_id: int | None = Field(default=None, gt=0)
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
    is_default: bool = Field(default=False)


class ProfileCreate(ProfileBase):
    """Profile 创建模型"""

    knowledge_base_ids: list[int] | None = None
    confirm_memory_embedding_selection: bool = False
    memory_embedding_selection_signature: str | None = None


class ProfileUpdate(SQLModel):
    """Profile 更新模型"""

    name: str | None = Field(None, min_length=1, max_length=100)
    prompt_id: int | None = Field(None, gt=0)
    configs: dict[str, Any] | None = None
    knowledge_base_ids: list[int] | None = None
    confirm_memory_embedding_selection: bool = False
    memory_embedding_selection_signature: str | None = None

    model_config = ConfigDict(json_schema_extra={"example": PROFILE_EXAMPLE})


class ProfileMemoryEmbeddingPreviewRequest(SQLModel):
    profile_id: int = Field(gt=0)
    embedding_channel_id: int = Field(gt=0)
    embedding_model_id: str = Field(min_length=1, max_length=255)


class ProfileMemoryEmbeddingConfirmRequest(SQLModel):
    profile_id: int = Field(gt=0)
    memory: LongTermMemoryConfig
    embedding_selection_signature: str = Field(min_length=1, max_length=128)


class ProfileMemoryRuntime(BaseModel):
    """长期记忆实际生效的嵌入配置及迁移状态。"""

    enabled: bool = False
    embedding_channel_id: int | None = None
    embedding_model_id: str | None = None
    embedding_dimensions: int | None = None
    embedding_signature: str | None = None
    embedding_revision: int = 0
    active_collection_name: str | None = None
    target_embedding_channel_id: int | None = None
    target_embedding_model_id: str | None = None
    target_embedding_dimensions: int | None = None
    target_embedding_signature: str | None = None
    migration_status: str | None = None
    migration_job_id: int | None = None


class ProfileResponse(ProfileBase):
    """Profile 响应模型"""

    id: int
    is_default: bool
    username: str | None = None
    knowledge_base_ids: list[int] = Field(default_factory=list)
    memory_runtime: ProfileMemoryRuntime = PydanticField(default_factory=ProfileMemoryRuntime)
    model_config = ConfigDict(from_attributes=True)
