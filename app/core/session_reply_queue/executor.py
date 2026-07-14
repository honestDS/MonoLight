import hashlib
import json
import time
from datetime import datetime
from typing import Any

from sqlalchemy import update

from app.core import constants
from app.core.crud.background_task import background_task_crud
from app.core.crud.message import message_crud
from app.core.crud.profile import profile_crud
from app.core.crud.session_reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session_reply_work_item import session_reply_work_item_crud
from app.core.dispatcher import ChatDispatcher
from app.core.i18n import t
from app.core.message_platforms.notifier import send_session_event
from app.core.prompts import BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.assistant_files import parse_assistant_files_content
from app.core.utils.dispatcher.helpers import dump_background_proactive_history
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX = "session-reply-work"


def _work_identity(work: SessionReplyWorkItem) -> str:
    created_at = work.created_at.replace(tzinfo=None).isoformat(timespec="microseconds") if isinstance(work.created_at, datetime) else str(work.created_at)
    payload = json.dumps(
        {
            "dedupe_key": work.dedupe_key,
            "created_at": created_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _result_message_dedupe_key(work: SessionReplyWorkItem) -> str:
    return f"{SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX}:{_work_identity(work)}:result"


def _error_message_dedupe_key(work: SessionReplyWorkItem) -> str:
    return f"{SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX}:{_work_identity(work)}:error"


def _legacy_result_message_dedupe_key(work_id: int) -> str:
    return f"{SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX}:{work_id}:result"


def _message_belongs_to_work(message: Message, work: SessionReplyWorkItem) -> bool:
    if message.uid != work.uid or message.session_id != work.session_id or message.profile_id != work.profile_id:
        return False
    if not isinstance(message.created_at, datetime) or not isinstance(work.created_at, datetime):
        return False
    return message.created_at.replace(tzinfo=None) >= work.created_at.replace(tzinfo=None)


async def _get_persisted_result(db, work: SessionReplyWorkItem) -> Message | None:
    persisted_result = await db.get(Message, work.result_message_id) if work.result_message_id else None
    if persisted_result is not None and _message_belongs_to_work(persisted_result, work):
        return persisted_result

    persisted_result = await message_crud.get_by_dedupe_key(db, _result_message_dedupe_key(work))
    if persisted_result is not None and _message_belongs_to_work(persisted_result, work):
        return persisted_result

    if work.id is None:
        return None
    legacy_result = await message_crud.get_by_dedupe_key(db, _legacy_result_message_dedupe_key(work.id))
    if legacy_result is not None and _message_belongs_to_work(legacy_result, work):
        return legacy_result
    return None


def _response_from_persisted_message(work: SessionReplyWorkItem, message: Message) -> dict[str, Any]:
    content, files = parse_assistant_files_content(message.content)
    if work.work_type == SessionReplyWorkType.FOREGROUND_REPLY:
        return {
            "choices": [
                {
                    "message": {
                        "role": MessageRole.ASSISTANT,
                        "content": content,
                    },
                    "finish_reason": True,
                    "created_at": time.time(),
                }
            ],
            "history": [],
            "files": files or None,
        }
    return {
        "content": content,
        "history": [],
        "files": files,
    }


def _response_content(response: dict[str, Any]) -> str:
    if isinstance(response.get("content"), str):
        return response["content"]
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _event_for_work(work: SessionReplyWorkItem, response: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    source = {
        SessionReplyWorkType.FOREGROUND_REPLY: "foreground",
        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY: "background_task",
        SessionReplyWorkType.SCHEDULED_TASK_SUMMARY: "scheduled_task",
    }[work.work_type]
    event = {
        "event_id": f"session-reply-work:{_work_identity(work)}:{'error' if error else 'event'}",
        "type": "proactive_reply_error" if error else "proactive_reply",
        "source": source,
        "session_id": work.session_id,
        "work_id": work.id,
        "content": _response_content(response),
        "history": response.get("history", []),
        "files": response.get("files", []),
    }
    if work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
        event["task_id"] = int(work.source_id)
        event["background_task_id"] = int(work.source_id)
    elif work.work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY:
        event["trigger_message_id"] = int(work.source_id)
    return event


async def _execute_foreground(db, work: SessionReplyWorkItem, worker_id: str) -> dict[str, Any]:
    content, attachments, message_ids = await session_reply_queue_manager.freeze_foreground_input(db, work=work, worker_id=worker_id)
    initial_message = InternalMessage(
        id=message_ids[-1],
        role=MessageRole.USER,
        content=content,
        attachments=attachments or None,
    )
    await db.refresh(work)
    stream_requested = bool((work.execution_state or {}).get("stream_requested"))
    async with AsyncSessionLocal() as event_db:
        next_stream_sequence = await session_reply_stream_event_crud.get_latest_sequence(event_db, work_id=work.id) + 1

    async def publish_stream_event(event: dict[str, Any]) -> None:
        nonlocal next_stream_sequence
        persisted_event = {
            **event,
            "session_id": work.session_id,
            "work_id": work.id,
        }
        async with AsyncSessionLocal() as event_db:
            await session_reply_stream_event_crud.publish(
                event_db,
                work_id=work.id,
                sequence_no=next_stream_sequence,
                event=persisted_event,
            )
        next_stream_sequence += 1

    async def fetch_additional_user_messages() -> list[InternalMessage]:
        return await session_reply_queue_manager.absorb_contiguous_foreground_messages(
            db,
            work_id=work.id,
            worker_id=worker_id,
        )

    async def check_work_validity() -> bool:
        async with AsyncSessionLocal() as validity_db:
            active_claims = await session_reply_work_item_crud.get_active_claims(
                validity_db,
                {work.id: worker_id},
            )
        return (work.id, worker_id) in active_claims

    async def save_execution_checkpoint(checkpoint: dict[str, Any]) -> None:
        state = {
            **(work.execution_state or {}),
            "dispatcher_checkpoint": checkpoint,
        }
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={"execution_state": state},
        )
        if not updated:
            raise RuntimeError("Session reply work lease was lost while saving execution checkpoint")
        work.execution_state = state

    execution_state = work.execution_state or {}
    expose_tool_call_content = bool(execution_state.get("expose_tool_call_content", True))
    resume_state = execution_state.get("dispatcher_checkpoint")
    if not isinstance(resume_state, dict) or not isinstance(resume_state.get("messages"), list):
        resume_state = None

    return await ChatDispatcher.dispatch(
        db=db,
        message=content,
        uid=work.uid,
        session_id=work.session_id,
        attachments=attachments or None,
        session_source="queue",
        persisted_initial_message=initial_message,
        history_before_id=message_ids[0],
        frozen_user_message_ids=message_ids,
        final_message_dedupe_key=_result_message_dedupe_key(work),
        persisted_profile_id=work.profile_id,
        stream_event_callback=publish_stream_event if stream_requested else None,
        additional_user_messages_fetcher=fetch_additional_user_messages,
        execution_resume_state=resume_state,
        execution_checkpoint_callback=save_execution_checkpoint,
        context_summary_work_validity_checker=check_work_validity,
        expose_tool_call_content=expose_tool_call_content,
    )


def _load_background_submission_context(task: BackgroundTask) -> list[InternalMessage] | None:
    extra = task.extra if isinstance(task.extra, dict) else {}
    if "submission_context" not in extra:
        return None
    raw_context = extra.get("submission_context")
    if not isinstance(raw_context, list):
        return None
    return [InternalMessage.model_validate(message) for message in raw_context if isinstance(message, dict)]


def _build_background_result_messages(task: BackgroundTask) -> list[InternalMessage]:
    task_result = task.result or {
        "status": task.status,
        "tool_name": task.tool_name,
        "error": task.error,
    }
    return [
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=task.tool_call_id,
            content=json.dumps(task_result, ensure_ascii=False),
        ),
        InternalMessage(
            role=MessageRole.USER,
            content=BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT,
        ),
    ]


async def _execute_background(db, work: SessionReplyWorkItem) -> dict[str, Any]:
    task = await background_task_crud.get(db, int(work.source_id))
    if task is None:
        raise RuntimeError(t(constants.ERR_BACKGROUND_TASK_NOT_FOUND))
    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        raise RuntimeError(t(constants.ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE))
    submission_context = _load_background_submission_context(task)
    ai_msg, turn_messages, files = await ChatDispatcher._generate_reply_from_history(
        db,
        uid=work.uid,
        session_id=work.session_id,
        profile=profile,
        call_context="session_reply_background_summary",
        allow_tools=True,
        extra_messages=_build_background_result_messages(task) if submission_context is not None else None,
        submission_context=submission_context,
        reply_source="background_task",
        final_message_dedupe_key=_result_message_dedupe_key(work),
    )
    content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
    return {
        "content": content,
        "history": dump_background_proactive_history(turn_messages),
        "files": files,
    }


async def _execute_scheduled(db, work: SessionReplyWorkItem) -> dict[str, Any]:
    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        raise RuntimeError(t(constants.ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND))
    ai_msg, turn_messages, files = await ChatDispatcher._generate_reply_from_history(
        db,
        uid=work.uid,
        session_id=work.session_id,
        profile=profile,
        call_context="session_reply_scheduled_summary",
        allow_tools=True,
        restrict_tools_to_background_allowlist=False,
        reply_source="scheduled_task",
        final_message_dedupe_key=_result_message_dedupe_key(work),
    )
    content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
    return {
        "content": content,
        "history": [message.model_dump(mode="json") for message in turn_messages],
        "files": files,
    }


async def execute_session_reply_work(work_id: int, worker_id: str) -> None:
    async with AsyncSessionLocal() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id:
            return

        persisted_result = await _get_persisted_result(db, work)
        if persisted_result is not None:
            response = (work.execution_state or {}).get("response") or _response_from_persisted_message(work, persisted_result)
        elif work.work_type == SessionReplyWorkType.FOREGROUND_REPLY:
            response = await _execute_foreground(db, work, worker_id)
        elif work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
            response = await _execute_background(db, work)
        else:
            response = await _execute_scheduled(db, work)

        result_message = persisted_result or await message_crud.get_by_dedupe_key(db, _result_message_dedupe_key(work))
        if result_message is None:
            raise RuntimeError("Final assistant message was not persisted")

        state = {**(work.execution_state or {}), "response": response}
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"result_message_id": result_message.id, "execution_state": state},
        )
        if not updated:
            return

    await send_session_event(work.uid, work.session_id, _event_for_work(work, response))

    async with AsyncSessionLocal() as db:
        if work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
            await db.execute(
                update(BackgroundTask)
                .where(BackgroundTask.id == int(work.source_id))
                .values(
                    reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
                    reply_locked_by=None,
                    reply_lock_until=None,
                )
            )
        await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=work_id,
            worker_id=worker_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            result_message_id=result_message.id,
            event_sent=True,
            commit=False,
        )
        await db.commit()


