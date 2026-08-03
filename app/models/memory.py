from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlmodel import JSON, Column, DateTime, Field, Index, SQLModel, Text, UniqueConstraint

from app.core.utils.time import get_local_time

__all__ = [
    "LongTermMemoryEmbeddingDelta",
    "LongTermMemoryEmbeddingDeltaAction",
    "LongTermMemoryEmbeddingDeltaStatus",
    "LongTermMemoryEmbeddingRevision",
    "LongTermMemoryEmbeddingRevisionStatus",
    "LongTermMemoryIndexStatus",
    "LongTermMemoryMigrationStatus",
    "LongTermMemoryMutationJob",
    "LongTermMemoryMutationOperation",
    "LongTermMemoryMutationStatus",
    "LongTermMemoryOldCollectionCleanupStatus",
    "LongTermMemoryRecord",
    "LongTermMemoryRecordIndexStatus",
    "LongTermMemoryRevision",
    "LongTermMemorySource",
    "LongTermMemoryStore",
    "LongTermMemoryType",
]


class LongTermMemoryMigrationStatus(StrEnum):
    PREPARING = "preparing"
    BUILDING = "building"
    CATCHING_UP = "catching_up"
    VALIDATING = "validating"
    SWITCHING = "switching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LongTermMemoryEmbeddingRevisionStatus(StrEnum):
    CONFIRMED = "confirmed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REVERSED = "reversed"


class LongTermMemoryOldCollectionCleanupStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LongTermMemoryIndexStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    REINDEXING = "reindexing"
    FAILED = "failed"


class LongTermMemoryEmbeddingDeltaAction(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"
    SUPPRESS = "suppress"


class LongTermMemoryEmbeddingDeltaStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"


class LongTermMemoryRecordIndexStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    REINDEXING = "reindexing"
    FAILED = "failed"


class LongTermMemoryType(StrEnum):
    FACT = "fact"
    PREFERENCE = "preference"
    PROJECT = "project"
    TODO = "todo"
    CONSTRAINT = "constraint"


class LongTermMemorySource(StrEnum):
    LLM_TOOL = "llm_tool"
    USER_API = "user_api"
    AUTO_EXTRACT = "auto_extract"


class LongTermMemoryMutationOperation(StrEnum):
    CREATE = "create"
    UPDATE = "update"
    RESTORE = "restore"
    REINDEX = "reindex"
    DELETE_CLEANUP = "delete_cleanup"
    EMBEDDING_MIGRATION = "embedding_migration"
    EXTRACT = "extract"


class LongTermMemoryMutationStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    RETRY = "retry"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LongTermMemoryStore(SQLModel, table=True):
    __tablename__ = "long_term_memory_store"
    __table_args__ = (UniqueConstraint("uid", name="uq_long_term_memory_store_uid"),)

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)

    active_embedding_channel_id: int | None = Field(default=None, index=True)
    active_embedding_model_id: str | None = Field(default=None, index=True, max_length=255)
    active_embedding_dimensions: int | None = Field(default=None, ge=1)
    active_embedding_signature: str | None = Field(default=None, index=True, max_length=128)
    active_embedding_revision: int = Field(default=0, ge=0, index=True)
    active_collection_name: str | None = Field(default=None, index=True, max_length=255)

    target_embedding_channel_id: int | None = Field(default=None, index=True)
    target_embedding_model_id: str | None = Field(default=None, index=True, max_length=255)
    target_embedding_dimensions: int | None = Field(default=None, ge=1)
    target_embedding_signature: str | None = Field(default=None, index=True, max_length=128)
    target_collection_name: str | None = Field(default=None, index=True, max_length=255)

    migration_job_id: int | None = Field(default=None, index=True)
    migration_status: LongTermMemoryMigrationStatus | None = Field(
        default=None,
        index=True,
        max_length=20,
    )
    migration_snapshot_boundary: int | None = Field(default=None, ge=0)
    migration_cursor: int | None = Field(default=None, ge=0)
    migration_total_count: int = Field(default=0, ge=0)
    migration_success_count: int = Field(default=0, ge=0)
    migration_failure_count: int = Field(default=0, ge=0)
    migration_delta_high_watermark: int = Field(default=0, ge=0)
    migration_delta_applied_watermark: int = Field(default=0, ge=0)
    migration_error: str | None = Field(default=None, sa_column=Column(Text))
    migration_started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    migration_finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    old_collection_name: str | None = Field(default=None, index=True, max_length=255)
    old_collection_cleanup_status: LongTermMemoryOldCollectionCleanupStatus = Field(
        default=LongTermMemoryOldCollectionCleanupStatus.NONE,
        index=True,
        max_length=20,
    )
    old_collection_cleanup_job_id: int | None = Field(default=None, index=True)
    old_collection_cleanup_error: str | None = Field(default=None, sa_column=Column(Text))
    old_collection_cleanup_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))

    max_active_records: int = Field(default=500, ge=1)
    index_revision: int = Field(default=0, ge=0, index=True)
    index_status: LongTermMemoryIndexStatus = Field(
        default=LongTermMemoryIndexStatus.PENDING,
        index=True,
        max_length=20,
    )
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))


