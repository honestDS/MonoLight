import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.event import session_event_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import (
    ensure_session_reply_work_claim,
    session_reply_work_item_crud,
)
from app.core.crud.session.session import session_crud
from app.core.crud.session.terminal_watch import terminal_watch_reply_crud
from app.core.crud.task.background import background_task_crud
from app.core.prompts import TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus

__all__ = ["route_terminal_auto_watch_reply", "prepare_terminal_auto_watch_resume"]


def _status_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _watch_arguments(task: BackgroundTask) -> tuple[str, int] | None:
    arguments = task.arguments if isinstance(task.arguments, dict) else {}
    terminal_session_id = arguments.get("terminal_session_id")
    after_offset = arguments.get("after_offset")
    if not isinstance(terminal_session_id, str) or not terminal_session_id.strip() or not isinstance(after_offset, int) or isinstance(after_offset, bool) or after_offset < 0:
        return None
    return terminal_session_id, after_offset


async def _continue_watch(db: AsyncSession, task: BackgroundTask) -> None:
    watch = _watch_arguments(task)
    if watch is None or not _positive_int(task.id):
        return
    extra = task.extra if isinstance(task.extra, dict) else {}
    await background_task_crud.create_terminal_auto_watch(
        db,
        uid=task.uid,
        session_id=task.session_id,
        profile_id=task.profile_id,
        terminal_session_id=watch[0],
        after_offset=watch[1],
        previous_task_id=task.id,
        origin_work_id=extra.get("origin_work_id"),
        origin_work_identity=extra.get("origin_work_identity"),
        commit=False,
    )


async def _consume_and_commit(db: AsyncSession, task: BackgroundTask) -> None:
    if _status_value(task.reply_status) == BackgroundTaskReplyStatus.RUNNING.value:
        task.reply_status = BackgroundTaskReplyStatus.PENDING
        task.reply_locked_by = None
        task.reply_lock_until = None
        db.add(task)
        await db.flush()
    await terminal_watch_reply_crud.consume_result(db, task)
    await db.commit()


async def _restore_reply_inbox(db: AsyncSession, task: BackgroundTask) -> None:
    if _status_value(task.reply_status) != BackgroundTaskReplyStatus.RUNNING.value:
        return
    task.reply_status = BackgroundTaskReplyStatus.PENDING
    task.reply_locked_by = None
    task.reply_lock_until = None
    db.add(task)
    await db.flush()


def _mark_routed(task: BackgroundTask) -> None:
    task.extra = {
        **(task.extra if isinstance(task.extra, dict) else {}),
        "terminal_auto_watch_routed": True,
    }


async def route_terminal_auto_watch_reply(db: AsyncSession, task: BackgroundTask) -> bool:
    extra = task.extra if isinstance(task.extra, dict) else {}
    if extra.get("auto_terminal_watch") is not True or "origin_work_id" not in extra:
        return False

    watch = _watch_arguments(task)
    result = task.result if isinstance(task.result, dict) else {}
    result_content = result.get("content")
    result_next_offset = result_content.get("next_offset") if isinstance(result_content, dict) else None
    if (
        watch is not None
        and isinstance(result_next_offset, int)
        and not isinstance(result_next_offset, bool)
        and result_next_offset >= 0
        and await terminal_watch_reply_crud.has_delivered_terminal_read(
            db,
            uid=task.uid,
            session_id=task.session_id,
            profile_id=task.profile_id,
            terminal_session_id=watch[0],
            start_offset=watch[1],
            next_offset=result_next_offset,
        )
    ):
        await _restore_reply_inbox(db, task)
        consumed = await terminal_watch_reply_crud.consume_result(db, task)
        if consumed and result_content.get("eof") is not True:
            await _continue_watch(db, task)
        await db.commit()
        return True

    work = await terminal_watch_reply_crud.lock_origin(db, task)
    await db.refresh(task)
    task_status = _status_value(task.status)
    if task.auto_reply is not True or _status_value(task.reply_status) not in {BackgroundTaskReplyStatus.PENDING.value, BackgroundTaskReplyStatus.RUNNING.value} or task_status not in {BackgroundTaskStatus.SUCCEEDED.value, BackgroundTaskStatus.FAILED.value}:
        await db.commit()
        return True

    work_status = _status_value(work.status) if work is not None else None
    invalid_work = work_status not in {
        SessionReplyWorkStatus.SUCCEEDED.value,
        SessionReplyWorkStatus.READY_FOR_LLM.value,
        SessionReplyWorkStatus.RUNNING.value,
        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value,
    }
    if invalid_work:
        await _consume_and_commit(db, task)
        return True

    session = await session_crud.get_by_session_id(db, task.session_id)
    if session is None or session.uid != task.uid or session.profile_id != task.profile_id:
        await _consume_and_commit(db, task)
        return True

    if await terminal_watch_reply_crud.manual_watch_covers(db, task):
        await _restore_reply_inbox(db, task)
        consumed = await terminal_watch_reply_crud.consume_result(db, task)
        if consumed:
            await _continue_watch(db, task)
        await db.commit()
        return True

    if work_status == SessionReplyWorkStatus.SUCCEEDED.value:
        await _restore_reply_inbox(db, task)
        _mark_routed(task)
        db.add(task)
        reopened = await terminal_watch_reply_crud.reopen_completed(db, work)
        if reopened:
            await session_event_crud.publish(
                db,
                dedupe_key=f"terminal-auto-watch-ready:{work.id}:{task.id}",
                uid=work.uid,
                session_id=work.session_id,
                event={"type": "work_ready", "session_id": work.session_id, "work_id": work.id},
                commit=False,
            )
    else:
        await _restore_reply_inbox(db, task)
        _mark_routed(task)
        db.add(task)
    await db.commit()
    return True


