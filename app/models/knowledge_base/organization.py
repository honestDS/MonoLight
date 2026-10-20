from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint, Text
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


class KnowledgeOrganizationStageStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class KnowledgeOrganizationFragmentStatus(StrEnum):
    COMPLETED = "completed"
    INVALIDATED = "invalidated"


class KnowledgeOrganizationSnapshot(SQLModel, table=True):
    __tablename__ = "knowledge_organization_snapshot"
    __table_args__ = (
        UniqueConstraint("uid", "knowledge_base_id", "snapshot_key", name="uq_knowledge_organization_snapshot_identity"),
        ForeignKeyConstraint(
            ["knowledge_base_id", "uid"],
            ["knowledge_base.id", "knowledge_base.uid"],
            name="fk_knowledge_organization_snapshot_kb_owner",
            ondelete="CASCADE",
        ),
        Index("ix_knowledge_organization_snapshot_kb_created", "knowledge_base_id", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(nullable=False, index=True, max_length=50)
    knowledge_base_id: int = Field(nullable=False, index=True)
    snapshot_key: str = Field(nullable=False, index=True, max_length=64)
    boundary_revision_id: int = Field(default=0, ge=0, nullable=False)
    active_embedding_revision: int = Field(default=0, ge=0, nullable=False)
    index_revision: int = Field(default=0, ge=0, nullable=False)
    item_count: int = Field(default=0, ge=0, nullable=False)
    items: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))


class KnowledgeOrganizationSnapshotItem(SQLModel, table=True):
    __tablename__ = "knowledge_organization_snapshot_item"
    __table_args__ = (
        UniqueConstraint("snapshot_id", "sequence", name="uq_knowledge_organization_snapshot_item_sequence"),
        UniqueConstraint("snapshot_id", "knowledge_id", name="uq_knowledge_organization_snapshot_item_knowledge"),
        Index("ix_knowledge_organization_snapshot_item_snapshot_sequence", "snapshot_id", "sequence"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    snapshot_id: int = Field(nullable=False, index=True, foreign_key="knowledge_organization_snapshot.id", ondelete="CASCADE")
    uid: str = Field(nullable=False, index=True, max_length=50)
    knowledge_base_id: int = Field(nullable=False, index=True)
    sequence: int = Field(ge=0, nullable=False)
    knowledge_id: int = Field(ge=1, nullable=False, index=True)
    expected_version: int = Field(ge=1, nullable=False)
    knowledge_key: str = Field(nullable=False, max_length=255)
    content_hash: str = Field(nullable=False, max_length=64)
    content_token_count: int = Field(default=0, ge=0, nullable=False)
    revision_id: int = Field(ge=1, nullable=False, index=True)
    source_type: str = Field(nullable=False, max_length=30)
    source_reference: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    llm_maintainable: bool = Field(default=True, nullable=False)
    indexed_version: int = Field(ge=1, nullable=False)
    vector_item_ids: list[str] = Field(default_factory=list, sa_column=Column(JSON, nullable=False))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))


class KnowledgeOrganizationStage(SQLModel, table=True):
    __tablename__ = "knowledge_organization_stage"
    __table_args__ = (
        UniqueConstraint("work_key", "stage_key", name="uq_knowledge_organization_stage_work_stage"),
        Index("ix_knowledge_organization_stage_kb_status", "knowledge_base_id", "status", "created_at"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(nullable=False, index=True, max_length=50)
    knowledge_base_id: int = Field(nullable=False, index=True)
    snapshot_id: int = Field(nullable=False, index=True, foreign_key="knowledge_organization_snapshot.id", ondelete="CASCADE")
    work_key: str = Field(nullable=False, index=True, max_length=64)
    snapshot_key: str = Field(nullable=False, index=True, max_length=64)
    stage_key: str = Field(nullable=False, index=True, max_length=64)
    stage_index: int = Field(default=0, ge=0, nullable=False)
    lower_stage_key: str | None = Field(default=None, index=True, max_length=64)
    model_key: str = Field(nullable=False, index=True, max_length=64)
    model_snapshot: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    expected_fragment_count: int = Field(ge=1, nullable=False)
    succeeded_fragment_count: int = Field(default=0, ge=0, nullable=False)
    status: KnowledgeOrganizationStageStatus = Field(default=KnowledgeOrganizationStageStatus.RUNNING, index=True, max_length=20)
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    completed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))


class KnowledgeOrganizationFragment(SQLModel, table=True):
    __tablename__ = "knowledge_organization_fragment"
    __table_args__ = (
        UniqueConstraint("work_key", "stage_key", "fragment_index", name="uq_knowledge_organization_fragment_work_stage_index"),
        UniqueConstraint("dedupe_key", name="uq_knowledge_organization_fragment_dedupe"),
        Index("ix_knowledge_organization_fragment_stage_index", "stage_id", "fragment_index"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    dedupe_key: str = Field(nullable=False, index=True, max_length=64)
    uid: str = Field(nullable=False, index=True, max_length=50)
    knowledge_base_id: int = Field(nullable=False, index=True)
    snapshot_id: int = Field(nullable=False, index=True)
    stage_id: int = Field(nullable=False, index=True, foreign_key="knowledge_organization_stage.id", ondelete="CASCADE")
    work_key: str = Field(nullable=False, index=True, max_length=64)
    snapshot_key: str = Field(nullable=False, index=True, max_length=64)
    stage_key: str = Field(nullable=False, index=True, max_length=64)
    model_key: str = Field(nullable=False, index=True, max_length=64)
    fragment_index: int = Field(ge=0, nullable=False)
    candidate_scope: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    result: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON, nullable=False))
    status: KnowledgeOrganizationFragmentStatus = Field(default=KnowledgeOrganizationFragmentStatus.COMPLETED, index=True, max_length=20)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
