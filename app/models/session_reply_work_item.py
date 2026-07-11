from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlmodel import JSON, Column, DateTime, Field, SQLModel, UniqueConstraint

from app.core.utils.time import get_local_time


class SessionReplyWorkType(StrEnum):
    FOREGROUND_REPLY = "foreground_reply"
    BACKGROUND_TOOL_SUMMARY = "background_tool_summary"
    SCHEDULED_TASK_SUMMARY = "scheduled_task_summary"


class SessionReplySourceType(StrEnum):
    USER_MESSAGE = "user_message"
    BACKGROUND_TASK = "background_task"
    SCHEDULED_TASK_RUN = "scheduled_task_run"
    TOOL_RESULT = "tool_result"


class SessionReplyWorkStatus(StrEnum):
    READY_FOR_LLM = "ready_for_llm"
    RUNNING = "running"
    WAITING_EXTERNAL_WORK = "waiting_external_work"
    MERGED = "merged"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


SESSION_REPLY_TERMINAL_STATUSES = {
    SessionReplyWorkStatus.MERGED,
    SessionReplyWorkStatus.SUCCEEDED,
    SessionReplyWorkStatus.FAILED,
    SessionReplyWorkStatus.CANCELLED,
}


class SessionReplyWorkItem(SQLModel, table=True):
    __tablename__ = "session_reply_work_item"
    __table_args__ = (UniqueConstraint("session_id", "sequence_no", name="uq_session_reply_work_sequence"),)

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    profile_id: int = Field(index=True)
    sequence_no: int = Field(index=True, ge=1)
    work_type: SessionReplyWorkType = Field(index=True, max_length=40)
    source_type: SessionReplySourceType = Field(index=True, max_length=40)
    source_id: str = Field(index=True, max_length=100)
    dedupe_key: str = Field(unique=True, index=True, max_length=160)
    status: SessionReplyWorkStatus = Field(default=SessionReplyWorkStatus.READY_FOR_LLM, index=True, max_length=30)
    merged_into_id: int | None = Field(default=None, index=True)
    input_message_ids: list[int] | None = Field(default=None, sa_column=Column(JSON))
    result_message_id: int | None = Field(default=None, index=True)
    execution_state: dict[str, Any] = Field(default_factory=dict, sa_column=Column(JSON))
    event_sent: bool = Field(default=False)
    locked_by: str | None = Field(default=None, index=True, max_length=100)
    lock_until: int | None = Field(default=None, index=True)
    attempt_count: int = Field(default=0, ge=0)
    max_attempts: int = Field(default=5, ge=1)
    available_at: int = Field(default=0, index=True)
    error: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))


class SessionReplySequence(SQLModel, table=True):
    __tablename__ = "session_reply_sequence"

    session_id: str = Field(primary_key=True, max_length=100)
    next_sequence_no: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True)))
