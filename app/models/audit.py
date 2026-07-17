from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlmodel import JSON, Column, DateTime, Field, SQLModel, Text, UniqueConstraint

from app.core.utils.time import get_local_time


class AuditRecordStatus(StrEnum):
    PREPARING = "preparing"
    PASSED = "passed"
    BLOCKED = "blocked"
    AUDIT_FAILED = "audit_failed"
    PENDING = "pending"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    EXECUTING = "executing"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    EXECUTION_UNKNOWN = "execution_unknown"


class AuditFailureType(StrEnum):
    AUDIT_SERVICE_FAILED = "audit_service_failed"
    AUDIT_PERSISTENCE_FAILED = "audit_persistence_failed"
    SOURCE_MESSAGE_INVALID = "source_message_invalid"


class AuditDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


class AuditToolConclusion(StrEnum):
    PASSED = "passed"
    BLOCKED = "blocked"
    AUDIT_FAILED = "audit_failed"
    PENDING = "pending"


class AuditExecutionStatus(StrEnum):
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    EXECUTION_UNKNOWN = "execution_unknown"


AUDIT_TERMINAL_STATUSES = {
    AuditRecordStatus.BLOCKED,
    AuditRecordStatus.AUDIT_FAILED,
    AuditRecordStatus.REJECTED,
    AuditRecordStatus.EXPIRED,
    AuditRecordStatus.CANCELLED,
    AuditRecordStatus.SUCCEEDED,
    AuditRecordStatus.FAILED,
    AuditRecordStatus.EXECUTION_UNKNOWN,
}


class AuditRecord(SQLModel, table=True):
    __tablename__ = "audit_record"

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    operator_username: str = Field(max_length=100)
    session_id: str = Field(index=True, max_length=100)
    source: str = Field(index=True, max_length=40)
    language: str = Field(max_length=20)
    status: AuditRecordStatus = Field(default=AuditRecordStatus.PREPARING, index=True, max_length=30)
    failure_type: AuditFailureType | None = Field(default=None, index=True, max_length=40)
    error_reason: str | None = Field(default=None, sa_column=Column(Text))
    source_assistant_message_id: int = Field(index=True)
    working_directory: str = Field(sa_column=Column(Text))
    round_arguments_hash: str = Field(index=True, max_length=64)
    tool_count: int = Field(ge=1)
    intent_summary: str | None = Field(default=None, sa_column=Column(Text))
    context_file_path: str | None = Field(default=None, sa_column=Column(Text))
    decision: AuditDecision | None = Field(default=None, max_length=20)
    decision_message_id: int | None = Field(default=None, index=True)
    decision_raw_message: str | None = Field(default=None, sa_column=Column(Text))
    decided_by: str | None = Field(default=None, max_length=100)
    execution_claim_token: str | None = Field(default=None, index=True, max_length=64)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    updated_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    audited_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    pending_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    expires_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
    decided_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    execution_started_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True)))
    completed_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))


class AuditToolDetail(SQLModel, table=True):
    __tablename__ = "audit_tool_detail"
    __table_args__ = (
        UniqueConstraint("audit_record_id", "original_tool_call_id", name="uq_audit_tool_detail_record_call"),
        UniqueConstraint("audit_record_id", "turn_index", name="uq_audit_tool_detail_record_turn"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    audit_record_id: int = Field(index=True)
    original_tool_call_id: str = Field(index=True, max_length=100)
    turn_index: int = Field(ge=0)
    tool_name: str = Field(index=True, max_length=100)
    conclusion: AuditToolConclusion = Field(index=True, max_length=30)
    score: int | None = Field(default=None, ge=0, le=10)
    reason: str = Field(sa_column=Column(Text))
    arguments_hash: str = Field(index=True, max_length=64)
    arguments_summary: str = Field(sa_column=Column(Text))
    file_snapshots: list[dict[str, Any]] = Field(default_factory=list, sa_column=Column(JSON))
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))


class AuditConfirmationClaim(SQLModel, table=True):
    __tablename__ = "audit_confirmation_claim"
    __table_args__ = (
        UniqueConstraint("uid", "session_id", name="uq_audit_confirmation_claim_user_session"),
        UniqueConstraint("audit_record_id", name="uq_audit_confirmation_claim_record"),
    )

    id: int | None = Field(default=None, primary_key=True, index=True)
    uid: str = Field(index=True, max_length=100)
    session_id: str = Field(index=True, max_length=100)
    audit_record_id: int = Field(index=True)
    created_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))


class AuditExecutionRecord(SQLModel, table=True):
    __tablename__ = "audit_execution_record"
    __table_args__ = (UniqueConstraint("audit_tool_detail_id", "attempt_no", name="uq_audit_execution_detail_attempt"),)

    id: int | None = Field(default=None, primary_key=True, index=True)
    audit_record_id: int = Field(index=True)
    audit_tool_detail_id: int = Field(index=True)
    attempt_no: int = Field(default=1, ge=1)
    status: AuditExecutionStatus = Field(default=AuditExecutionStatus.RUNNING, index=True, max_length=30)
    claim_token: str = Field(index=True, max_length=64)
    execution_node: str = Field(max_length=100)
    new_tool_call_id: str = Field(unique=True, index=True, max_length=100)
    result_summary: str | None = Field(default=None, sa_column=Column(Text))
    error: str | None = Field(default=None, sa_column=Column(Text))
    started_at: datetime = Field(default_factory=get_local_time, sa_column=Column(DateTime(timezone=True), index=True))
    finished_at: datetime | None = Field(default=None, sa_column=Column(DateTime(timezone=True), index=True))
