import hashlib
import json
from collections import Counter, deque
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION,
    ERR_SESSION_REPLY_CHECKPOINT_INVALID,
    TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.event import session_event_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.background import background_task_crud
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.prompts import PROMPT_TOOL_INTERRUPTED, PROMPT_TOOL_RESULT_UNKNOWN_AFTER_REPLY_FAILURE
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTask
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType

__all__ = ["complete_cancelled_tool_results", "complete_cancelled_tool_results_for_work"]


def _json_object(content: str | None) -> dict[str, Any] | None:
    try:
        payload = json.loads(content or "{}")
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _get_queued_task_id(queued_result: Message, original_call_id: str) -> int | None:
    if queued_result.role != MessageRole.TOOL or queued_result.type != MessageType.TOOL_RESULT:
        return None
    wrapper = _json_object(queued_result.content)
    if wrapper is None or wrapper.get("tool_call_id") != original_call_id:
        return None
    queued_payload = _json_object(wrapper.get("content") if isinstance(wrapper.get("content"), str) else None)
    task_id = queued_payload.get("task_id") if queued_payload is not None and queued_payload.get("status") == "queued" else None
    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
        return None
    return task_id


async def _load_registered_sources(db: AsyncSession, *, work: SessionReplyWorkItem, source_ids: Any, message_work_id: int | None) -> dict[int, list[InternalToolCall]]:
    if not isinstance(source_ids, list):
        raise ParameterException(ERR_SESSION_REPLY_CHECKPOINT_INVALID)

    sources: dict[int, list[InternalToolCall]] = {}
    for source_id in source_ids:
        source = await message_crud.get(db, source_id)
        if source is None or source.uid != work.uid or source.session_id != work.session_id or source.profile_id != work.profile_id or source.role != MessageRole.ASSISTANT or source.type != MessageType.TOOL_CALL or source.work_id != message_work_id:
            raise ParameterException(ERR_SESSION_REPLY_CHECKPOINT_INVALID)
        try:
            source_message = InternalMessage.model_validate_json(source.content or "{}")
        except (TypeError, ValueError) as exc:
            raise ParameterException(ERR_SESSION_REPLY_CHECKPOINT_INVALID) from exc
        calls: list[InternalToolCall] = []
        for call in source_message.tool_calls or []:
            if call.id.strip():
                calls.append(call)
        if calls:
            sources[source_id] = calls
    return sources


async def _load_saved_tool_results(db: AsyncSession, *, work: SessionReplyWorkItem, message_work_id: int | None, sources: dict[int, list[InternalToolCall]]) -> dict[tuple[int, str], deque[Message]]:
    result_queues: dict[tuple[int, str], deque[Message]] = {}
    call_counts: Counter[tuple[int, str]] = Counter()
    for source_id, calls in sources.items():
        for call in calls:
            call_counts[(source_id, call.id)] += 1
    if not call_counts:
        return result_queues
    call_ids = {call_id for _source_id, call_id in call_counts}
    total_calls = sum(call_counts.values())

    current_source: dict[str, int] = {}
    received = 0
    page_after_id: int | None = None
    first_source_id = min(sources)
    while received < total_calls:
        page = await message_crud.get_history_forward_by_id(
            db,
            session_id=work.session_id,
            uid=work.uid,
            work_id=message_work_id,
            profile_id=work.profile_id,
            message_types=(MessageType.TOOL_CALL, MessageType.TOOL_RESULT),
            lock_for_update=True,
            after_id=first_source_id - 1,
            page_after_id=page_after_id,
            limit=500,
        )
        if not page:
            break

        for message in page:
            if message.role == MessageRole.ASSISTANT and message.type == MessageType.TOOL_CALL:
                payload = _json_object(message.content)
                raw_calls = payload.get("tool_calls") if payload is not None else None
                if isinstance(raw_calls, list):
                    for raw_call in raw_calls:
                        call_id = raw_call.get("id") if isinstance(raw_call, dict) else None
                        if isinstance(call_id, str) and call_id.strip() and call_id in call_ids:
                            current_source[call_id] = message.id
                continue
            if message.role != MessageRole.TOOL or message.type != MessageType.TOOL_RESULT:
                continue

            payload = _json_object(message.content)
            call_id = payload.get("tool_call_id") if payload is not None else None
            if not isinstance(call_id, str) or not call_id.strip() or call_id not in call_ids:
                continue

            metadata = message.provider_metadata
            if isinstance(metadata, dict) and TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY in metadata:
                source_id = metadata.get(TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY)
                if not isinstance(source_id, int) or isinstance(source_id, bool) or source_id not in sources:
                    continue
            else:
                source_id = current_source.get(call_id)
            key = (source_id, call_id)
            call_count = call_counts.get(key)
            if call_count is None:
                continue
            queue = result_queues.setdefault(key, deque())
            if len(queue) >= call_count:
                continue
            queue.append(message)
            received += 1
            if received >= total_calls:
                break

        if received >= total_calls or len(page) < 500:
            break
        page_after_id = page[-1].id
    return result_queues


