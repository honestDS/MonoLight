from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import ForeignKeyConstraint
from sqlmodel import JSON, Column, DateTime, Field, SQLModel, UniqueConstraint

from app.core.utils.time import get_local_time


class SessionReplyWorkCategory(StrEnum):
    FOREGROUND = "foreground"
    SCHEDULED_TASK = "scheduled_task"
    SUBAGENT = "subagent"


class SessionReplyWorkType(StrEnum):
    FOREGROUND_REPLY = "foreground_reply"
    CONFIRMED_TOOL_EXECUTION = "confirmed_tool_execution"
    BACKGROUND_TOOL_SUMMARY = "background_tool_summary"
    BACKGROUND_TASK_RESUME = "background_task_resume"
    AGENT_WORK = "agent_work"


class SessionReplySourceType(StrEnum):
    USER_MESSAGE = "user_message"
    AUDIT_RECORD = "audit_record"
    BACKGROUND_TASK = "background_task"
    SCHEDULED_TASK_RUN = "scheduled_task_run"
    PARENT_WORK = "parent_work"
    TOOL_RESULT = "tool_result"


class SessionReplyWorkStatus(StrEnum):
    READY_FOR_LLM = "ready_for_llm"
    RUNNING = "running"
    WAITING_EXTERNAL_WORK = "waiting_external_work"
    MERGED = "merged"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


SESSION_REPLY_ACTIVE_STATUSES = {
    SessionReplyWorkStatus.READY_FOR_LLM,
    SessionReplyWorkStatus.RUNNING,
    SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
}

SESSION_REPLY_TERMINAL_STATUSES = {
    SessionReplyWorkStatus.MERGED,
    SessionReplyWorkStatus.SUCCEEDED,
    SessionReplyWorkStatus.FAILED,
    SessionReplyWorkStatus.CANCELLED,
}


class SessionReplyWorkItem(SQLModel, table=True):
    __tablename__ = "session_reply_work_item"
    __table_args__ = (
        UniqueConstraint("session_id", "sequence_no", name="uq_session_reply_work_sequence"),
        UniqueConstraint("id", "session_id", "uid", name="uq_session_reply_work_item_id_session_uid"),
        ForeignKeyConstraint(
            ["session_id", "uid"],
            ["chat_session.session_id", "chat_session.uid"],
            name="fk_session_reply_work_item_session_owner",
            ondelete="CASCADE",
        ),
    )

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
    max_attempts: int = Field(default=2, ge=1)
    available_at: int = Field(default=0, index=True)
    error: str | None = Field(default=None)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))

    @property
    def work_category(self) -> SessionReplyWorkCategory:
        if self.work_type == SessionReplyWorkType.AGENT_WORK:
            if self.source_type == SessionReplySourceType.PARENT_WORK:
                return SessionReplyWorkCategory.SUBAGENT
            return SessionReplyWorkCategory.SCHEDULED_TASK

        work_category = self.execution_state.get("work_category", SessionReplyWorkCategory.FOREGROUND.value)
        return SessionReplyWorkCategory(work_category)


class SessionReplyWorkCheckpoint(SQLModel, table=True):
    __tablename__ = "session_reply_work_checkpoint"

    work_id: int = Field(
        primary_key=True,
        foreign_key="session_reply_work_item.id",
        ondelete="CASCADE",
    )
    checkpoint: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))


class SessionReplyCheckpointMessage(SQLModel, table=True):
    __tablename__ = "session_reply_checkpoint_message"
    __table_args__ = (
        UniqueConstraint("session_id", "uid", "content_hash"),
        ForeignKeyConstraint(
            ["session_id", "uid"],
            ["chat_session.session_id", "chat_session.uid"],
            ondelete="CASCADE",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)
    uid: str = Field(max_length=100)
    session_id: str = Field(max_length=100)
    content_hash: str = Field(max_length=64)
    payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))


class SessionReplyCheckpointMessageReference(SQLModel, table=True):
    __tablename__ = "session_reply_checkpoint_message_reference"

    work_id: int = Field(
        primary_key=True,
        foreign_key="session_reply_work_item.id",
        ondelete="CASCADE",
    )
    kind: str = Field(primary_key=True, max_length=20)
    message_id: int = Field(
        primary_key=True,
        foreign_key="session_reply_checkpoint_message.id",
        index=True,
        ondelete="CASCADE",
    )


class SessionReplySequence(SQLModel, table=True):
    __tablename__ = "session_reply_sequence"

    session_id: str = Field(
        primary_key=True,
        max_length=100,
        foreign_key="chat_session.session_id",
        ondelete="CASCADE",
    )
    next_sequence_no: int = Field(default=1, ge=1)
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True)))
