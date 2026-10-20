from datetime import datetime
from enum import StrEnum

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, Text
from sqlmodel import Column, DateTime, Field, SQLModel, UniqueConstraint

from app.core.utils.time import get_local_time


class KnowledgeBaseType(StrEnum):
    USER = "user"
    LLM_MANAGED = "llm_managed"


class KnowledgeBaseMigrationStatus(StrEnum):
    PREPARING = "preparing"
    BUILDING = "building"
    CATCHING_UP = "catching_up"
    VALIDATING = "validating"
    SWITCHING = "switching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class KnowledgeBaseOldCollectionCleanupStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class KnowledgeBaseIndexStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    REINDEXING = "reindexing"
    FAILED = "failed"


class KnowledgeBaseCore(SQLModel):
    uid: str = Field(index=True, nullable=False, max_length=50, description="知识库所属用户ID")
    name: str = Field(index=True, nullable=False, min_length=1, max_length=100, description="知识库名称")
    description: str | None = Field(default=None, max_length=500, description="知识库描述")
    embedding_channel_id: int = Field(
        nullable=False,
        index=True,
        foreign_key="channel.id",
        ondelete="RESTRICT",
        description="向量化使用的渠道ID",
    )
    embedding_model_id: str = Field(nullable=False, max_length=255, description="向量化使用的模型ID")
    embedding_dimensions: int | None = Field(default=None, gt=0, description="向量输出维度")
    collection_name: str = Field(unique=True, index=True, nullable=False, max_length=100, description="ChromaDB 中的 collection 名称")
    knowledge_base_type: KnowledgeBaseType = Field(
        default=KnowledgeBaseType.USER,
        nullable=False,
        index=True,
        max_length=20,
        description="知识库类型",
    )
    managed_profile_id: int | None = Field(
        default=None,
        description="LLM 托管知识库所属 Profile",
    )

    active_embedding_channel_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="channel.id",
        ondelete="RESTRICT",
        description="当前生效的向量化渠道ID",
    )
    active_embedding_model_id: str | None = Field(default=None, index=True, max_length=255, description="当前生效的向量化模型ID")
    active_embedding_dimensions: int | None = Field(default=None, ge=1, description="当前生效的向量输出维度")
    active_embedding_signature: str | None = Field(default=None, index=True, max_length=128, description="当前生效的向量化配置签名")
    active_embedding_revision: int = Field(default=0, ge=0, index=True, description="当前生效的向量化配置版本")
    active_collection_name: str | None = Field(default=None, index=True, unique=True, max_length=255, description="当前生效的 collection 名称")

    target_embedding_channel_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="channel.id",
        ondelete="RESTRICT",
        description="目标向量化渠道ID",
    )
    target_embedding_model_id: str | None = Field(default=None, index=True, max_length=255, description="目标向量化模型ID")
    target_embedding_dimensions: int | None = Field(default=None, ge=1, description="目标向量输出维度")
    target_embedding_signature: str | None = Field(default=None, index=True, max_length=128, description="目标向量化配置签名")
    target_embedding_revision: int | None = Field(default=None, ge=1, index=True, description="目标向量化配置版本")
    target_collection_name: str | None = Field(default=None, index=True, unique=True, max_length=255, description="目标 collection 名称")

    migration_job_id: int | None = Field(default=None, index=True, description="迁移任务ID")
    migration_status: KnowledgeBaseMigrationStatus | None = Field(default=None, index=True, max_length=20, description="迁移状态")
    migration_snapshot_boundary: int | None = Field(default=None, ge=0, description="迁移快照边界")
    migration_cursor: int | None = Field(default=None, ge=0, description="迁移游标")
    migration_total_count: int = Field(default=0, ge=0, description="迁移总数量")
    migration_success_count: int = Field(default=0, ge=0, description="迁移成功数量")
    migration_failure_count: int = Field(default=0, ge=0, description="迁移失败数量")
    migration_delta_high_watermark: int = Field(default=0, ge=0, description="迁移增量高水位")
    migration_delta_applied_watermark: int = Field(default=0, ge=0, description="迁移增量已应用水位")
    migration_error: str | None = Field(default=None, sa_column=Column(Text), description="迁移错误信息")
    migration_started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)), description="迁移开始时间")
    migration_finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)), description="迁移完成时间")

    old_collection_name: str | None = Field(default=None, index=True, unique=True, max_length=255, description="旧 collection 名称")
    old_collection_cleanup_status: KnowledgeBaseOldCollectionCleanupStatus = Field(
        default=KnowledgeBaseOldCollectionCleanupStatus.NONE,
        index=True,
        max_length=20,
        description="旧 collection 清理状态",
    )
    old_collection_cleanup_job_id: int | None = Field(default=None, index=True, description="旧 collection 清理任务ID")
    old_collection_cleanup_error: str | None = Field(default=None, sa_column=Column(Text), description="旧 collection 清理错误信息")
    old_collection_cleanup_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)), description="旧 collection 清理时间")

    organization_last_job_id: int | None = Field(default=None, index=True, description="最近一次知识整理任务ID")
    organization_last_run_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)), description="最近一次知识整理运行时间")
    organization_error: str | None = Field(default=None, sa_column=Column(Text), description="知识整理错误信息")

    index_revision: int = Field(default=0, ge=0, index=True, description="索引版本")
    index_status: KnowledgeBaseIndexStatus = Field(
        default=KnowledgeBaseIndexStatus.PENDING,
        index=True,
        max_length=20,
        description="索引状态",
    )


class KnowledgeBase(KnowledgeBaseCore, table=True):
    __tablename__ = "knowledge_base"
    __table_args__ = (
        UniqueConstraint("managed_profile_id", name="uq_knowledge_base_managed_profile"),
        UniqueConstraint("id", "uid", name="uq_knowledge_base_id_uid"),
        ForeignKeyConstraint(
            ["managed_profile_id", "uid"],
            ["profile.id", "profile.uid"],
            name="fk_kb_managed_profile",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(knowledge_base_type = 'USER' AND managed_profile_id IS NULL) OR (knowledge_base_type = 'LLM_MANAGED' AND managed_profile_id IS NOT NULL)",
            name="ck_knowledge_base_type_profile_owner",
        ),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    created_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True)),
    )
    updated_at: datetime | None = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), onupdate=get_local_time),
    )


class KnowledgeBaseProfileBinding(SQLModel, table=True):
    """知识库与 Profile 的绑定关系。"""

    __tablename__ = "knowledge_base_profile_binding"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "profile_id", name="uq_knowledge_base_profile_binding_pair"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_kb_profile_binding_kb_owner",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["profile_id", "uid"],
            ["profile.id", "profile.uid"],
            name="fk_kb_profile_binding_profile_owner",
            ondelete="CASCADE",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    knowledge_base_id: int = Field(nullable=False, index=True, description="知识库ID")
    profile_id: int = Field(nullable=False, index=True, description="配置文件ID")
    uid: str = Field(nullable=False, index=True, max_length=50, description="所属用户ID")