def _cancelled_result_dedupe_key(source_message_id: int, call_index: int, tool_call_id: str) -> str:
    return hashlib.sha256(json.dumps(["tool-cancellation", source_message_id, call_index, tool_call_id], ensure_ascii=False, separators=(",", ":")).encode("utf-8")).hexdigest()


async def _save_cancelled_background_result_marker(
    db: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    message_work_id: int | None,
    queued_result_id: int,
    tool_call: InternalToolCall,
    task: BackgroundTask,
) -> tuple[int, dict[str, object]] | None:
    dedupe_key = f"background-task:{task.id}:result"
    if await message_crud.get_by_dedupe_key(db, dedupe_key) is not None:
        return None

    started = task.started_at is not None
    marker_payload = {
        "type": "background_tool_result",
        "ui_only": True,
        "task_id": task.id,
        "original_tool_result_message_id": queued_result_id,
        "tool_call": {
            "id": tool_call.id,
            "name": tool_call.name,
            "arguments": dict(task.arguments or {}),
        },
        "task": {
            "status": "cancelled",
            "error": PROMPT_TOOL_INTERRUPTED if started else t(ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION),
        },
        "execution_status": "execution_unknown" if started else "failed",
        "tool_cancelled": True,
    }
    marker = await save_message(
        db,
        work.session_id,
        work.uid,
        MessageRole.USER,
        MessageType.BACKGROUND_TASK_RESULT,
        json.dumps(marker_payload, ensure_ascii=False),
        work.profile_id,
        is_processed=True,
        dedupe_key=dedupe_key,
        created_at=task.finished_at or work.updated_at,
        commit=False,
        work_id=message_work_id,
    )
    if marker.id is None:
        raise ParameterException(ERR_SESSION_REPLY_CHECKPOINT_INVALID)
    marker_content = marker.content if isinstance(marker.content, str) else json.dumps(marker_payload, ensure_ascii=False)
    return marker.id, {
        "id": marker.id,
        "db_id": marker.id,
        "role": MessageRole.ASSISTANT.value,
        "type": MessageType.BACKGROUND_TASK_RESULT.value,
        "content": marker_content,
        "created_at": marker.created_at,
    }


async def _create_cancelled_background_result_marker(
    db: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    message_work_id: int | None,
    queued_result: Message,
    tool_call: InternalToolCall,
    cancelled_tasks: list[BackgroundTask],
) -> tuple[int, dict[str, object]] | None:
    if queued_result.id is None:
        return None
    task_id = _get_queued_task_id(queued_result, tool_call.id)
    if task_id is None:
        return None
    matching_tasks = [task for task in cancelled_tasks if task.id == task_id]
    if len(matching_tasks) != 1:
        return None
    task = matching_tasks[0]
    if task.tool_call_id != tool_call.id or task.tool_name != tool_call.name:
        return None
    return await _save_cancelled_background_result_marker(
        db,
        work=work,
        message_work_id=message_work_id,
        queued_result_id=queued_result.id,
        tool_call=tool_call,
        task=task,
    )


