from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlmodel import (
    JSON,
    Column,
    DateTime,
    Field,
    Index,
    SQLModel,
    UniqueConstraint,
)

from app.core.utils.time import get_local_time


class ManagedKnowledgeSourceType(StrEnum):
    LLM_TOOL = "llm_tool"
    USER_API = "user_api"
    AUTO_ORGANIZE = "auto_organize"
    SYSTEM = "system"


class ManagedKnowledgeActorType(StrEnum):
    LLM = "llm"
    USER = "user"
    SYSTEM = "system"


class ManagedKnowledgeRevisionOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    DELETE = "delete"


class ManagedKnowledgeItem(SQLModel, table=True):
    __tablename__ = "managed_knowledge_item"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "knowledge_key", name="uq_managed_knowledge_item_kb_key"),
        UniqueConstraint("knowledge_base_id", "content_hash", name="uq_managed_knowledge_item_kb_content_hash"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_managed_knowledge_item_kb_owner",
            ondelete="CASCADE",
        ),
        Index("ix_managed_knowledge_item_kb_recallable", "knowledge_base_id", "is_recallable", "deleted_at"),
        Index("ix_managed_knowledge_item_kb_updated", "knowledge_base_id", "updated_at", "id"),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True, index=True, description="稳定的托管知识标识")
    knowledge_base_id: int = Field(nullable=False, index=True, description="所属托管知识库")
    uid: str = Field(nullable=False, index=True, max_length=50, description="所属用户")
    knowledge_key: str | None = Field(default=None, nullable=True, index=True, max_length=255, description="稳定知识键")
    content: str = Field(
        sa_column=Column(Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
        description="完整知识正文，不保存截断内容",
    )
    content_token_count: int = Field(default=0, ge=0, nullable=False, description="完整正文 Token 数")
    content_hash: str | None = Field(default=None, nullable=True, index=True, max_length=64, description="完整正文的稳定 SHA-256 摘要")
    version: int = Field(default=1, ge=1, index=True, nullable=False, description="当前知识版本")
    source_type: ManagedKnowledgeSourceType = Field(default=ManagedKnowledgeSourceType.USER_API, index=True, max_length=30)
    source_reference: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON), description="当前版本来源引用")
    source_job_id: int | None = Field(default=None, index=True, description="产生当前版本的知识作业；步骤 4 接入")
    created_by: ManagedKnowledgeActorType = Field(default=ManagedKnowledgeActorType.USER, index=True, max_length=20)
    last_modified_by: ManagedKnowledgeActorType = Field(default=ManagedKnowledgeActorType.USER, index=True, max_length=20)
    llm_maintainable: bool = Field(default=False, index=True, nullable=False, description="是否允许 LLM 后续维护")
    indexed_version: int = Field(default=0, ge=0, index=True, nullable=False, description="已写入当前向量索引的知识版本")
    vector_item_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False), description="当前关联的向量分块标识")
    is_recallable: bool = Field(default=False, index=True, nullable=False, description="当前版本是否允许召回")
    pending_job_id: int | None = Field(default=None, index=True, description="待处理知识作业；步骤 4 接入")
    organization_lock_token: str | None = Field(default=None, index=True, max_length=64, description="知识整理运行锁；非空时禁止人工和 LLM 修改或删除")
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    last_recalled_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))


class ManagedKnowledgeRevision(SQLModel, table=True):
    __tablename__ = "managed_knowledge_revision"
    __table_args__ = (
        UniqueConstraint("knowledge_base_id", "knowledge_id", "version", name="uq_managed_knowledge_revision_kb_knowledge_version"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_managed_knowledge_revision_kb_owner",
            ondelete="CASCADE",
        ),
        Index("ix_managed_knowledge_revision_history", "knowledge_base_id", "knowledge_id", "version"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    knowledge_base_id: int = Field(nullable=False, index=True)
    uid: str = Field(nullable=False, index=True, max_length=50)
    knowledge_id: int = Field(nullable=False, index=True, description="稳定知识标识；故意不外键到条目，以便未来清理主记录后仍保留审计快照")
    version: int = Field(ge=1, index=True, nullable=False)
    operation: ManagedKnowledgeRevisionOperation = Field(index=True, max_length=20)
    before_snapshot: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    after_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    source_type: ManagedKnowledgeSourceType = Field(index=True, max_length=30)
    source_reference: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    source_job_id: int | None = Field(default=None, index=True)
    modified_by: ManagedKnowledgeActorType = Field(index=True, max_length=20)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
