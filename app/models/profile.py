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
    StrictBool,
    StrictInt,
    StrictStr,
    ValidationInfo,
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
    UniqueConstraint,
)

from app.core.constants import (
    AUDIT_DEFAULT_DEPTH,
    AUDIT_MAX_DEPTH,
    ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID,
    ERR_PROFILE_AUDIT_REPORT_LANGUAGE_UNSUPPORTED,
    ERR_PROFILE_CHAT_HISTORY_CANDIDATE_K_TOO_SMALL,
    ERR_PROFILE_KNOWLEDGE_CANDIDATE_K_TOO_SMALL,
    ERR_PROFILE_MEMORY_CANDIDATE_K_TOO_SMALL,
    ERR_PROFILE_MEMORY_EMBEDDING_SELECTION_INCOMPLETE,
    FILE_TOOL_NAME,
    IMAGE_GENERATION_TOOL_NAME,
    MANAGE_BACKGROUND_TASKS_TOOL_NAME,
    MANAGE_SCHEDULED_TASKS_TOOL_NAME,
    READ_MULTIMODAL_FILE_TOOL_NAME,
    SEND_FILE_TO_USER_TOOL_NAME,
    SHELL_TOOL_NAME,
    WEB_TOOL_NAME,
)
from app.core.i18n import t
from app.core.i18n.locale import DEFAULT_LOCALE, SUPPORTED_LOCALES
from app.core.utils.config import standardize_config
from app.models.channel import ChannelConfig


class ChannelGroupConfig(BaseModel):
    """渠道管理组配置（渠道管理架构）"""

    chat_channel: ChannelConfig = PydanticField(default_factory=ChannelConfig, description="对话渠道配置")
    context_summary_channel: ChannelConfig = PydanticField(default_factory=ChannelConfig, description="上下文总结渠道配置")
    auxiliary_channel: ChannelConfig = PydanticField(default_factory=ChannelConfig, description="标题生成及前置召回预检辅助渠道配置")
    rerank_channel: ChannelConfig = PydanticField(default_factory=ChannelConfig, description="重排渠道配置")
    image_generation_channel: ChannelConfig = PydanticField(default_factory=ChannelConfig, description="图像生成渠道配置")

    def resolve_auxiliary_channel(self) -> ChannelConfig:
        """辅助渠道未配置规则时动态回退到对话渠道。"""
        return self.auxiliary_channel if self.auxiliary_channel.rules else self.chat_channel

    @model_validator(mode="before")
    @classmethod
    def fallback_context_summary_channel(cls, data: Any, info: ValidationInfo) -> Any:
        """旧配置缺少总结渠道时，运行期使用对话渠道的独立副本。"""
        if (info.context or {}).get("partial_update") or not isinstance(data, dict) or "context_summary_channel" in data:
            return data

        normalized = dict(data)
        normalized["context_summary_channel"] = copy.deepcopy(data.get("chat_channel", {}))
        return normalized


