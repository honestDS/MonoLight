from datetime import datetime
from enum import StrEnum

from sqlalchemy import ForeignKeyConstraint, Text
from sqlmodel import Column, DateTime, Field, Index, SQLModel, UniqueConstraint

from app.core.utils.time import get_local_time


class KnowledgeBaseMigrationSourceType(StrEnum):
    USER_DOCUMENT = "user_document"
    MANAGED_KNOWLEDGE = "managed_knowledge"


class KnowledgeBaseMigrationDeltaAction(StrEnum):
    UPSERT = "upsert"
    DELETE = "delete"


class KnowledgeBaseMigrationDeltaStatus(StrEnum):
    PENDING = "pending"
    APPLIED = "applied"


class KnowledgeBaseEmbeddingDelta(SQLModel, table=True):
    __tablename__ = "knowledge_base_embedding_delta"
    __table_args__ = (
        UniqueConstraint("migration_job_id", "sequence", name="uq_kb_embedding_delta_job_sequence"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_kb_embedding_delta_kb_owner",
            ondelete="CASCADE",
        ),
        Index(
            "ix_kb_embedding_delta_uid_job_sequence",
            "uid",
            "migration_job_id",
            "sequence",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(nullable=False, index=True, max_length=50)
    knowledge_base_id: int = Field(nullable=False, index=True)
    migration_job_id: int = Field(nullable=False, index=True)
    sequence: int = Field(ge=1, index=True)
    source_type: KnowledgeBaseMigrationSourceType = Field(index=True, max_length=30)
    source_id: int = Field(ge=1, index=True)
    source_version: int | None = Field(default=None, ge=1, index=True)
    action: KnowledgeBaseMigrationDeltaAction = Field(index=True, max_length=20)
    status: KnowledgeBaseMigrationDeltaStatus = Field(
        default=KnowledgeBaseMigrationDeltaStatus.PENDING,
        index=True,
        max_length=20,
    )
    error: str | None = Field(default=None, sa_column=Column(Text))
    applied_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
