"""Polling observer for interactive terminal sessions."""

import asyncio
import hashlib
from datetime import datetime, timedelta

from app.core.constants import (
    ERR_TERMINAL_ACTION_NOT_ALLOWED,
    ERR_TERMINAL_READ_OFFSET_AHEAD,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
    TERMINAL_WATCH_POLL_INTERVAL_SECONDS,
)
from app.core.exceptions import ForbiddenException, ParameterException
from app.core.i18n import t
from app.core.terminal.manager import terminal_session_manager
from app.core.terminal.schemas import (
    TERMINAL_SESSION_FINAL_STATUSES,
    TerminalAction,
    TerminalOutputReadStatus,
    TerminalReadRequest,
    TerminalReadResult,
    TerminalSessionSnapshot,
    TerminalSessionStatus,
)
from app.core.terminal.watch_schema import TerminalWatchRequest
from app.core.utils.time import get_local_time
from app.providers.database import AsyncSessionLocal

__all__ = ["watch_terminal"]


async def _read(
    *,
    task_id: int,
    uid: str,
    session_id: str,
    terminal_session_id: str,
    offset: int,
    max_bytes: int,
    timeout_seconds: float,
) -> TerminalReadResult:
    request = TerminalReadRequest(
        terminal_session_id=terminal_session_id,
        offset=offset,
        max_bytes=max_bytes,
    )
    request_id = hashlib.sha256(f"watch:{task_id}:{offset}:{max_bytes}".encode()).hexdigest()
    async with AsyncSessionLocal() as db:
        try:
            command, _ = await terminal_session_manager.enqueue_read(
                db,
                uid,
                session_id,
                request,
                request_id,
            )
            if command.id is None:
                raise RuntimeError(t(ERR_TOOL_RUNTIME_CONTEXT_MISSING))
            result = await terminal_session_manager.wait_for_command_result(
                db,
                command.id,
                timeout_seconds,
            )
            return TerminalReadResult.model_validate(result)
        finally:
            await db.commit()


async def watch_terminal(
    *,
    task_id: int,
    uid: str,
    session_id: str,
    request: TerminalWatchRequest,
    deadline: datetime,
) -> dict:
    after_offset = request.after_offset
    cursor = after_offset
    last_seen_offset: int | None = None
    first_output_time: datetime | None = None
    last_change_time: datetime | None = None
    suffix = ""
    deadline = deadline.replace(tzinfo=None)
    while True:
        async with AsyncSessionLocal() as db:
            snapshot: TerminalSessionSnapshot = await terminal_session_manager.get_snapshot(
                db,
                request.terminal_session_id,
                uid,
                session_id,
            )
            if not snapshot.permission_scope.permits(uid, session_id, TerminalAction.READ):
                raise ForbiddenException(
                    ERR_TERMINAL_ACTION_NOT_ALLOWED,
                    terminal_session_id=request.terminal_session_id,
                    action=TerminalAction.READ.value,
                )
            if after_offset > snapshot.output_buffer.next_offset and (last_seen_offset is None or after_offset > last_seen_offset):
                raise ParameterException(ERR_TERMINAL_READ_OFFSET_AHEAD)
            await db.commit()
        now = get_local_time().replace(tzinfo=None)
        bounds = snapshot.output_buffer
        previous_seen = after_offset if last_seen_offset is None else last_seen_offset
        latest_offset = max(previous_seen, bounds.next_offset)
        new_output = bounds.next_offset > previous_seen
        last_seen_offset = latest_offset
        event: str | None = None
        output_truncated = False
        read_offset: int | None = None
        scan_page = False
        read_result: TerminalReadResult | None = None
        matched_text: str | None = None
        match_context: str | None = None
        if snapshot.status in TERMINAL_SESSION_FINAL_STATUSES:
            event = "process_exit" if snapshot.status is TerminalSessionStatus.EXITED else "terminal_error"
            output_truncated = bounds.oldest_offset > cursor
        elif now >= deadline:
            event = "timeout"
        elif bounds.oldest_offset > cursor:
            event = "output_gap"
            output_truncated = True
        elif request.condition == "output":
            if new_output and first_output_time is None:
                first_output_time = now
            if first_output_time is not None and now >= first_output_time + timedelta(seconds=request.settle_seconds):
                event = "output"
        elif request.condition == "quiet_for":
            if new_output:
                last_change_time = now
            if last_change_time is not None and bounds.next_offset > cursor and now >= last_change_time + timedelta(seconds=request.settle_seconds):
                event = "quiet_for"
        elif request.condition == "output_contains" and latest_offset > cursor:
            scan_page = True
            read_offset = cursor

        if not scan_page and event is not None and event != "timeout" and now < deadline and latest_offset > cursor and latest_offset > bounds.oldest_offset:
            read_offset = max(
                cursor,
                bounds.oldest_offset,
                latest_offset - request.max_bytes,
            )
        if read_offset is not None:
            remaining = max(0.0, (deadline - get_local_time().replace(tzinfo=None)).total_seconds())
            try:
                read_result = await _read(
                    task_id=task_id,
                    uid=uid,
                    session_id=session_id,
                    terminal_session_id=request.terminal_session_id,
                    offset=read_offset,
                    max_bytes=request.max_bytes,
                    timeout_seconds=min(5.0, remaining),
                )
            except TimeoutError:
                event = "observation_error"
            else:
                last_seen_offset = max(last_seen_offset, read_result.latest_offset)
                if read_result.read_status in {
                    TerminalOutputReadStatus.TRUNCATED,
                    TerminalOutputReadStatus.EXPIRED,
                }:
                    event = "output_gap"
                    output_truncated = True
                elif scan_page:
                    text = request.text or ""
                    combined = suffix + read_result.output
                    if text in combined:
                        event = "output_contains"
                        matched_text = text
                        match_context = suffix
                        output_truncated = output_truncated or read_result.start_offset > after_offset
                    else:
                        cursor = read_result.next_offset
                        suffix = combined[-(len(text) - 1) :] if len(text) > 1 else ""
                        event = None
                elif read_result.start_offset > cursor:
                    output_truncated = True

        if event is None:
            await asyncio.sleep(TERMINAL_WATCH_POLL_INTERVAL_SECONDS)
            continue
        output = ""
        start_offset = cursor
        next_offset = cursor
        latest = snapshot.output_buffer.next_offset
        eof = event in {"process_exit", "terminal_error"}
        if read_result is not None:
            output = read_result.output
            start_offset = read_result.start_offset
            next_offset = read_result.next_offset
            latest = read_result.latest_offset
            eof = read_result.eof
            output_truncated = output_truncated or read_result.read_status in {
                TerminalOutputReadStatus.TRUNCATED,
                TerminalOutputReadStatus.EXPIRED,
            }
            output_truncated = output_truncated or read_result.next_offset < read_result.latest_offset
        if event == "observation_error":
            eof = False
        result = {
            "terminal_session_id": snapshot.terminal_session_id,
            "event": event,
            "after_offset": after_offset,
            "session_status": snapshot.status.value,
            "exit_code": snapshot.exit_code,
            "failure_reason": snapshot.failure_reason,
            "output": output,
            "start_offset": start_offset,
            "next_offset": next_offset,
            "latest_offset": latest,
            "output_truncated": output_truncated,
            "eof": eof,
        }
        if matched_text is not None:
            result["matched_text"] = matched_text
            result["match_context"] = match_context or ""
        return result