class SecurityConfig(BaseModel):
    """安全审计系统参数配置"""

    audit_channel_id: int | None = PydanticField(None, gt=0, description="执行安全审计的渠道 ID")
    audit_model_id: str | None = PydanticField(None, description="用于审计的具体模型 ID")
    audit_threshold: int = PydanticField(5, ge=0, le=7, description="触发二次确认的风险评分阈值（1-7），0 表示关闭二次确认")
    audit_depth: StrictInt = PydanticField(AUDIT_DEFAULT_DEPTH, ge=1, le=AUDIT_MAX_DEPTH, description="审计深度：单次审计、格式纠正重试与确认摘要共用的文件读取和 Shell 取证调用总次数，失败和重复调用也计数，默认 3")
    audit_confirmation_timeout_seconds: StrictInt = PydanticField(600, ge=1, le=86400, description="审计二次确认卡片有效期（秒）")
    audit_report_language: str = PydanticField(DEFAULT_LOCALE, description="审计报告摘要使用的语言")
    protect_system_directories: StrictBool = PydanticField(True, description="是否启用系统目录写保护")

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
    background_task_max_concurrency: int = PydanticField(2, ge=1, le=20, description="允许的最大后台任务并发数量")
    scheduled_task_max_concurrency: int = PydanticField(4, ge=1, le=20, description="允许的最大计划任务回复并发数量")
    firecrawl_api_key: str | None = PydanticField(None, description="Firecrawl API Key")
    enabled_tools: list[str] = PydanticField(
        default_factory=lambda: [
            SHELL_TOOL_NAME,
            FILE_TOOL_NAME,
            WEB_TOOL_NAME,
            SEND_FILE_TO_USER_TOOL_NAME,
            MANAGE_BACKGROUND_TASKS_TOOL_NAME,
            MANAGE_SCHEDULED_TASKS_TOOL_NAME,
            IMAGE_GENERATION_TOOL_NAME,
            READ_MULTIMODAL_FILE_TOOL_NAME,
        ],
        description="允许向 LLM 暴露的工具名称列表",
    )
    allowed_operation_dirs: list[str] = PydanticField(default_factory=list, description=f"{FILE_TOOL_NAME}、{SEND_FILE_TO_USER_TOOL_NAME} 和 {READ_MULTIMODAL_FILE_TOOL_NAME} 允许操作文件的安全目录白名单，目录必须使用绝对路径")
    file_send_max_count: int = PydanticField(10, ge=1, le=100, description=f"{SEND_FILE_TO_USER_TOOL_NAME} 单次最多允许发送的文件数量")
    file_send_max_single_size_mb: int = PydanticField(50, ge=1, le=1024, description=f"{SEND_FILE_TO_USER_TOOL_NAME} 单个文件大小上限（MB）")
    file_send_max_total_size_mb: int = PydanticField(100, ge=1, le=4096, description=f"{SEND_FILE_TO_USER_TOOL_NAME} 单次发送总大小上限（MB）")
    file_send_blocked_extensions: list[str] = PydanticField(default_factory=list, description=f"{SEND_FILE_TO_USER_TOOL_NAME} 禁止发送的文件后缀列表，例如 .pem、.key")
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


class ChatHistoryRecallConfig(BaseModel):
    """聊天记录召回参数配置。"""

    top_k: int = PydanticField(5, ge=1, le=50, description="聊天记录最终返回数量")
    candidate_k: int = PydanticField(500, ge=1, le=500, description="聊天记录候选数量")

    @model_validator(mode="after")
    def validate_candidate_k(self, info: ValidationInfo) -> "ChatHistoryRecallConfig":
        if (info.context or {}).get("partial_update"):
            return self
        if self.candidate_k < self.top_k:
            raise ValueError(t(ERR_PROFILE_CHAT_HISTORY_CANDIDATE_K_TOO_SMALL))
        return self


class KnowledgeRecallConfig(BaseModel):
    """知识库召回参数配置。"""

    top_k: int = PydanticField(5, ge=1, le=50, description="知识最终返回数量")
    candidate_k: int = PydanticField(20, ge=1, le=50, description="知识候选数量")

    @model_validator(mode="after")
    def validate_candidate_k(self, info: ValidationInfo) -> "KnowledgeRecallConfig":
        if (info.context or {}).get("partial_update"):
            return self
        if self.candidate_k < self.top_k:
            raise ValueError(t(ERR_PROFILE_KNOWLEDGE_CANDIDATE_K_TOO_SMALL))
        return self


class LongTermMemoryConfig(BaseModel):
    """长期记忆检索和嵌入模型配置。"""

    enabled: bool = PydanticField(False, description="是否启用长期记忆")
    precheck_enabled: bool = PydanticField(True, description="是否在回答前自动查找相关记忆和知识")
    embedding_channel_id: int | None = PydanticField(None, gt=0, description="长期记忆嵌入渠道 ID")
    embedding_model_id: str | None = PydanticField(None, min_length=1, description="长期记忆嵌入模型 ID")
    top_k: int = PydanticField(5, ge=1, le=50, description="长期记忆最终返回数量")
    candidate_k: int = PydanticField(10, ge=1, le=100, description="长期记忆候选数量")
    chat_history: ChatHistoryRecallConfig = PydanticField(default_factory=ChatHistoryRecallConfig, description="聊天记录召回参数")
    knowledge: KnowledgeRecallConfig = PydanticField(default_factory=KnowledgeRecallConfig, description="知识库召回参数")

    @model_validator(mode="before")
    @classmethod
    def migrate_prefixed_flat_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        for field in ("enabled", "precheck_enabled", "embedding_channel_id", "embedding_model_id", "top_k", "candidate_k"):
            prefixed_name = f"memory_{field}"
            if field not in normalized and prefixed_name in normalized:
                normalized[field] = normalized[prefixed_name]
        return normalized

    @model_validator(mode="after")
    def validate_embedding_selection_pair(self, info: ValidationInfo) -> "LongTermMemoryConfig":
        if (info.context or {}).get("partial_update"):
            return self
        if (self.embedding_channel_id is None) != (self.embedding_model_id is None):
            raise ValueError(t(ERR_PROFILE_MEMORY_EMBEDDING_SELECTION_INCOMPLETE))
        return self

    @model_validator(mode="after")
    def validate_candidate_k(self, info: ValidationInfo) -> "LongTermMemoryConfig":
        if (info.context or {}).get("partial_update"):
            return self
        if self.candidate_k < self.top_k:
            raise ValueError(t(ERR_PROFILE_MEMORY_CANDIDATE_K_TOO_SMALL))
        return self


