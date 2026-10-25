import json
from typing import Any

from sqlalchemy import select, update

from app.core.background_tasks.terminal_watch_reply import route_terminal_auto_watch_reply
from app.core.crud.session.message import message_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.background import background_task_crud
from app.core.log import get_logger
from app.core.prompts import BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT, TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

BACKGROUND_RESULT_MESSAGE_SAVED_EXTRA_KEY = "background_result_message_saved"


def _build_background_message_dedupe_key(task_id: int | str, purpose: str) -> str:
    return f"background-task:{task_id}:{purpose}"


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _get_terminal_auto_watch_origin(task: BackgroundTask) -> tuple[int, str] | None:
    extra = task.extra if isinstance(task.extra, dict) else {}
    origin_work_id = extra.get("origin_work_id")
    origin_work_identity = extra.get("origin_work_identity")
    if not _is_positive_int(origin_work_id) or not isinstance(origin_work_identity, str) or not origin_work_identity.strip():
        return None
    return origin_work_id, origin_work_identity


def _get_terminal_session_id(task: BackgroundTask) -> str | None:
    extra = task.extra if isinstance(task.extra, dict) else {}
    terminal_session_id = extra.get("terminal_session_id")
    if isinstance(terminal_session_id, str) and terminal_session_id.strip():
        return terminal_session_id
    arguments = task.arguments if isinstance(task.arguments, dict) else {}
    terminal_session_id = arguments.get("terminal_session_id")
    if isinstance(terminal_session_id, str) and terminal_session_id.strip():
        return terminal_session_id
    return None


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


def _terminal_auto_watch_result_dedupe_key(work: SessionReplyWorkItem) -> str:
    task_ids = _terminal_auto_watch_task_ids(work)
    anchor_task_id: int | str = task_ids[0] if task_ids else work.source_id
    return _build_background_message_dedupe_key(anchor_task_id, "result")


async def _merge_terminal_auto_watch_reply(
    db,
    *,
    task: BackgroundTask,
    work: SessionReplyWorkItem,
) -> bool:
    if not _is_positive_int(task.id):
        return False

    result_message = await message_crud.get_by_dedupe_key(db, _terminal_auto_watch_result_dedupe_key(work))
    if result_message is None or result_message.uid != task.uid or result_message.session_id != task.session_id or result_message.profile_id != task.profile_id or result_message.type != MessageType.BACKGROUND_TASK_RESULT:
        raise RuntimeError("Terminal auto-watch result message is unavailable")

    try:
        message_payload = json.loads(result_message.content or "")
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Terminal auto-watch result message is invalid") from exc
    if not isinstance(message_payload, dict):
        raise RuntimeError("Terminal auto-watch result message is invalid")

    tasks = message_payload.get("tasks")
    if not isinstance(tasks, list):
        tasks = []
    if not any(isinstance(item, dict) and item.get("task_id") == task.id for item in tasks):
        tasks.append(_build_background_tool_result_payload(task))
    message_payload["tasks"] = tasks
    message_updated = await db.execute(
        update(Message)
        .where(
            Message.id == result_message.id,
            Message.uid == task.uid,
            Message.session_id == task.session_id,
            Message.profile_id == task.profile_id,
            Message.type == MessageType.BACKGROUND_TASK_RESULT,
        )
        .values(content=json.dumps(message_payload, ensure_ascii=False))
        .execution_options(synchronize_session=False)
    )
    if (message_updated.rowcount or 0) != 1:
        return False

    execution_state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
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

        if auto_terminal_watch:
            terminal_session_id = _get_terminal_session_id(task)
            if terminal_session_id is not None:
                active_manual_watch = await background_task_crud.get_active_terminal_watch(
                    db,
                    task.uid,
                    task.session_id,
                    terminal_session_id,
                    manual_only=True,
                )
                if active_manual_watch is not None:
                    if not await _mark_reply_succeeded(db, task_id):
                        await db.rollback()
                        return
                    await db.commit()
                    return

        if grouped_work is not None:
            if not await _merge_terminal_auto_watch_reply(db, task=task, work=grouped_work):
                await db.rollback()
                return
            if not await _mark_reply_succeeded(db, task_id):
                await db.rollback()
                return
            await db.commit()
            return

        result_message = Message(
            session_id=task.session_id,
            uid=task.uid,
            role=MessageRole.USER,
            type=MessageType.BACKGROUND_TASK_RESULT,
            content=_build_background_tool_result_message(task, include_tasks=True) if origin is not None else _build_background_tool_result_message(task),
            profile_id=task.profile_id,
            is_processed=True,
            dedupe_key=_build_background_message_dedupe_key(task_id, "result"),
        )
        await message_crud.persist(db, message=result_message, commit=False)
        if origin is not None:
            work, _created = await session_reply_queue_manager.enqueue_background_summary(
                db,
                uid=task.uid,
                session_id=task.session_id,
                profile_id=task.profile_id,
                background_task_id=task_id,
                commit=False,
            )
        else:
            await session_reply_queue_manager.enqueue_background_summary(
                db,
                uid=task.uid,
                session_id=task.session_id,
                profile_id=task.profile_id,
                background_task_id=task_id,
                commit=False,
            )
        task.extra = {
            **extra,
            BACKGROUND_RESULT_MESSAGE_SAVED_EXTRA_KEY: True,
        }
        db.add(task)
        if origin is not None:
            work.execution_state = {
                **(work.execution_state if isinstance(work.execution_state, dict) else {}),
                "terminal_auto_watch_group": True,
                "terminal_auto_watch_origin_work_id": origin[0],
                "terminal_auto_watch_origin_work_identity": origin[1],
                "terminal_auto_watch_task_ids": [task.id],
            }
            db.add(work)
        await db.commit()
        logger.bind(task_id=task_id, uid=task.uid, session_id=task.session_id).info("Background task summary queued")
