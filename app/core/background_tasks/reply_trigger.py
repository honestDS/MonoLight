import json
from typing import Any

from sqlalchemy import select, update

from app.core.background_tasks.terminal_watch_reply import route_terminal_auto_watch_reply
from app.core.crud.session.session import session_crud
from app.core.crud.task.background import background_task_crud
from app.core.log import get_logger
from app.core.prompts import BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT, TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _get_terminal_auto_watch_origin(task: BackgroundTask) -> tuple[int, str] | None:
    extra = task.extra if isinstance(task.extra, dict) else {}
    origin_work_id = extra.get("origin_work_id")
    origin_work_identity = extra.get("origin_work_identity")
    if not _is_positive_int(origin_work_id) or not isinstance(origin_work_identity, str) or not origin_work_identity.strip():
        return None
    return origin_work_id, origin_work_identity


def _build_background_tool_result_payload(task: BackgroundTask) -> dict[str, Any]:
    extra = task.extra if isinstance(task.extra, dict) else {}
    task_result = task.result or {"status": task.status, "tool_name": task.tool_name, "error": task.error}
    tool_call = {
        "id": task.tool_call_id,
        "name": task.tool_name,
        "arguments": task.arguments or {},
    }
    tool_result = {
        "tool_call_id": task.tool_call_id,
        "content": json.dumps(task_result, ensure_ascii=False),
    }
    return {
        "type": "background_tool_result",
        "instruction": TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT if extra.get("auto_terminal_watch") is True else BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT,
        "task": task_result,
        "tool_call": tool_call,
        "tool_result": tool_result,
        "task_id": task.id,
        "tool_name": task.tool_name,
        "source": extra.get("source"),
        "request_context": extra.get("request_context") if isinstance(extra.get("request_context"), list) else [],
        "purpose": extra.get("purpose"),
    }


def _build_background_tool_result_message(task: BackgroundTask, *, include_tasks: bool = False) -> str:
    payload = _build_background_tool_result_payload(task)
    if include_tasks:
        payload = {**payload, "tasks": [dict(payload)]}
    return json.dumps(payload, ensure_ascii=False)


async def _find_terminal_auto_watch_group(
    db,
    *,
    task: BackgroundTask,
    origin_work_id: int,
    origin_work_identity: str,
) -> SessionReplyWorkItem | None:
    result = await db.execute(
        select(SessionReplyWorkItem)
        .where(
            SessionReplyWorkItem.uid == task.uid,
            SessionReplyWorkItem.session_id == task.session_id,
            SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
        )
        .order_by(SessionReplyWorkItem.sequence_no.asc(), SessionReplyWorkItem.id.asc())
    )
    for work in result.scalars():
        execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
        if work.profile_id == task.profile_id and execution_state.get("terminal_auto_watch_origin_work_id") == origin_work_id and execution_state.get("terminal_auto_watch_origin_work_identity") == origin_work_identity:
            return work
    return None


def _terminal_auto_watch_task_ids(work: SessionReplyWorkItem) -> list[int]:
    execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
    raw_task_ids = execution_state.get("terminal_auto_watch_task_ids")
    task_ids: list[int] = []
    if isinstance(raw_task_ids, list):
        for task_id in raw_task_ids:
            if _is_positive_int(task_id) and task_id not in task_ids:
                task_ids.append(task_id)
    return task_ids


async def _merge_terminal_auto_watch_reply(
    db,
    *,
    task: BackgroundTask,
    work: SessionReplyWorkItem,
) -> bool:
    if not _is_positive_int(task.id):
        return False

    execution_state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
    payloads = execution_state.get("external_resume_context")
    if not isinstance(payloads, list) or not payloads or not isinstance(payloads[0], dict):
        raise RuntimeError("Terminal auto-watch result payload is unavailable")

    payload = payloads[0]
    if payload.get("type") != "background_tool_result" or not _is_positive_int(payload.get("task_id")) or "task" not in payload or not isinstance(payload.get("instruction"), str) or not payload["instruction"].strip():
        raise RuntimeError("Terminal auto-watch result payload is invalid")
    tool_call = payload.get("tool_call")
    if not isinstance(tool_call, dict) or not isinstance(tool_call.get("id"), str) or not tool_call["id"].strip() or not isinstance(tool_call.get("name"), str) or not tool_call["name"].strip() or not isinstance(tool_call.get("arguments"), dict):
        raise RuntimeError("Terminal auto-watch result payload is invalid")

    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or any(not isinstance(item, dict) or not _is_positive_int(item.get("task_id")) for item in tasks):
        raise RuntimeError("Terminal auto-watch result payload is invalid")
    if not any(isinstance(item, dict) and item.get("task_id") == task.id for item in tasks):
        tasks.append(_build_background_tool_result_payload(task))
    payload = {**payload, "tasks": tasks}
    payloads = [*payloads]
    payloads[0] = payload
    execution_state["external_resume_context"] = payloads
    task_ids = _terminal_auto_watch_task_ids(work)
    if not task_ids:
        try:
            anchor_task_id = int(work.source_id)
        except (TypeError, ValueError):
            anchor_task_id = None
        if anchor_task_id is not None and _is_positive_int(anchor_task_id):
            task_ids.append(anchor_task_id)
    if task.id not in task_ids:
        task_ids.append(task.id)
    execution_state["terminal_auto_watch_task_ids"] = task_ids
    work_updated = await db.execute(
        update(SessionReplyWorkItem)
        .where(
            SessionReplyWorkItem.id == work.id,
            SessionReplyWorkItem.uid == task.uid,
            SessionReplyWorkItem.session_id == task.session_id,
            SessionReplyWorkItem.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
        )
        .values(execution_state=execution_state)
        .execution_options(synchronize_session=False)
    )
    return (work_updated.rowcount or 0) == 1


