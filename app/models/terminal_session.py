from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlmodel import JSON, Column, DateTime, Field, SQLModel, Text, UniqueConstraint

from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.core.utils.time import get_local_time

__all__ = [
    "TerminalControlCommand",
    "TerminalControlCommandStatus",
    "TerminalSession",
]


class TerminalControlCommandStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class TerminalSession(SQLModel, table=True):
    __tablename__ = "terminal_session"

    terminal_session_id: str = Field(primary_key=True, index=True, max_length=128)
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    original_tool_call_id: str = Field(index=True, max_length=100)
    profile_id: int = Field(index=True)
    audit_record_id: int | None = Field(default=None, index=True)
    audit_execution_record_id: int | None = Field(default=None, unique=True, index=True)
    command: str = Field(sa_column=Column(Text, nullable=False))
    working_directory: str = Field(sa_column=Column(Text, nullable=False))
    status: TerminalSessionStatus = Field(
        default=TerminalSessionStatus.STARTING,
        index=True,
        max_length=20,
    )
    allowed_actions: list[str] = Field(sa_column=Column(JSON, nullable=False))
    process_identity: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    output_capacity_bytes: int = Field(default=1_048_576, ge=1)
    oldest_output_offset: int = Field(default=0, ge=0)
    next_output_offset: int = Field(default=0, ge=0)
    oldest_output_sequence: int = Field(default=1, ge=1)
    next_output_sequence: int = Field(default=1, ge=1)
    exit_code: int | None = Field(default=None)
    failure_reason: str | None = Field(default=None, sa_column=Column(Text))
    locked_by: str | None = Field(default=None, index=True, max_length=100)
    lock_until: int | None = Field(default=None, index=True)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))


class TerminalControlCommand(SQLModel, table=True):
    __tablename__ = "terminal_control_command"
    __table_args__ = (
        UniqueConstraint(
            "terminal_session_id",
            "request_id",
            name="uq_terminal_control_command_session_request",
        ),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    terminal_session_id: str = Field(index=True, max_length=128)
    request_id: str = Field(index=True, max_length=128)
    action: TerminalAction = Field(index=True, max_length=20)
    payload: dict[str, Any] = Field(sa_column=Column(JSON, nullable=False))
    payload_hash: str = Field(index=True, max_length=64)
    status: TerminalControlCommandStatus = Field(
        default=TerminalControlCommandStatus.PENDING,
        index=True,
        max_length=20,
    )
    locked_by: str | None = Field(default=None, index=True, max_length=100)
    lock_until: int | None = Field(default=None, index=True)
    result: dict[str, Any] | None = Field(default=None, sa_column=Column(JSON))
    error: str | None = Field(default=None, sa_column=Column(Text))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True, nullable=False))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