async def _create_confirmed_cancelled_background_result_markers(
    db: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    message_work_id: int | None,
    queued_result_ids: Any,
    cancelled_tasks: list[BackgroundTask],
) -> tuple[list[int], list[dict[str, object]]]:
    if queued_result_ids is not None and (not isinstance(queued_result_ids, list) or not queued_result_ids):
        return [], []
    try:
        audit_record_id = int(work.source_id)
    except (TypeError, ValueError):
        return [], []
    if audit_record_id <= 0:
        return [], []

    record = await audit_crud.get_record(db, audit_record_id)
    if record is None or record.uid != work.uid or record.session_id != work.session_id:
        return [], []
    source_assistant_message_id = record.source_assistant_message_id
    if isinstance(source_assistant_message_id, bool) or not isinstance(source_assistant_message_id, int) or source_assistant_message_id <= 0:
        return [], []
    source_message = await message_crud.get(db, source_assistant_message_id)
    if source_message is None:
        return [], []
    if source_message.uid != work.uid or source_message.session_id != work.session_id:
        return [], []
    if source_message.profile_id != work.profile_id or source_message.role != MessageRole.ASSISTANT:
        return [], []
    if source_message.type != MessageType.TOOL_CALL or (source_message.work_id is None) != (message_work_id is None):
        return [], []
    try:
        source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
    except (TypeError, ValueError):
        return [], []
    if source_internal.role != MessageRole.ASSISTANT:
        return [], []
    source_calls = list(source_internal.tool_calls or [])
    source_calls_by_id: dict[str, InternalToolCall] = {}
    for source_call in source_calls:
        if source_call.id in source_calls_by_id:
            return [], []
        source_calls_by_id[source_call.id] = source_call

    details = await audit_crud.list_tool_details(db, audit_record_id)
    details_by_id = {detail.id: detail for detail in details if detail.id is not None}

    if queued_result_ids is None:
        queued_results = await message_crud.list_audit_tool_results(
            db,
            uid=work.uid,
            session_id=work.session_id,
            profile_id=work.profile_id,
            audit_record_id=audit_record_id,
        )
    else:
        queued_results = []
        seen_result_ids: set[int] = set()
        for raw_message_id in queued_result_ids:
            if isinstance(raw_message_id, bool) or not isinstance(raw_message_id, int) or raw_message_id <= 0 or raw_message_id in seen_result_ids:
                continue
            seen_result_ids.add(raw_message_id)
            queued_result = await message_crud.get(db, raw_message_id)
            if queued_result is None:
                continue
            await db.refresh(queued_result, with_for_update=True)
            queued_results.append(queued_result)

    receipt_candidates: dict[str, list[Message]] = {}
    undelivered_statuses = {"pending", "executing", "cancelled", "execution_unknown"}
    for queued_result in queued_results:
        if queued_result.id is None:
            continue
        if queued_result.uid != work.uid or queued_result.session_id != work.session_id:
            continue
        if queued_result.profile_id != work.profile_id or queued_result.role != MessageRole.TOOL:
            continue
        if queued_result.type != MessageType.TOOL_RESULT or queued_result.audit_record_id != audit_record_id:
            continue
        if queued_result.work_id != source_message.work_id:
            continue
        wrapper = _json_object(queued_result.content)
        original_call_id = wrapper.get("tool_call_id") if wrapper is not None else None
        if not isinstance(original_call_id, str) or queued_result.audit_tool_call_id != original_call_id:
            continue
        if original_call_id not in source_calls_by_id:
            continue
        result_payload = _json_object(wrapper.get("content") if wrapper is not None and isinstance(wrapper.get("content"), str) else None)
        if result_payload is None:
            continue
        result_status = result_payload.get("status")
        if result_status != "queued" and result_status not in undelivered_statuses:
            continue
        receipt_candidates.setdefault(original_call_id, []).append(queued_result)

    tasks_by_original_call_id: dict[str, list[BackgroundTask]] = {}
    for task in cancelled_tasks:
        if isinstance(task.id, bool) or not isinstance(task.id, int) or task.id <= 0:
            continue
        extra = task.extra if isinstance(task.extra, dict) else {}
        if task.audit_record_id != audit_record_id:
            continue
        execution_id = task.audit_execution_record_id
        if isinstance(execution_id, bool) or not isinstance(execution_id, int) or execution_id <= 0:
            continue
        execution = await audit_crud.get_execution_record(db, execution_id)
        if execution is None or execution.audit_record_id != audit_record_id or execution.new_tool_call_id != task.tool_call_id:
            continue
        detail = details_by_id.get(execution.audit_tool_detail_id)
        if detail is None:
            continue
        original_call = source_calls_by_id.get(detail.original_tool_call_id)
        if original_call is None or detail.tool_name != original_call.name or task.tool_name != original_call.name:
            continue
        audit_binding = extra.get("audit_binding")
        claim_token = audit_binding.get("claim_token") if isinstance(audit_binding, dict) else None
        if claim_token is not None and (not isinstance(claim_token, str) or not claim_token or claim_token != execution.claim_token):
            continue
        tasks_by_original_call_id.setdefault(original_call.id, []).append(task)

    result_ids: list[int] = []
    message_snapshots: list[dict[str, object]] = []
    for original_call in source_calls:
        task_candidates = tasks_by_original_call_id.get(original_call.id, [])
        if len(task_candidates) != 1:
            continue
        result_candidates = receipt_candidates.get(original_call.id, [])
        if len(result_candidates) != 1:
            continue
        task = task_candidates[0]
        queued_result = result_candidates[0]
        wrapper = _json_object(queued_result.content)
        result_payload = _json_object(wrapper.get("content") if wrapper is not None and isinstance(wrapper.get("content"), str) else None)
        if result_payload is None:
            continue
        result_status = result_payload.get("status")
        if result_status == "queued":
            task_id = result_payload.get("task_id")
            if isinstance(task_id, bool) or not isinstance(task_id, int) or task_id != task.id:
                continue
        elif result_status not in undelivered_statuses:
            continue
        if queued_result.id is None:
            continue
        marker = await _save_cancelled_background_result_marker(
            db,
            work=work,
            message_work_id=message_work_id,
            queued_result_id=queued_result.id,
            tool_call=original_call,
            task=task,
        )
        if marker is None:
            continue
        marker_id, marker_snapshot = marker
        result_ids.append(marker_id)
        message_snapshots.append(marker_snapshot)
    return result_ids, message_snapshots


