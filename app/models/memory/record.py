from datetime import datetime
from enum import StrEnum

from sqlmodel import Column, DateTime, Field, Index, SQLModel, Text, UniqueConstraint

from app.core.utils.time import get_local_time


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
    AUTO_ORGANIZE = "auto_organize"


class LongTermMemoryRecord(SQLModel, table=True):
    __tablename__ = "long_term_memory_record"
    __table_args__ = (
        UniqueConstraint("uid", "memory_key", name="uq_long_term_memory_record_uid_key"),
        UniqueConstraint("uid", "content_hash", name="uq_long_term_memory_record_uid_content_hash"),
        UniqueConstraint("vector_item_id", name="uq_long_term_memory_record_vector_item_id"),
        Index("ix_long_term_memory_record_uid_is_active_deleted_at", "uid", "is_active", "deleted_at"),
        Index("ix_long_term_memory_record_uid_updated_at", "uid", "updated_at"),
        Index(
            "ix_ltm_record_eviction_candidate",
            "uid",
            "is_active",
            "pinned",
            "last_recalled_at",
            "updated_at",
            "id",
        ),
        {"sqlite_autoincrement": True},
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    memory_key: str | None = Field(default=None, index=True, max_length=255)
    memory_type: LongTermMemoryType = Field(default=LongTermMemoryType.FACT, index=True, max_length=50)
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    content_token_count: int = Field(default=0, ge=0)
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
    pinned: bool = Field(default=False)
    last_recalled_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
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
    content: str = Field(default="", sa_column=Column(Text, nullable=False))
    content_token_count: int = Field(default=0, ge=0)
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
