"""Polling observer for interactive terminal sessions."""

import asyncio
import hashlib
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.core.constants import (
    ERR_TERMINAL_ACTION_NOT_ALLOWED,
    ERR_TERMINAL_READ_OFFSET_AHEAD,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
    TERMINAL_WATCH_POLL_INTERVAL_SECONDS,
)
from app.core.crud.task.background import background_task_crud
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


@dataclass
class _WatchState:
    request: TerminalWatchRequest
    task_id: int
    cursor: int
    last_seen_offset: int | None = None
    first_output_time: datetime | None = None
    last_change_time: datetime | None = None
    suffix: str = ""
    output_truncated: bool = False


@dataclass
class _ReadObservation:
    read_result: TerminalReadResult | None = None
    matched_text: str | None = None
    match_context: str | None = None
    error: str | None = None


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


def _state_time(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _restore_state(request: TerminalWatchRequest, task_id: int, payload: Any) -> _WatchState:
    state = _WatchState(request=request, task_id=task_id, cursor=request.after_offset)
    if not isinstance(payload, dict):
        return state

    cursor = payload.get("cursor")
    if isinstance(cursor, int) and not isinstance(cursor, bool) and cursor >= request.after_offset:
        state.cursor = cursor
    last_seen_offset = payload.get("last_seen_offset")
    if isinstance(last_seen_offset, int) and not isinstance(last_seen_offset, bool) and last_seen_offset >= 0:
        state.last_seen_offset = last_seen_offset
    suffix = payload.get("suffix")
    if isinstance(suffix, str):
        suffix_size = max(0, len(request.text or "") - 1)
        state.suffix = suffix[-suffix_size:] if suffix_size else ""
    state.first_output_time = _state_time(payload.get("first_output_time"))
    state.last_change_time = _state_time(payload.get("last_change_time"))
    state.output_truncated = payload.get("output_truncated") is True
    return state


def _serialize_state(state: _WatchState) -> dict[str, Any]:
    return {
        "cursor": state.cursor,
        "suffix": state.suffix,
        "first_output_time": state.first_output_time.isoformat() if state.first_output_time is not None else None,
        "last_change_time": state.last_change_time.isoformat() if state.last_change_time is not None else None,
        "output_truncated": state.output_truncated,
        "last_seen_offset": state.last_seen_offset,
    }


def _observe_snapshot(state: _WatchState, snapshot: TerminalSessionSnapshot) -> bool:
    bounds = snapshot.output_buffer
    previous_offset = state.request.after_offset if state.last_seen_offset is None else state.last_seen_offset
    if state.request.after_offset > bounds.next_offset and (state.last_seen_offset is None or state.request.after_offset > state.last_seen_offset):
        raise ParameterException(ERR_TERMINAL_READ_OFFSET_AHEAD)

    new_output = bounds.next_offset > previous_offset
    if bounds.oldest_offset > state.cursor:
        state.output_truncated = True
        state.cursor = bounds.oldest_offset
        state.suffix = ""
    state.last_seen_offset = max(previous_offset, bounds.next_offset)
    return new_output


def _condition_match(
    state: _WatchState,
    snapshot: TerminalSessionSnapshot,
    now: datetime,
    new_output: bool,
    condition: str,
) -> str | None:
    if condition == "output":
        if new_output and state.first_output_time is None:
            state.first_output_time = now
        if state.first_output_time is not None and now >= state.first_output_time + timedelta(seconds=state.request.settle_seconds):
            return "output"
    elif condition == "quiet_for":
        if new_output:
            state.last_change_time = now
        if state.last_change_time is not None and snapshot.output_buffer.next_offset > state.cursor and now >= state.last_change_time + timedelta(seconds=state.request.settle_seconds):
            return "quiet_for"
    return None


async def _read_page(
    *,
    state: _WatchState,
    uid: str,
    session_id: str,
    offset: int,
    timeout_seconds: float,
    scan: bool,
) -> _ReadObservation:
    try:
        read_result = await _read(
            task_id=state.task_id,
            uid=uid,
            session_id=session_id,
            terminal_session_id=state.request.terminal_session_id,
            offset=offset,
            max_bytes=state.request.max_bytes,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:
        return _ReadObservation(error=str(exc) or exc.__class__.__name__)

    state.last_seen_offset = max(
        state.last_seen_offset if state.last_seen_offset is not None else state.request.after_offset,
        read_result.latest_offset,
    )
    read_gap = read_result.read_status in {
        TerminalOutputReadStatus.TRUNCATED,
        TerminalOutputReadStatus.EXPIRED,
    }
    if read_gap:
        state.output_truncated = True
        state.suffix = ""

    observation = _ReadObservation(read_result=read_result)
    if not scan:
        if read_result.start_offset > state.cursor:
            state.output_truncated = True
        return observation

    contiguous = not read_gap and read_result.start_offset == state.cursor
    if read_result.start_offset > state.cursor:
        state.output_truncated = True
    text = state.request.text or ""
    combined = state.suffix + read_result.output if contiguous else read_result.output
    if text in combined:
        observation.matched_text = text
        observation.match_context = state.suffix if contiguous else ""
    state.cursor = read_result.next_offset
    if observation.matched_text is None:
        suffix_size = max(0, len(text) - 1)
        state.suffix = combined[-suffix_size:] if suffix_size else ""
    else:
        state.suffix = ""
    return observation


def _available_offset(state: _WatchState, snapshot: TerminalSessionSnapshot) -> int:
    return max(
        snapshot.output_buffer.next_offset,
        state.last_seen_offset if state.last_seen_offset is not None else state.request.after_offset,
    )


def _read_offset(state: _WatchState, snapshot: TerminalSessionSnapshot, *, tail: bool) -> int | None:
    latest_offset = _available_offset(state, snapshot)
    if latest_offset <= state.cursor or latest_offset <= snapshot.output_buffer.oldest_offset:
        return None
    if not tail:
        return state.cursor
    return max(
        state.cursor,
        snapshot.output_buffer.oldest_offset,
        latest_offset - state.request.max_bytes,
    )


def _build_result(
    *,
    state: _WatchState,
    snapshot: TerminalSessionSnapshot,
    event: str,
    matched: bool,
    observation: _ReadObservation | None,
    extended: bool,
    condition: str | None = None,
    error: str | None = None,
    output_read_error: bool = False,
) -> dict[str, Any]:
    read_result = observation.read_result if observation is not None else None
    output = read_result.output if read_result is not None else ""
    start_offset = read_result.start_offset if read_result is not None else state.cursor
    next_offset = read_result.next_offset if read_result is not None else state.cursor
    latest_offset = read_result.latest_offset if read_result is not None else _available_offset(state, snapshot)
    output_truncated = state.output_truncated
    if read_result is not None:
        output_truncated = output_truncated or read_result.read_status in {
            TerminalOutputReadStatus.TRUNCATED,
            TerminalOutputReadStatus.EXPIRED,
        }
        output_truncated = output_truncated or read_result.next_offset < read_result.latest_offset

    eof = event in {"process_exit", "terminal_error"}
    if read_result is not None and not eof:
        eof = read_result.eof
    if event == "observation_error":
        eof = False
    result = {
        "terminal_session_id": snapshot.terminal_session_id,
        "event": event,
        "after_offset": state.request.after_offset,
        "session_status": snapshot.status.value,
        "exit_code": snapshot.exit_code,
        "failure_reason": snapshot.failure_reason,
        "output": output,
        "start_offset": start_offset,
        "next_offset": next_offset,
        "latest_offset": latest_offset,
        "output_truncated": output_truncated,
        "eof": eof,
    }
    if extended:
        result["condition"] = condition or state.request.condition
        result["matched"] = matched
    if observation is not None and observation.matched_text is not None:
        result["matched_text"] = observation.matched_text
        result["match_context"] = observation.match_context or ""
    if error is not None:
        result["error"] = error
    if output_read_error:
        result["output_read_error"] = True
    return result


async def watch_terminal(
    *,
    task_id: int,
    uid: str,
    session_id: str,
    request: TerminalWatchRequest,
    deadline: datetime,
    read_on_timeout: bool = False,
    idle_timeout: float | None = None,
    subscription_profile_id: int | None = None,
    subscription_states: dict | None = None,
) -> dict:
    coordinator_mode = idle_timeout is not None or subscription_profile_id is not None or subscription_states is not None
    main_state = _WatchState(request=request, task_id=task_id, cursor=request.after_offset)
    subscription_state_by_id: dict[int, _WatchState] = {}
    deadline = deadline.replace(tzinfo=None)
    last_output_offset = request.after_offset

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
            subscription_tasks = []
            if subscription_profile_id is not None:
                subscription_tasks = await background_task_crud.list_terminal_watch_subscriptions(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=subscription_profile_id,
                    terminal_session_id=request.terminal_session_id,
                )
            await db.commit()

        now = get_local_time().replace(tzinfo=None)
        if idle_timeout is not None and snapshot.output_buffer.next_offset > last_output_offset:
            last_output_offset = snapshot.output_buffer.next_offset
            deadline = now + timedelta(seconds=idle_timeout)

        active_states: list[tuple[int | None, _WatchState]] = [(None, main_state)]
        for subscription_task in subscription_tasks:
            subscription_task_id = subscription_task.id
            subscription_request = TerminalWatchRequest.model_validate(subscription_task.arguments)
            state = subscription_state_by_id.get(subscription_task_id)
            if state is None:
                payload = subscription_states.get(str(subscription_task_id)) if subscription_states is not None else None
                state = _restore_state(subscription_request, subscription_task_id, payload)
                subscription_state_by_id[subscription_task_id] = state
            else:
                state.request = subscription_request
            active_states.append((subscription_task_id, state))

        final_event = None
        if snapshot.status in TERMINAL_SESSION_FINAL_STATUSES:
            final_event = "process_exit" if snapshot.status is TerminalSessionStatus.EXITED else "terminal_error"
        deadline_expired = now >= deadline
        legacy_timeout = not coordinator_mode and final_event is None and deadline_expired
        observations: dict[int | None, _ReadObservation] = {}
        matched: dict[int | None, str] = {}
        errors: dict[int | None, str] = {}

        for watch_id, state in active_states:
            new_output = _observe_snapshot(state, snapshot)
            is_main = watch_id is None
            condition = "process_exit" if is_main and coordinator_mode else state.request.condition

            if condition == "process_exit":
                if final_event == "process_exit":
                    matched[watch_id] = "process_exit"
                continue

            if not (is_main and legacy_timeout):
                condition_event = _condition_match(state, snapshot, now, new_output, condition)
                if condition_event is not None:
                    matched[watch_id] = condition_event

            if condition != "output_contains":
                continue
            scan_allowed = not deadline_expired or (final_event is not None and not is_main)
            if is_main and not coordinator_mode and final_event is not None:
                scan_allowed = False
            if not scan_allowed:
                continue
            read_offset = _read_offset(state, snapshot, tail=False)
            if read_offset is None:
                continue
            timeout_seconds = (
                5.0
                if final_event is not None
                else min(
                    5.0,
                    max(0.0, (deadline - get_local_time().replace(tzinfo=None)).total_seconds()),
                )
            )
            observation = await _read_page(
                state=state,
                uid=uid,
                session_id=session_id,
                offset=read_offset,
                timeout_seconds=timeout_seconds,
                scan=True,
            )
            observations[watch_id] = observation
            if observation.error is not None:
                errors[watch_id] = observation.error
            elif observation.matched_text is not None:
                matched[watch_id] = "output_contains"

        if final_event is not None:
            event = final_event
        elif errors:
            event = "observation_error"
        elif None in matched:
            event = matched[None]
        elif any(watch_id is not None for watch_id in matched):
            event = next(matched[watch_id] for watch_id, _ in active_states if watch_id is not None and watch_id in matched)
        elif deadline_expired:
            event = "timeout"
        else:
            await asyncio.sleep(TERMINAL_WATCH_POLL_INTERVAL_SECONDS)
            continue

        if event != "observation_error":
            read_targets: list[tuple[int | None, _WatchState]] = []
            if event == "timeout":
                if read_on_timeout:
                    read_targets.append((None, main_state))
            elif final_event is not None:
                read_targets.extend(active_states)
            else:
                read_targets.append((None, main_state))
                read_targets.extend((watch_id, state) for watch_id, state in active_states if watch_id is not None and watch_id in matched)

            for watch_id, state in read_targets:
                if watch_id in observations:
                    continue
                read_offset = _read_offset(state, snapshot, tail=True)
                if read_offset is None:
                    continue
                timeout_seconds = (
                    5.0
                    if event in {"timeout", "process_exit", "terminal_error"}
                    else min(
                        5.0,
                        max(0.0, (deadline - get_local_time().replace(tzinfo=None)).total_seconds()),
                    )
                )
                observation = await _read_page(
                    state=state,
                    uid=uid,
                    session_id=session_id,
                    offset=read_offset,
                    timeout_seconds=timeout_seconds,
                    scan=False,
                )
                observations[watch_id] = observation
                if observation.error is not None:
                    errors[watch_id] = observation.error

        if final_event is not None:
            event = final_event
        elif errors:
            event = "observation_error"
        error = next(iter(errors.values())) if errors else None
        terminal_read_error = final_event is not None and bool(errors)
        main_condition = "process_exit" if coordinator_mode else request.condition
        result = _build_result(
            state=main_state,
            snapshot=snapshot,
            event=event,
            matched=None in matched,
            observation=observations.get(None),
            extended=coordinator_mode,
            condition=main_condition,
            error=error,
            output_read_error=terminal_read_error,
        )
        if not coordinator_mode:
            return result

        result["observed_at"] = get_local_time().isoformat()
        result["matched_watch_task_ids"] = sorted(watch_id for watch_id, _ in active_states if watch_id is not None and watch_id in matched)
        watch_results: dict[str, dict[str, Any]] = {}
        for watch_id, state in active_states:
            if watch_id is None:
                continue
            if final_event is None and watch_id not in matched and watch_id not in errors:
                continue
            child_error = errors.get(watch_id)
            child_event = final_event or ("observation_error" if child_error is not None else matched[watch_id])
            watch_results[str(watch_id)] = _build_result(
                state=state,
                snapshot=snapshot,
                event=child_event,
                matched=watch_id in matched,
                observation=observations.get(watch_id),
                extended=True,
                error=child_error,
                output_read_error=final_event is not None and child_error is not None,
            )
        result["watch_results"] = watch_results
        result["watch_states"] = {str(watch_id): _serialize_state(state) for watch_id, state in active_states if watch_id is not None and watch_id not in matched} if subscription_profile_id is not None and final_event is None else {}
        return result
