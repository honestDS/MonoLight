from datetime import datetime
from enum import StrEnum

from sqlmodel import Column, DateTime, Field, SQLModel, Text, UniqueConstraint

from app.core.constants import (
    MEMORY_MAX_ACTIVE_RECORDS,
    MEMORY_ORGANIZE_POLICY_VERSION,
    MEMORY_ORGANIZE_TRIGGER_RECORDS,
)
from app.core.utils.time import get_local_time


class LongTermMemoryMigrationStatus(StrEnum):
    PREPARING = "preparing"
    BUILDING = "building"
    CATCHING_UP = "catching_up"
    VALIDATING = "validating"
    SWITCHING = "switching"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LongTermMemoryOldCollectionCleanupStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class LongTermMemoryCapacityStatus(StrEnum):
    NORMAL = "normal"
    OVER_LIMIT = "over_limit"


class LongTermMemoryIndexStatus(StrEnum):
    PENDING = "pending"
    READY = "ready"
    REINDEXING = "reindexing"
    FAILED = "failed"


class LongTermMemoryStore(SQLModel, table=True):
    __tablename__ = "long_term_memory_store"
    __table_args__ = (UniqueConstraint("uid", name="uq_long_term_memory_store_uid"),)

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)

    active_embedding_channel_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="channel.id",
        ondelete="RESTRICT",
    )
    active_embedding_model_id: str | None = Field(default=None, index=True, max_length=255)
    active_embedding_dimensions: int | None = Field(default=None, ge=1)
    active_embedding_signature: str | None = Field(default=None, index=True, max_length=128)
    active_embedding_revision: int = Field(default=0, ge=0, index=True)
    active_collection_name: str | None = Field(default=None, index=True, max_length=255)

    target_embedding_channel_id: int | None = Field(
        default=None,
        index=True,
        foreign_key="channel.id",
        ondelete="RESTRICT",
    )
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

    max_active_records: int = Field(default=MEMORY_MAX_ACTIVE_RECORDS, ge=1, le=MEMORY_MAX_ACTIVE_RECORDS)
    organize_trigger_records: int = Field(
        default=MEMORY_ORGANIZE_TRIGGER_RECORDS,
        ge=MEMORY_ORGANIZE_TRIGGER_RECORDS,
        le=MEMORY_ORGANIZE_TRIGGER_RECORDS,
    )
    auto_organize_enabled: bool = Field(default=False)
    organization_channel_id: int | None = Field(
        default=None,
        index=True,
        gt=0,
        foreign_key="channel.id",
        ondelete="RESTRICT",
    )
    organization_model_id: str | None = Field(default=None, index=True, max_length=255)
    organization_policy_version: int = Field(default=MEMORY_ORGANIZE_POLICY_VERSION, ge=1)
    organization_last_job_id: int | None = Field(default=None, index=True)
    organization_last_run_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    organization_error: str | None = Field(default=None, sa_column=Column(Text))
    capacity_status: LongTermMemoryCapacityStatus = Field(
        default=LongTermMemoryCapacityStatus.NORMAL,
        index=True,
        max_length=20,
    )
    index_revision: int = Field(default=0, ge=0, index=True)
    index_status: LongTermMemoryIndexStatus = Field(
        default=LongTermMemoryIndexStatus.PENDING,
        index=True,
        max_length=20,
    )
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