async def _create_missing_tool_result(
    db: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    message_work_id: int | None,
    source_message_id: int,
    call_index: int,
    tool_call: InternalToolCall,
    task: BackgroundTask | None = None,
) -> Message:
    tool_execution_status = "execution_unknown"
    tool_cancelled = work.status == SessionReplyWorkStatus.CANCELLED or task is not None
    if tool_cancelled:
        error = PROMPT_TOOL_INTERRUPTED
        if task is not None and task.started_at is None:
            tool_execution_status = "failed"
            error = t(ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION)
        content = {
            "type": "tool_cancellation",
            "status": "cancelled",
            "error": error,
        }
    else:
        content = {
            "status": "execution_unknown",
            "error": PROMPT_TOOL_RESULT_UNKNOWN_AFTER_REPLY_FAILURE,
        }
    tool_result = InternalMessage(
        role=MessageRole.TOOL,
        type=MessageType.TOOL_RESULT,
        tool_call_id=tool_call.id,
        tool_execution_status=tool_execution_status,
        content=json.dumps(content, ensure_ascii=False),
        created_at=work.updated_at.timestamp(),
    )
    tool_result_payload = tool_result.model_dump(mode="json", exclude_none=True)
    if tool_cancelled:
        tool_result_payload["tool_cancelled"] = True
    return await message_crud.create_idempotent(
        db,
        obj_in={
            "session_id": work.session_id,
            "uid": work.uid,
            "role": MessageRole.TOOL,
            "type": MessageType.TOOL_RESULT,
            "content": json.dumps(tool_result_payload, ensure_ascii=False),
            "profile_id": work.profile_id,
            "is_processed": True,
            "provider_metadata": {TOOL_CANCELLATION_SOURCE_MESSAGE_ID_KEY: source_message_id},
            "work_id": message_work_id,
            "created_at": work.updated_at,
        },
        dedupe_key=_cancelled_result_dedupe_key(source_message_id, call_index, tool_call.id),
        commit=False,
    )


def _message_snapshot(message: Message, tool_call_id: str) -> dict[str, object]:
    return {
        "id": message.id,
        "db_id": message.id,
        "role": MessageRole.TOOL.value,
        "type": MessageType.TOOL_RESULT.value,
        "content": message.content,
        "tool_call_id": tool_call_id,
        "created_at": message.created_at.timestamp(),
    }


