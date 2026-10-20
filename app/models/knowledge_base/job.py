from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, Text
from sqlmodel import JSON, Column, DateTime, Field, Index, SQLModel, UniqueConstraint

from app.core.utils.time import get_local_time


class KnowledgeJobOperation(StrEnum):
    MANAGED_CREATE = "managed_create"
    MANAGED_UPDATE = "managed_update"
    MANAGED_DELETE_CLEANUP = "managed_delete_cleanup"
    MANAGED_VECTOR_CLEANUP = "managed_vector_cleanup"
    USER_DOCUMENT_INDEX = "user_document_index"
    USER_DOCUMENT_DELETE_CLEANUP = "user_document_delete_cleanup"
    EMBEDDING_MIGRATION = "embedding_migration"
    REINDEX = "reindex"
    OLD_COLLECTION_CLEANUP = "old_collection_cleanup"
    MIGRATION_TARGET_CLEANUP = "migration_target_cleanup"
    KNOWLEDGE_MAINTENANCE = "knowledge_maintenance"
    AUTO_ORGANIZE = "auto_organize"
    MANUAL_ORGANIZE = "manual_organize"
    ORGANIZE_MUTATION = "organize_mutation"


class KnowledgeJobStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class KnowledgeJob(SQLModel, table=True):
    __tablename__ = "knowledge_job"
    __table_args__ = (
        UniqueConstraint("uid", "dedupe_key", name="uq_knowledge_job_uid_dedupe"),
        UniqueConstraint("uid", "active_change_key", name="uq_knowledge_job_uid_active_change"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_knowledge_job_kb_owner",
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_job_uid_status_available", "uid", "status", "available_at"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(nullable=False, index=True, max_length=50)
    parent_job_id: int | None = Field(default=None, index=True)
    operation: KnowledgeJobOperation = Field(index=True, max_length=40)
    dedupe_key: str = Field(index=True, max_length=255)
    request_hash: str = Field(index=True, max_length=64)
    active_change_key: str | None = Field(default=None, index=True, max_length=255)
    status: KnowledgeJobStatus = Field(default=KnowledgeJobStatus.PENDING, index=True, max_length=20)
    knowledge_base_id: int = Field(nullable=False, index=True)
    knowledge_id: int | None = Field(default=None, index=True)
    expected_version: int | None = Field(default=None, ge=1)
    payload: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))
    source_session_id: str | None = Field(default=None, index=True, max_length=100)
    source_profile_id: int | None = Field(default=None, index=True)
    source_message_id: int | None = Field(default=None, index=True)
    available_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    attempt_count: int = Field(default=0, ge=0, index=True)
    max_attempts: int = Field(default=3, ge=1)
    locked_by: str | None = Field(default=None, index=True, max_length=100)
    lock_until: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    cancel_requested_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
