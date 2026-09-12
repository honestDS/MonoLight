from typing import Any

from app.core.constants import (
    ERR_TERMINAL_READ_OFFSET_AHEAD,
)
from app.core.i18n import t
from app.core.terminal.schemas import (
    TerminalOutputBufferState,
    TerminalOutputReadStatus,
    TerminalReadResult,
)
from app.models.terminal_session import TerminalSession

__all__ = []


def _terminal_output_buffer(terminal_session: TerminalSession) -> TerminalOutputBufferState:
    return TerminalOutputBufferState(
        capacity_bytes=terminal_session.output_capacity_bytes,
        oldest_offset=terminal_session.oldest_output_offset,
        next_offset=terminal_session.next_output_offset,
        oldest_sequence=terminal_session.oldest_output_sequence,
        next_sequence=terminal_session.next_output_sequence,
    )


def _read_without_driver_result(
    terminal_session_id: str,
    output_buffer: TerminalOutputBufferState,
    payload: dict[str, Any],
) -> dict[str, Any]:
    offset = payload["offset"]
    latest_offset = output_buffer.next_offset
    if offset > latest_offset:
        raise ValueError(t(ERR_TERMINAL_READ_OFFSET_AHEAD))

    if offset == latest_offset and output_buffer.oldest_offset == latest_offset:
        result = TerminalReadResult(
            terminal_session_id=terminal_session_id,
            read_status=TerminalOutputReadStatus.EMPTY,
            requested_offset=offset,
            start_offset=latest_offset,
            next_offset=latest_offset,
            oldest_available_offset=latest_offset,
            latest_offset=latest_offset,
            sequence=0,
            output="",
            eof=True,
        )
    else:
        result = TerminalReadResult(
            terminal_session_id=terminal_session_id,
            read_status=TerminalOutputReadStatus.EXPIRED,
            requested_offset=offset,
            start_offset=latest_offset,
            next_offset=latest_offset,
            oldest_available_offset=latest_offset,
            latest_offset=latest_offset,
            sequence=max(0, output_buffer.next_sequence - 1),
            output="",
            eof=True,
        )
    return result.model_dump(mode="json")


class _TerminalLeaseLost(Exception):
    pass