async def complete_cancelled_tool_results_for_work(
    db: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    confirmed_queued_result_ids: list[int] | None = None,
) -> None:
    """Complete one work item; the caller must hold its ended-work lock (CANCELLED or FAILED)."""
    original_state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
    state = dict(original_state)
    message_work_id = await session_reply_work_item_crud.get_message_work_id(db, work)
    cancelled_tasks = await background_task_crud.list_cancelled_for_origin_work(
        db,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        origin_work_id=work.id,
        origin_work_identity=work.dedupe_key,
    )
    is_confirmed_work = work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION and work.source_type == SessionReplySourceType.AUDIT_RECORD
    sources = await _load_registered_sources(
        db,
        work=work,
        source_ids=state.get("tool_call_message_ids", []),
        message_work_id=message_work_id,
    )
    saved_results = await _load_saved_tool_results(
        db,
        work=work,
        message_work_id=message_work_id,
        sources=sources,
    )

    result_ids: list[int] = []
    message_snapshots: list[dict[str, object]] = []
    for source_id, calls in sorted(sources.items()):
        for call_index, tool_call in enumerate(calls):
            saved_queue = saved_results.get((source_id, tool_call.id))
            if saved_queue:
                marker = await _create_cancelled_background_result_marker(
                    db,
                    work=work,
                    message_work_id=message_work_id,
                    queued_result=saved_queue[0],
                    tool_call=tool_call,
                    cancelled_tasks=cancelled_tasks,
                )
                if marker is not None:
                    marker_id, marker_snapshot = marker
                    result_ids.append(marker_id)
                    message_snapshots.append(marker_snapshot)
                saved_queue.popleft()
                continue
            matching_tasks: list[BackgroundTask] = []
            for task in cancelled_tasks:
                if task.tool_call_id != tool_call.id:
                    continue
                if task.tool_name != tool_call.name:
                    continue
                matching_tasks.append(task)
            task: BackgroundTask | None = None
            if len(matching_tasks) == 1:
                task = matching_tasks[0]
            saved = await _create_missing_tool_result(
                db,
                work=work,
                message_work_id=message_work_id,
                source_message_id=source_id,
                call_index=call_index,
                tool_call=tool_call,
                task=task,
            )
            result_ids.append(saved.id)
            message_snapshots.append(_message_snapshot(saved, tool_call.id))

    if is_confirmed_work:
        confirmed_result_ids, confirmed_snapshots = await _create_confirmed_cancelled_background_result_markers(
            db,
            work=work,
            message_work_id=message_work_id,
            queued_result_ids=confirmed_queued_result_ids,
            cancelled_tasks=cancelled_tasks,
        )
        result_ids.extend(confirmed_result_ids)
        message_snapshots.extend(confirmed_snapshots)

    if result_ids:
        existing_message_ids = state.get("external_tool_result_message_ids")
        external_tool_result_message_ids = list(existing_message_ids) if isinstance(existing_message_ids, list) else []
        for result_id in result_ids:
            if result_id not in external_tool_result_message_ids:
                external_tool_result_message_ids.append(result_id)
        state["external_tool_result_message_ids"] = external_tool_result_message_ids

    state.pop("tool_call_message_ids", None)
    if state != original_state:
        work.execution_state = state
        db.add(work)
        await db.flush()

    if result_ids and message_work_id is None and bool(state.get("show_tool_calls", True)):
        session = await session_crud.get_by_session_id(db, work.session_id)
        if session is not None and session.uid == work.uid and session.show_tool_calls:
            event_id = f"cancelled-tool-results:{work.id}:{max(result_ids)}"
            await session_event_crud.publish(
                db,
                dedupe_key=event_id,
                uid=work.uid,
                session_id=work.session_id,
                event={
                    "type": "tool_results_update",
                    "event_id": event_id,
                    "session_id": work.session_id,
                    "work_id": work.id,
                    "messages": message_snapshots,
                },
                commit=False,
            )


async def complete_cancelled_tool_results(db: AsyncSession, *, uid: str, session_id: str) -> None:
    works = await session_reply_work_item_crud.list_cancelled_with_tool_calls(db, uid=uid, session_id=session_id)
    for work in works:
        await complete_cancelled_tool_results_for_work(db, work=work)
