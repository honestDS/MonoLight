from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlmodel import JSON, Column, DateTime, Field, Index, SQLModel, Text, UniqueConstraint

from app.core.utils.time import get_local_time


class LongTermMemoryMutationOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    RESTORE = "restore"
    REINDEX = "reindex"
    DELETE_CLEANUP = "delete_cleanup"
    VECTOR_CLEANUP = "vector_cleanup"
    EMBEDDING_MIGRATION = "embedding_migration"
    EXTRACT = "extract"
    CREATE_WITH_EVICTION = "create_with_eviction"
    ORGANIZE = "organize"
    ORGANIZE_MERGE = "organize_merge"


class LongTermMemoryMutationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LongTermMemoryMutationJob(SQLModel, table=True):
    __tablename__ = "long_term_memory_mutation_job"
    __table_args__ = (
        UniqueConstraint("uid", "dedupe_key", name="uq_long_term_memory_mutation_job_uid_dedupe"),
        UniqueConstraint("active_mutation_key", name="uq_long_term_memory_mutation_job_active_key"),
        Index("ix_long_term_memory_mutation_job_uid_status_available", "uid", "status", "available_at"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    parent_job_id: int | None = Field(default=None, index=True)
    operation: LongTermMemoryMutationOperation = Field(index=True, max_length=30)
    dedupe_key: str = Field(index=True, max_length=255)
    active_mutation_key: str | None = Field(default=None, index=True, max_length=255)
    status: LongTermMemoryMutationStatus = Field(default=LongTermMemoryMutationStatus.PENDING, index=True, max_length=20)
    memory_id: int | None = Field(default=None, index=True)
    expected_version: int | None = Field(default=None, ge=0)
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