class LongTermMemoryOrganizationConfig(BaseModel):
    """长期记忆自动整理配置。"""

    auto_organize_enabled: StrictBool = PydanticField(False)
    organization_channel_id: StrictInt | None = PydanticField(default=None, gt=0)
    organization_model_id: StrictStr | None = PydanticField(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def validate_organization_selection(self) -> "LongTermMemoryOrganizationConfig":
        if (self.organization_channel_id is None) != (self.organization_model_id is None):
            raise ValueError(t(ERR_MEMORY_ORGANIZATION_MODEL_CONFIG_INVALID))
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
    def data_pump(cls, data: Any, info: ValidationInfo) -> Any:
        """数据泵：将扁平或非标准的字典数据映射到结构化的嵌套配置模型中"""
        schema_map = {
            "channel": [
                "chat_channel",
                "context_summary_channel",
                "auxiliary_channel",
                "rerank_channel",
                "image_generation_channel",
            ],
            "security": [
                "audit_channel_id",
                "audit_model_id",
                "audit_threshold",
                "audit_depth",
                "audit_confirmation_timeout_seconds",
                "audit_confirmation_timeout_minutes",
                "audit_report_language",
                "protect_system_directories",
            ],
            "tool": [
                "tool_timeout",
                "image_generation_timeout",
                "max_parallel_tools",
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
                "precheck_enabled",
                "embedding_channel_id",
                "embedding_model_id",
                "top_k",
                "candidate_k",
                "chat_history",
                "knowledge",
                "memory_enabled",
                "memory_precheck_enabled",
                "memory_embedding_channel_id",
                "memory_embedding_model_id",
                "memory_top_k",
                "memory_candidate_k",
            ],
        }
        normalized = standardize_config(data, schema_map)
        if not isinstance(normalized, dict):
            return normalized

        tool = normalized.get("tool")
        if isinstance(tool, dict) and isinstance(tool.get("enabled_tools"), list):
            enabled_tools = []
            for tool_name in tool["enabled_tools"]:
                if not isinstance(tool_name, str):
                    continue
                if tool_name not in enabled_tools:
                    enabled_tools.append(tool_name)
            tool["enabled_tools"] = enabled_tools

        if (info.context or {}).get("partial_update"):
            memory = normalized["memory"]
            if not isinstance(memory.get("knowledge"), dict):
                rerank_channel = normalized["channel"].get("rerank_channel")
                if isinstance(rerank_channel, dict):
                    knowledge = {}
                    if "kb_query_top_k" in rerank_channel:
                        knowledge["top_k"] = rerank_channel["kb_query_top_k"]
                    if "rerank_candidate_k" in rerank_channel:
                        knowledge["candidate_k"] = rerank_channel["rerank_candidate_k"]
                    if knowledge:
                        memory["knowledge"] = knowledge
            return normalized

        memory = normalized.get("memory")
        if not isinstance(memory, dict):
            memory = {}
            normalized["memory"] = memory
        if not isinstance(memory.get("chat_history"), dict):
            chat_history = ChatHistoryRecallConfig().model_dump()
            if "top_k" in memory:
                chat_history["top_k"] = memory["top_k"]
            elif "memory_top_k" in memory:
                chat_history["top_k"] = memory["memory_top_k"]
            memory["chat_history"] = chat_history
        if not isinstance(memory.get("knowledge"), dict):
            channel = normalized.get("channel")
            rerank_channel = channel.get("rerank_channel") if isinstance(channel, dict) else None
            if not isinstance(rerank_channel, dict):
                rerank_channel = {}
            memory["knowledge"] = {
                "top_k": rerank_channel.get("kb_query_top_k", 5),
                "candidate_k": rerank_channel.get("rerank_candidate_k", 20),
            }
        return normalized


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
            "auxiliary_channel": {
                "rules": [
                    {"channel_id": 1, "model_id": "gpt-4o-mini", "priority": 1, "weight": 100},
                ],
            },
            "rerank_channel": {
                "rerank_timeout": 15.0,
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
            "audit_depth": AUDIT_DEFAULT_DEPTH,
            "audit_confirmation_timeout_seconds": 600,
            "audit_report_language": "zh",
            "protect_system_directories": True,
        },
        "tool": {
            "tool_timeout": 30,
            "image_generation_timeout": 60,
            "max_parallel_tools": 5,
            "background_task_max_concurrency": 2,
            "scheduled_task_max_concurrency": 4,
            "firecrawl_api_key": "",
            "enabled_tools": [SHELL_TOOL_NAME, FILE_TOOL_NAME, WEB_TOOL_NAME, SEND_FILE_TO_USER_TOOL_NAME, MANAGE_BACKGROUND_TASKS_TOOL_NAME, MANAGE_SCHEDULED_TASKS_TOOL_NAME, IMAGE_GENERATION_TOOL_NAME, READ_MULTIMODAL_FILE_TOOL_NAME],
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
            "chat_history": {
                "top_k": 5,
                "candidate_k": 500,
            },
            "knowledge": {
                "top_k": 5,
                "candidate_k": 20,
            },
        },
    },
}