async def fail_session_reply_work(
    work_id: int,
    worker_id: str,
    error: str,
    *,
    user_error: str | None = None,
) -> None:
    async with AsyncSessionLocal() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id:
            return
        error_content = user_error or t(constants.ERR_LLM_UNEXPECTED_ERROR)
        error_message = await save_message(
            db,
            work.session_id,
            work.uid,
            MessageRole.ERR,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ERR, content=error_content),
            work.profile_id,
            is_processed=True,
            dedupe_key=_error_message_dedupe_key(work),
        )

    await send_session_event(work.uid, work.session_id, _event_for_work(work, {"content": error_content}, error=True))

    async with AsyncSessionLocal() as db:
        if work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
            await db.execute(
                update(BackgroundTask)
                .where(BackgroundTask.id == int(work.source_id))
                .values(
                    reply_status=BackgroundTaskReplyStatus.FAILED,
                    reply_locked_by=None,
                    reply_lock_until=None,
                    error=error,
                )
            )
        await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=work_id,
            worker_id=worker_id,
            status=SessionReplyWorkStatus.FAILED,
            result_message_id=error_message.id,
            error=error,
            event_sent=True,
            commit=False,
        )
        await db.commit()


def retry_delay_seconds(attempt_count: int) -> int:
    return min(300, 2 ** max(0, attempt_count - 1))


def lease_deadline() -> int:
    return int(time.time())