async def _mark_reply_succeeded(db, task_id: int) -> bool:
    result = await db.execute(
        update(BackgroundTask)
        .where(
            BackgroundTask.id == task_id,
            BackgroundTask.status.in_([BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED]),
            BackgroundTask.auto_reply.is_(True),
            BackgroundTask.reply_status == BackgroundTaskReplyStatus.RUNNING,
        )
        .values(
            reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
            reply_locked_by=None,
            reply_lock_until=None,
        )
        .execution_options(synchronize_session=False)
    )
    return (result.rowcount or 0) == 1


async def trigger_background_task_reply(task_id: int) -> None:
    async with AsyncSessionLocal() as db:
        task = await background_task_crud.get(db, task_id)
        if task is None:
            return
        session = await session_crud.get_by_session_id(db, task.session_id)
        if session is None:
            task.reply_status = BackgroundTaskReplyStatus.FAILED
            task.reply_locked_by = None
            task.reply_lock_until = None
            db.add(task)
            await db.commit()
            return

        claimed = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.status.in_([BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED]),
                BackgroundTask.auto_reply.is_(True),
                BackgroundTask.reply_status == BackgroundTaskReplyStatus.PENDING,
            )
            .values(
                reply_status=BackgroundTaskReplyStatus.RUNNING,
                reply_locked_by=None,
                reply_lock_until=None,
            )
        )
        if (claimed.rowcount or 0) != 1:
            await db.rollback()
            return

        extra = task.extra if isinstance(task.extra, dict) else {}
        auto_terminal_watch = extra.get("auto_terminal_watch") is True
        origin = _get_terminal_auto_watch_origin(task) if auto_terminal_watch else None
        if auto_terminal_watch and "origin_work_id" in extra:
            if await route_terminal_auto_watch_reply(db, task):
                await db.commit()
                return

        grouped_work = None
        if origin is not None:
            grouped_work = await _find_terminal_auto_watch_group(
                db,
                task=task,
                origin_work_id=origin[0],
                origin_work_identity=origin[1],
            )

        if grouped_work is not None:
            if not await _merge_terminal_auto_watch_reply(db, task=task, work=grouped_work):
                await db.rollback()
                return
            if not await _mark_reply_succeeded(db, task_id):
                await db.rollback()
                return
            await db.commit()
            return

        work, _ = await session_reply_queue_manager.enqueue_background_summary(
            db,
            uid=task.uid,
            session_id=task.session_id,
            profile_id=task.profile_id,
            background_task_id=task_id,
            commit=False,
        )
        payload = _build_background_tool_result_payload(task)
        if origin is not None:
            payload = {**payload, "tasks": [dict(payload)]}
        execution_state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        execution_state.update(
            {
                "external_resume_context": [payload],
                "dispatcher_checkpoint": {
                    "messages": [],
                    "turn_messages": [],
                    "reload_history": True,
                    "deferred_external_results": True,
                },
            }
        )
        if origin is not None:
            execution_state.update(
                {
                    "terminal_auto_watch_group": True,
                    "terminal_auto_watch_origin_work_id": origin[0],
                    "terminal_auto_watch_origin_work_identity": origin[1],
                    "terminal_auto_watch_task_ids": [task.id],
                }
            )
        work.execution_state = execution_state
        db.add(work)
        await db.commit()
        logger.bind(task_id=task_id, uid=task.uid, session_id=task.session_id).info("Background task summary queued")
