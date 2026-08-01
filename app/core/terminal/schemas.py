"""Immutable protocol models for interactive terminal sessions.

PTY output is always a single merged stdout/stderr byte stream. Offsets refer
to absolute raw-byte positions, while sequence numbers identify output chunks.
"""

import secrets
from enum import StrEnum
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.core.constants import (
    ERR_TERMINAL_ACTIVE_OUTCOME_INVALID,
    ERR_TERMINAL_EMPTY_BUFFER_SEQUENCE_INVALID,
    ERR_TERMINAL_EXITED_OUTCOME_INVALID,
    ERR_TERMINAL_FAILURE_OUTCOME_INVALID,
    ERR_TERMINAL_NONEMPTY_BUFFER_SEQUENCE_INVALID,
    ERR_TERMINAL_OUTPUT_CAPACITY_EXCEEDED,
    ERR_TERMINAL_OUTPUT_OFFSET_ORDER_INVALID,
    ERR_TERMINAL_OUTPUT_SEQUENCE_ORDER_INVALID,
    ERR_TERMINAL_READ_EMPTY_INVALID,
    ERR_TERMINAL_READ_EXPIRED_INVALID,
    ERR_TERMINAL_READ_OFFSET_AHEAD,
    ERR_TERMINAL_READ_OFFSET_ORDER_INVALID,
    ERR_TERMINAL_READ_OK_INVALID,
    ERR_TERMINAL_READ_TRUNCATED_INVALID,
    ERR_TERMINAL_STATUS_TARGET_INVALID,
    ERR_TERMINAL_STATUS_TRANSITION_INVALID,
    ERR_TOOL_SHELL_EXECUTION_MODE_INVALID,
)
from app.core.i18n import t


class TerminalSessionStatus(StrEnum):
    """Terminal session lifecycle status.

    Exited means the child process ended regardless of its exit code. Failed
    means terminal or driver execution failed. Lost means Worker or PTY
    ownership was lost and recovery cannot be claimed.
    """

    STARTING = "starting"
    RUNNING = "running"
    CLOSING = "closing"
    EXITED = "exited"
    FAILED = "failed"
    LOST = "lost"


class ShellExecutionMode(StrEnum):
    INTERACTIVE = "interactive"
    NON_INTERACTIVE = "non_interactive"


