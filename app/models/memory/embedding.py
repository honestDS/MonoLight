from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlmodel import JSON, Column, DateTime, Field, Index, SQLModel, Text, UniqueConstraint

from app.core.utils.time import get_local_time


class LongTermMemoryEmbeddingRevisionStatus(StrEnum):
    CONFIRMED = "confirmed"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    REVERSED = "reversed"


class LongTermMemoryEmbeddingDeltaAction(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"
    SUPPRESS = "suppress"


class LongTermMemoryEmbeddingDeltaStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"
    SKIPPED = "skipped"
    FAILED = "failed"


class LongTermMemoryEmbeddingSelectionToken(SQLModel, table=True):
    """一次性长期记忆嵌入模型选择凭证。"""

    __tablename__ = "long_term_memory_embedding_selection_token"
    __table_args__ = (
        UniqueConstraint("token_digest", name="uq_ltm_embedding_selection_token_digest"),
        Index("ix_ltm_embedding_selection_token_uid_profile", "uid", "profile_id"),
        Index("ix_ltm_embedding_selection_token_expires_at", "expires_at"),
        Index("ix_ltm_embedding_selection_token_consumed_at", "consumed_at"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    profile_id: int = Field(index=True, foreign_key="profile.id", ondelete="CASCADE")
    token_digest: str = Field(max_length=64, nullable=False)
    profile_config_digest: str = Field(max_length=64, nullable=False)
    active_embedding_revision: int = Field(default=0, ge=0)
    target_embedding_channel_id: int = Field(
        gt=0,
        index=True,
        foreign_key="channel.id",
        ondelete="CASCADE",
    )
    target_embedding_model_id: str = Field(max_length=255)
    target_embedding_dimensions: int = Field(gt=0)
    target_embedding_signature: str = Field(max_length=128)
    expires_at: datetime = Field(sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    consumed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))


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