class LongTermMemoryEmbeddingRevision(SQLModel, table=True):
    __tablename__ = "long_term_memory_embedding_revision"
    __table_args__ = (
        UniqueConstraint("uid", "revision", name="uq_long_term_memory_embedding_revision_uid_revision"),
        Index("ix_ltm_embedding_revision_confirm_profile", "confirmation_source_profile_id"),
        Index("ix_ltm_embedding_revision_selection_signature", "embedding_selection_signature"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    revision: int = Field(default=1, ge=1, index=True)
    from_channel_id: int | None = Field(default=None, index=True)
    from_model_id: str | None = Field(default=None, index=True, max_length=255)
    from_dimensions: int | None = Field(default=None, ge=1)
    from_signature: str | None = Field(default=None, max_length=128)
    from_collection: str | None = Field(default=None, index=True, max_length=255)
    to_channel_id: int | None = Field(default=None, index=True)
    to_model_id: str | None = Field(default=None, index=True, max_length=255)
    to_dimensions: int | None = Field(default=None, ge=1)
    to_signature: str | None = Field(default=None, max_length=128)
    to_collection: str | None = Field(default=None, index=True, max_length=255)
    confirmation_source_profile_id: int | None = Field(default=None)
    confirmation_source: str = Field(default="profile", index=True, max_length=50)
    embedding_selection_signature: str | None = Field(default=None, max_length=128)
    confirmed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    job_id: int | None = Field(default=None, index=True)
    status: LongTermMemoryEmbeddingRevisionStatus = Field(
        default=LongTermMemoryEmbeddingRevisionStatus.CONFIRMED,
        index=True,
        max_length=20,
    )
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))


class LongTermMemoryEmbeddingDelta(SQLModel, table=True):
    __tablename__ = "long_term_memory_embedding_delta"
    __table_args__ = (
        UniqueConstraint("migration_job_id", "sequence", name="uq_long_term_memory_embedding_delta_job_sequence"),
        Index("ix_long_term_memory_embedding_delta_uid_job_sequence", "uid", "migration_job_id", "sequence"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    migration_job_id: int = Field(index=True)
    sequence: int = Field(ge=1, index=True)
    memory_id: int | None = Field(default=None, index=True)
    memory_version: int | None = Field(default=None, ge=0)
    action: LongTermMemoryEmbeddingDeltaAction = Field(
        default=LongTermMemoryEmbeddingDeltaAction.UPSERT,
        index=True,
        max_length=20,
    )
    source_mutation_job_id: int | None = Field(default=None, index=True)
    snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: LongTermMemoryEmbeddingDeltaStatus = Field(
        default=LongTermMemoryEmbeddingDeltaStatus.PENDING,
        index=True,
        max_length=20,
    )
    error: str | None = Field(default=None, sa_column=Column(Text))
    applied_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))