def validate_shell_execution_mode(value: ShellExecutionMode | str) -> ShellExecutionMode:
    """Validate and return a shell execution mode without normalization."""
    try:
        return ShellExecutionMode(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(t(ERR_TOOL_SHELL_EXECUTION_MODE_INVALID, value=value)) from exc


TERMINAL_SESSION_FINAL_STATUSES = frozenset(
    {
        TerminalSessionStatus.EXITED,
        TerminalSessionStatus.FAILED,
        TerminalSessionStatus.LOST,
    }
)

TERMINAL_STATUS_TRANSITIONS = MappingProxyType(
    {
        TerminalSessionStatus.STARTING: frozenset(
            {
                TerminalSessionStatus.RUNNING,
                TerminalSessionStatus.CLOSING,
                TerminalSessionStatus.EXITED,
                TerminalSessionStatus.FAILED,
                TerminalSessionStatus.LOST,
            }
        ),
        TerminalSessionStatus.RUNNING: frozenset(
            {
                TerminalSessionStatus.CLOSING,
                TerminalSessionStatus.EXITED,
                TerminalSessionStatus.FAILED,
                TerminalSessionStatus.LOST,
            }
        ),
        TerminalSessionStatus.CLOSING: frozenset(
            {
                TerminalSessionStatus.EXITED,
                TerminalSessionStatus.FAILED,
                TerminalSessionStatus.LOST,
            }
        ),
    }
)


def can_transition_terminal_status(
    current: TerminalSessionStatus,
    target: TerminalSessionStatus,
) -> bool:
    """Return whether a terminal-session status change is valid."""
    try:
        current_status = TerminalSessionStatus(current)
        target_status = TerminalSessionStatus(target)
    except (TypeError, ValueError):
        return False

    if current_status == target_status:
        return True
    if current_status in TERMINAL_SESSION_FINAL_STATUSES:
        return False
    return target_status in TERMINAL_STATUS_TRANSITIONS[current_status]


def validate_terminal_status_transition(
    current: TerminalSessionStatus,
    target: TerminalSessionStatus,
) -> TerminalSessionStatus:
    """Validate a status change and return its target status."""
    try:
        target_status = TerminalSessionStatus(target)
    except (TypeError, ValueError) as exc:
        raise ValueError(t(ERR_TERMINAL_STATUS_TARGET_INVALID, target=target)) from exc

    if not can_transition_terminal_status(current, target_status):
        raise ValueError(
            t(
                ERR_TERMINAL_STATUS_TRANSITION_INVALID,
                current=current,
                target=target,
            )
        )
    return target_status


class TerminalAction(StrEnum):
    STATUS = "status"
    READ = "read"
    WRITE = "write"
    RESIZE = "resize"
    CLOSE = "close"


ALL_TERMINAL_ACTIONS = frozenset(TerminalAction)

type TerminalSessionId = Annotated[
    str,
    Field(min_length=32, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]
type TerminalMutatingRequestId = Annotated[
    str,
    Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$"),
]


def generate_terminal_session_id() -> str:
    """Generate an unguessable terminal session identifier."""
    return secrets.token_urlsafe(32)


def generate_terminal_request_id() -> str:
    """Generate an unguessable mutating terminal request identifier."""
    return secrets.token_urlsafe(32)


class TerminalProtocolModel(BaseModel):
    """Base configuration for immutable terminal protocol messages."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class TerminalStatusRequest(TerminalProtocolModel):
    action: Literal[TerminalAction.STATUS] = TerminalAction.STATUS
    terminal_session_id: TerminalSessionId


class TerminalReadRequest(TerminalProtocolModel):
    action: Literal[TerminalAction.READ] = TerminalAction.READ
    terminal_session_id: TerminalSessionId
    offset: int = Field(default=0, ge=0)
    max_bytes: int = Field(default=65_536, ge=1, le=1_048_576)


class TerminalWriteRequest(TerminalProtocolModel):
    action: Literal[TerminalAction.WRITE] = TerminalAction.WRITE
    terminal_session_id: TerminalSessionId
    request_id: TerminalMutatingRequestId
    data: str = Field(min_length=1, max_length=65_536)


class TerminalResizeRequest(TerminalProtocolModel):
    action: Literal[TerminalAction.RESIZE] = TerminalAction.RESIZE
    terminal_session_id: TerminalSessionId
    request_id: TerminalMutatingRequestId
    columns: int = Field(ge=1, le=1_000)
    rows: int = Field(ge=1, le=1_000)


class TerminalCloseRequest(TerminalProtocolModel):
    action: Literal[TerminalAction.CLOSE] = TerminalAction.CLOSE
    terminal_session_id: TerminalSessionId
    request_id: TerminalMutatingRequestId
    force: bool = False


type TerminalRequest = Annotated[
    TerminalStatusRequest | TerminalReadRequest | TerminalWriteRequest | TerminalResizeRequest | TerminalCloseRequest,
    Field(discriminator="action"),
]


class TerminalActionReceipt(TerminalProtocolModel):
    """Receipt for a mutating action with idempotency-bound semantics.

    The same terminal_session_id and request_id with an identical payload must
    return the same effect and receipt. The same key with another payload must
    be rejected.
    """

    terminal_session_id: TerminalSessionId
    request_id: TerminalMutatingRequestId
    action: Literal[
        TerminalAction.WRITE,
        TerminalAction.RESIZE,
        TerminalAction.CLOSE,
    ]
    duplicate: bool
    session_status: TerminalSessionStatus


class TerminalPermissionScope(TerminalProtocolModel):
    owner_uid: str = Field(min_length=1, max_length=100)
    owner_session_id: str = Field(min_length=1, max_length=100)
    original_tool_call_id: str = Field(min_length=1, max_length=100)
    audit_record_id: int | None = Field(default=None, ge=1)
    audit_execution_record_id: int | None = Field(default=None, ge=1)
    allowed_actions: frozenset[TerminalAction] = Field(
        default_factory=lambda: ALL_TERMINAL_ACTIONS,
        min_length=1,
    )

    def permits(self, uid: str, session_id: str, action: TerminalAction) -> bool:
        """Return whether this exact owner and session may perform an action."""
        return self.owner_uid == uid and self.owner_session_id == session_id and action in self.allowed_actions


class TerminalOutputBufferState(TerminalProtocolModel):
    """Bounds for the retained merged stdout/stderr byte stream.

    Offsets are absolute raw-byte positions. Sequence values start at one and
    increase strictly for each output chunk; next values identify the next
    append position and sequence number.
    """

    capacity_bytes: int = Field(ge=1)
    oldest_offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    oldest_sequence: int = Field(ge=1)
    next_sequence: int = Field(ge=1)

    @model_validator(mode="after")
    def validate_monotonic_bounds(self) -> "TerminalOutputBufferState":
        if self.next_offset < self.oldest_offset:
            raise ValueError(t(ERR_TERMINAL_OUTPUT_OFFSET_ORDER_INVALID))
        if self.next_sequence < self.oldest_sequence:
            raise ValueError(t(ERR_TERMINAL_OUTPUT_SEQUENCE_ORDER_INVALID))
        retained_bytes = self.next_offset - self.oldest_offset
        if retained_bytes > self.capacity_bytes:
            raise ValueError(t(ERR_TERMINAL_OUTPUT_CAPACITY_EXCEEDED))
        if retained_bytes == 0 and self.next_sequence != self.oldest_sequence:
            raise ValueError(t(ERR_TERMINAL_EMPTY_BUFFER_SEQUENCE_INVALID))
        if retained_bytes > 0 and self.next_sequence <= self.oldest_sequence:
            raise ValueError(t(ERR_TERMINAL_NONEMPTY_BUFFER_SEQUENCE_INVALID))
        return self


class TerminalOutputReadStatus(StrEnum):
    OK = "ok"
    EMPTY = "empty"
    TRUNCATED = "truncated"
    EXPIRED = "expired"


class TerminalReadResult(TerminalProtocolModel):
    """A merged stdout/stderr read result addressed by raw-byte offset.

    Clients continue with next_offset. Sequence is the highest output-chunk
    sequence appended in this read snapshot; it is zero before any output, and
    actual chunks start at one and increase strictly. Pagination uses only
    offsets, while sequence detects ordering and gaps. An offset beyond
    latest_offset is rejected by the execution layer and must not produce a
    result. Truncated means bounded retention discarded old data; expired means
    the output retention period has ended.
    """

    terminal_session_id: TerminalSessionId
    read_status: TerminalOutputReadStatus
    requested_offset: int = Field(ge=0)
    start_offset: int = Field(ge=0)
    next_offset: int = Field(ge=0)
    oldest_available_offset: int = Field(ge=0)
    latest_offset: int = Field(ge=0)
    sequence: int = Field(ge=0)
    output: str
    eof: bool

    @model_validator(mode="after")
    def validate_read_status_semantics(self) -> "TerminalReadResult":
        if self.requested_offset > self.latest_offset:
            raise ValueError(t(ERR_TERMINAL_READ_OFFSET_AHEAD))
        if not (self.oldest_available_offset <= self.start_offset <= self.next_offset <= self.latest_offset):
            raise ValueError(t(ERR_TERMINAL_READ_OFFSET_ORDER_INVALID))

        if self.read_status is TerminalOutputReadStatus.TRUNCATED:
            if not (self.requested_offset < self.oldest_available_offset and self.start_offset == self.oldest_available_offset and self.next_offset > self.start_offset and self.sequence >= 1 and self.output != ""):
                raise ValueError(t(ERR_TERMINAL_READ_TRUNCATED_INVALID))
        elif self.read_status is TerminalOutputReadStatus.OK:
            if not (self.oldest_available_offset <= self.requested_offset < self.latest_offset and self.start_offset == self.requested_offset and self.next_offset > self.start_offset and self.sequence >= 1 and self.output != ""):
                raise ValueError(t(ERR_TERMINAL_READ_OK_INVALID))
        elif self.read_status is TerminalOutputReadStatus.EMPTY:
            if not (self.requested_offset == self.latest_offset and self.start_offset == self.next_offset == self.latest_offset and self.output == ""):
                raise ValueError(t(ERR_TERMINAL_READ_EMPTY_INVALID))
        elif self.read_status is TerminalOutputReadStatus.EXPIRED:
            if not (self.oldest_available_offset == self.latest_offset and self.start_offset == self.next_offset == self.latest_offset and self.output == "" and self.eof):
                raise ValueError(t(ERR_TERMINAL_READ_EXPIRED_INVALID))
        return self


class TerminalWriteResult(TerminalActionReceipt):
    """Receipt for a write followed by an optional merged-output read."""

    action: Literal[TerminalAction.WRITE] = TerminalAction.WRITE
    bytes_written: int = Field(ge=0)
    read_offset: int = Field(ge=0)
    read_timed_out: bool
    read_result: TerminalReadResult | None = None


class TerminalSessionSnapshot(TerminalProtocolModel):
    terminal_session_id: TerminalSessionId
    status: TerminalSessionStatus
    permission_scope: TerminalPermissionScope
    output_buffer: TerminalOutputBufferState
    exit_code: int | None = None
    failure_reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_terminal_outcome(self) -> "TerminalSessionSnapshot":
        _validate_terminal_outcome(self.status, self.exit_code, self.failure_reason)
        return self


def _validate_terminal_outcome(
    status: TerminalSessionStatus,
    exit_code: int | None,
    failure_reason: str | None,
) -> None:
    if status is TerminalSessionStatus.EXITED:
        if exit_code is None or failure_reason is not None:
            raise ValueError(t(ERR_TERMINAL_EXITED_OUTCOME_INVALID))
    elif status in {TerminalSessionStatus.FAILED, TerminalSessionStatus.LOST}:
        if exit_code is not None or not failure_reason or not failure_reason.strip():
            raise ValueError(t(ERR_TERMINAL_FAILURE_OUTCOME_INVALID))
    elif exit_code is not None or failure_reason is not None:
        raise ValueError(t(ERR_TERMINAL_ACTIVE_OUTCOME_INVALID))


class ShellNonInteractiveCompletedResult(TerminalProtocolModel):
    stdout: str
    stderr: str
    exit_code: int
    system_info: str


class ShellNonInteractiveTimeoutResult(TerminalProtocolModel):
    error: str = Field(min_length=1)
    system_info: str


class ShellInteractiveHandoffResult(TerminalProtocolModel):
    terminal_session_id: TerminalSessionId
    status: TerminalSessionStatus
    output_buffer: TerminalOutputBufferState
    output_stream: Literal["merged_stdout_stderr"] = "merged_stdout_stderr"
    exit_code: int | None = None
    failure_reason: str | None = Field(default=None, min_length=1)

    @model_validator(mode="after")
    def validate_terminal_outcome(self) -> "ShellInteractiveHandoffResult":
        _validate_terminal_outcome(self.status, self.exit_code, self.failure_reason)
        return self


type ShellExecutionResult = ShellNonInteractiveCompletedResult | ShellNonInteractiveTimeoutResult | ShellInteractiveHandoffResult


__all__ = [
    "ALL_TERMINAL_ACTIONS",
    "TERMINAL_SESSION_FINAL_STATUSES",
    "TERMINAL_STATUS_TRANSITIONS",
    "TerminalAction",
    "TerminalActionReceipt",
    "TerminalCloseRequest",
    "TerminalMutatingRequestId",
    "TerminalOutputBufferState",
    "TerminalOutputReadStatus",
    "TerminalPermissionScope",
    "TerminalProtocolModel",
    "TerminalReadRequest",
    "TerminalReadResult",
    "TerminalRequest",
    "TerminalResizeRequest",
    "TerminalSessionId",
    "TerminalSessionSnapshot",
    "TerminalSessionStatus",
    "TerminalStatusRequest",
    "TerminalWriteRequest",
    "TerminalWriteResult",
    "can_transition_terminal_status",
    "generate_terminal_request_id",
    "generate_terminal_session_id",
    "ShellExecutionMode",
    "ShellExecutionResult",
    "ShellInteractiveHandoffResult",
    "ShellNonInteractiveCompletedResult",
    "ShellNonInteractiveTimeoutResult",
    "validate_shell_execution_mode",
    "validate_terminal_status_transition",
]