def _result_payload(task: BackgroundTask) -> dict[str, Any]:
    extra = task.extra if isinstance(task.extra, dict) else {}
    result: Any = task.result
    if result is None:
        result = {"status": _status_value(task.status), "tool_name": task.tool_name, "error": task.error}
    task_result = result.get("content") if isinstance(result, dict) and result.get("content") is not None else result
    return {
        "type": "background_tool_result",
        "ui_only": True,
        "instruction": TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT,
        "task": task_result,
        "task_id": task.id,
        "tool_name": task.tool_name,
        "tool_call": {
            "id": task.tool_call_id,
            "name": task.tool_name,
            "arguments": task.arguments or {},
        },
        "request_context": extra.get("request_context") if isinstance(extra.get("request_context"), list) else [],
        "purpose": extra.get("purpose"),
    }


async def _persist_result_marker(
    db: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    task: BackgroundTask,
    payload: dict[str, Any],
) -> Message:
    dedupe_key = f"background-task:{task.id}:result"
    marker = await message_crud.get_by_dedupe_key(db, dedupe_key)
    if marker is None:
        marker = Message(
            session_id=work.session_id,
            uid=work.uid,
            role=MessageRole.USER,
            type=MessageType.BACKGROUND_TASK_RESULT,
            content=json.dumps(payload, ensure_ascii=False),
            profile_id=work.profile_id,
            is_processed=True,
            dedupe_key=dedupe_key,
        )
        await message_crud.persist(db, message=marker, commit=False)
    elif marker.uid != work.uid or marker.session_id != work.session_id or marker.profile_id != work.profile_id or marker.role not in {MessageRole.USER, MessageRole.ASSISTANT} or marker.type != MessageType.BACKGROUND_TASK_RESULT:
        raise RuntimeError("Terminal auto-watch result marker identity mismatch")
    if not _positive_int(marker.id):
        raise RuntimeError("Terminal auto-watch result marker was not persisted")
    return marker


async def prepare_terminal_auto_watch_resume(
    db: AsyncSession,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> bool:
    await ensure_session_reply_work_claim(db)
    await db.refresh(work)

    payloads: list[dict[str, Any]] = []
    marker_ids: list[int] = []
    for task in await terminal_watch_reply_crud.list_pending_results(db, work):
        if not await terminal_watch_reply_crud.consume_result(db, task):
            continue
        covered = await terminal_watch_reply_crud.manual_watch_covers(db, task)
        await _continue_watch(db, task)
        if covered:
            continue

        payload = _result_payload(task)
        marker = await _persist_result_marker(db, work=work, task=task, payload=payload)
        payloads.append(payload)
        marker_ids.append(marker.id)

    state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
    if payloads:
        raw_checkpoint = state.get("dispatcher_checkpoint")
        checkpoint = dict(raw_checkpoint) if isinstance(raw_checkpoint, dict) else {}
        checkpoint.pop("external_resume_messages", None)
        checkpoint["pending_resume_message_ids"] = list(
            dict.fromkeys(
                [
                    *checkpoint.get("pending_resume_message_ids", []),
                    *marker_ids,
                ]
            )
        )
        checkpoint.update({"messages": [], "turn_messages": [], "reload_history": True})
        checkpoint["context_summary_trigger_mode"] = "user_message"
        old_upper = checkpoint.get("context_summary_fixed_upper_message_id")
        upper_bounds = [*marker_ids, old_upper] if _positive_int(old_upper) else marker_ids
        checkpoint["context_summary_fixed_upper_message_id"] = max(upper_bounds)
        state["dispatcher_checkpoint"] = checkpoint
        if work.input_message_ids:
            state["external_resume_pending"] = True
        else:
            state.pop("external_resume_pending", None)
        state.pop("terminal_auto_resume", None)
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={"execution_state": state},
            commit=False,
        )
        if not updated:
            raise RuntimeError("Terminal auto-watch resume lost its work claim")
        work.execution_state = state
        await db.commit()
        return True

    if state.get("terminal_auto_resume") is True:
        state.pop("terminal_auto_resume", None)
        state.pop("external_resume_pending", None)
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={"execution_state": state},
            commit=False,
        )
        if not updated:
            raise RuntimeError("Terminal auto-watch resume lost its work claim")
        work.execution_state = state
        marked = await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=work.id,
            worker_id=worker_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            event_sent=True,
            commit=False,
        )
        if not marked:
            raise RuntimeError("Terminal auto-watch work could not be completed")
        await db.commit()
        return False

    await db.commit()
    return True
