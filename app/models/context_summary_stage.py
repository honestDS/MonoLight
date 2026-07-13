from datetime import datetime
from enum import StrEnum

from sqlmodel import Column, DateTime, Field, SQLModel, UniqueConstraint

from app.core.utils.time import get_local_time


class ContextSummaryStageStatus(StrEnum):
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    INVALIDATED = "invalidated"


class ContextSummaryFragmentStatus(StrEnum):
    COMPLETED = "completed"
    INVALIDATED = "invalidated"


class ContextSummaryStage(SQLModel, table=True):
    __tablename__ = "context_summary_stage"
    __table_args__ = (
        UniqueConstraint(
            "work_dedupe_key",
            "stage_key",
            name="uq_context_summary_stage_work_stage",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    work_id: int = Field(index=True)
    work_dedupe_key: str = Field(index=True, max_length=160)
    snapshot_key: str = Field(index=True, max_length=64)
    stage_key: str = Field(index=True, max_length=64)
    lower_stage_key: str | None = Field(default=None, index=True, max_length=64)
    model_key: str = Field(index=True, max_length=64)
    channel_id: int = Field(index=True)
    model_id: str = Field(max_length=255)
    context_window_k: int = Field(ge=1)
    max_output_tokens: int = Field(ge=1)
    safety_margin_tokens: int = Field(ge=0)
    expected_summary_message_id: int | None = Field(default=None)
    expected_summary_revision: int = Field(ge=0)
    snapshot_max_message_id: int = Field(ge=1)
    persistent_summary_target_id: int = Field(ge=1)
    expected_fragment_count: int = Field(ge=1)
    succeeded_fragment_count: int = Field(default=0, ge=0)
    status: ContextSummaryStageStatus = Field(
        default=ContextSummaryStageStatus.RUNNING,
        index=True,
        max_length=20,
    )
    error: str | None = Field(default=None)
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
    completed_at: datetime | None = Field(
        default=None,
        sa_column=Column(DateTime(timezone=True), index=True),
    )


class ContextSummaryFragment(SQLModel, table=True):
    __tablename__ = "context_summary_fragment"
    __table_args__ = (
        UniqueConstraint(
            "work_dedupe_key",
            "stage_key",
            "fragment_index",
            name="uq_context_summary_fragment_work_stage_index",
        ),
        UniqueConstraint(
            "dedupe_key",
            name="uq_context_summary_fragment_dedupe",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    dedupe_key: str = Field(index=True, max_length=64)
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    work_id: int = Field(index=True)
    work_dedupe_key: str = Field(index=True, max_length=160)
    snapshot_key: str = Field(index=True, max_length=64)
    stage_key: str = Field(index=True, max_length=64)
    model_key: str = Field(index=True, max_length=64)
    fragment_index: int = Field(ge=0)
    message_start_id: int = Field(ge=1)
    message_end_id: int = Field(ge=1)
    channel_id: int = Field(index=True)
    model_id: str = Field(max_length=255)
    token_count: int = Field(ge=0)
    content: str
    status: ContextSummaryFragmentStatus = Field(
        default=ContextSummaryFragmentStatus.COMPLETED,
        index=True,
        max_length=20,
    )
    created_at: datetime = Field(
        default_factory=get_local_time,
        sa_column=Column(DateTime(timezone=True), index=True),
    )