class LongTermMemoryRecord(SQLModel, table=True):
    __tablename__ = "long_term_memory_record"
    __table_args__ = (
        UniqueConstraint("uid", "memory_key", name="uq_long_term_memory_record_uid_key"),
        UniqueConstraint("uid", "content_hash", name="uq_long_term_memory_record_uid_content_hash"),
        UniqueConstraint("vector_item_id", name="uq_long_term_memory_record_vector_item_id"),
        Index("ix_long_term_memory_record_uid_is_active_deleted_at", "uid", "is_active", "deleted_at"),
        Index("ix_long_term_memory_record_uid_updated_at", "uid", "updated_at"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    memory_key: str | None = Field(default=None, index=True, max_length=255)
    memory_type: LongTermMemoryType = Field(default=LongTermMemoryType.FACT, index=True, max_length=50)
    importance: int = Field(default=0, ge=0, le=10, index=True)
    scope: str | None = Field(default=None, index=True, max_length=100)
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    content_hash: str | None = Field(default=None, index=True, max_length=64)
    version: int = Field(default=0, ge=0, index=True)
    indexed_version: int = Field(default=0, ge=0, index=True)
    vector_item_id: str | None = Field(default=None, index=True, max_length=255)
    source: LongTermMemorySource = Field(default=LongTermMemorySource.USER_API, index=True, max_length=50)
    source_id: str | None = Field(default=None, index=True, max_length=255)
    source_session_id: str | None = Field(default=None, index=True, max_length=100)
    source_profile_id: int | None = Field(default=None, index=True)
    source_message_id: int | None = Field(default=None, index=True)
    source_job_id: int | None = Field(default=None, index=True)
    change_evidence: str | None = Field(default=None, sa_column=Column(Text))
    is_active: bool = Field(default=False, index=True)
    pending_mutation_job_id: int | None = Field(default=None, index=True)
    suppress_recall: bool = Field(default=False, index=True)
    suppressed_by_job_id: int | None = Field(default=None, index=True)
    index_status: LongTermMemoryRecordIndexStatus = Field(
        default=LongTermMemoryRecordIndexStatus.PENDING,
        index=True,
        max_length=20,
    )
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    indexed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    deleted_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))


class LongTermMemoryRevision(SQLModel, table=True):
    __tablename__ = "long_term_memory_revision"
    __table_args__ = (
        UniqueConstraint("uid", "memory_id", "version", name="uq_long_term_memory_revision_uid_memory_version"),
        Index("ix_long_term_memory_revision_uid_memory_version", "uid", "memory_id", "version"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    memory_id: int = Field(index=True)
    version: int = Field(ge=1, index=True)
    memory_key: str = Field(default="", index=True, max_length=255)
    memory_type: LongTermMemoryType = Field(default=LongTermMemoryType.FACT, index=True, max_length=50)
    importance: int = Field(default=0, ge=0, le=10)
    scope: str | None = Field(default=None, index=True, max_length=100)
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    content_hash: str | None = Field(default=None, index=True, max_length=64)
    source: LongTermMemorySource = Field(default=LongTermMemorySource.USER_API, index=True, max_length=50)
    source_id: str | None = Field(default=None, index=True, max_length=255)
    source_session_id: str | None = Field(default=None, index=True, max_length=100)
    source_profile_id: int | None = Field(default=None, index=True)
    source_message_id: int | None = Field(default=None, index=True)
    source_job_id: int | None = Field(default=None, index=True)
    change_evidence: str | None = Field(default=None, sa_column=Column(Text))
    published_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))


class LongTermMemoryMutationJob(SQLModel, table=True):
    __tablename__ = "long_term_memory_mutation_job"
    __table_args__ = (
        UniqueConstraint("uid", "dedupe_key", name="uq_long_term_memory_mutation_job_uid_dedupe"),
        UniqueConstraint("active_mutation_key", name="uq_long_term_memory_mutation_job_active_key"),
        Index("ix_long_term_memory_mutation_job_uid_status_available", "uid", "status", "available_at"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
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