class ProfileBase(SQLModel):
    """Profile 基础模型，包含 OpenAPI 示例文档"""

    uid: str | None = Field(default=None, index=True, max_length=50)
    name: str = Field(index=True, nullable=False, min_length=1, max_length=100)
    prompt_id: int | None = Field(default=None, gt=0, foreign_key="prompt.id", ondelete="RESTRICT")
    configs: dict[str, Any] = Field(
        default={},
        sa_column=Column(JSON),
        description="Profile 详细配置对象",
    )

    model_config = ConfigDict(json_schema_extra={"example": PROFILE_EXAMPLE})


class Profile(ProfileBase, table=True):
    """Profile 数据库实体模型"""

    __tablename__ = "profile"
    __table_args__ = (UniqueConstraint("id", "uid", name="uq_profile_id_uid"),)
    id: int | None = Field(default=None, primary_key=True, index=True)
    is_default: bool = Field(default=False)


class ProfileCreate(ProfileBase):
    """Profile 创建模型"""

    knowledge_base_ids: list[int] | None = None
    confirm_memory_embedding_selection: bool = False
    memory_embedding_selection_signature: str | None = None
    memory_organization: LongTermMemoryOrganizationConfig | None = None

    @field_validator("configs")
    @classmethod
    def validate_configs(cls, value: dict[str, Any]) -> dict[str, Any]:
        return ProfileConfig.model_validate(value).model_dump(exclude_unset=True)


class ProfileUpdate(SQLModel):
    """Profile 更新模型"""

    name: str | None = Field(None, min_length=1, max_length=100)
    prompt_id: int | None = Field(None, gt=0)
    configs: dict[str, Any] | None = None
    knowledge_base_ids: list[int] | None = None
    confirm_memory_embedding_selection: bool = False
    memory_embedding_selection_signature: str | None = None
    memory_organization: LongTermMemoryOrganizationConfig | None = None

    @field_validator("configs")
    @classmethod
    def validate_configs(cls, value: dict[str, Any] | None) -> dict[str, Any] | None:
        if value is None:
            return None
        return ProfileConfig.model_validate(
            copy.deepcopy(value),
            context={"partial_update": True},
        ).model_dump(exclude_unset=True)

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
    memory_organization: LongTermMemoryOrganizationConfig = PydanticField(default_factory=LongTermMemoryOrganizationConfig)
    model_config = ConfigDict(from_attributes=True)
