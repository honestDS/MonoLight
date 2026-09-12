import time

from sqlalchemy import update

from app.core.constants import ERR_LLM_UNEXPECTED_ERROR, ERR_SESSION_REPLY_FINAL_MESSAGE_NOT_PERSISTED
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.i18n import t
from app.core.message_platforms.notifier import send_session_event
from app.core.session_reply_queue.executor_common import (
    _error_message_dedupe_key,
    _event_for_work,
    _get_persisted_result,
    _response_from_persisted_message,
    _result_message_dedupe_key,
)
from app.core.session_reply_queue.executor_confirmed import _execute_confirmed_tools
from app.core.session_reply_queue.executor_replies import _execute_background, _execute_foreground, _execute_scheduled
from app.core.session_reply_queue.manager import build_identified_work_response
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

__all__ = [
    "execute_session_reply_work",
    "fail_session_reply_work",
    "retry_delay_seconds",
    "lease_deadline",
]


async def execute_session_reply_work(work_id: int, worker_id: str) -> None:
    """执行已领取的会话回复工作并完成结果投递。"""
    async with AsyncSessionLocal() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id:
            return

        persisted_result = await _get_persisted_result(db, work)
        if persisted_result is not None:
            response = (work.execution_state or {}).get("response") or _response_from_persisted_message(work, persisted_result)
        elif work.work_type == SessionReplyWorkType.FOREGROUND_REPLY:
            response = await _execute_foreground(db, work, worker_id)
        elif work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
            response = await _execute_confirmed_tools(db, work, worker_id)
        elif work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
            response = await _execute_background(db, work, worker_id)
        else:
            response = await _execute_scheduled(db, work, worker_id)

        result_message = persisted_result or await message_crud.get_by_dedupe_key(db, _result_message_dedupe_key(work))
        if result_message is None:
            raise RuntimeError(t(ERR_SESSION_REPLY_FINAL_MESSAGE_NOT_PERSISTED))

        identified_response = build_identified_work_response(work, response, message_id=result_message.id)
        state = {**(work.execution_state or {}), "response": identified_response}
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"result_message_id": result_message.id, "execution_state": state},
        )
        if not updated:
            return

    await send_session_event(work.uid, work.session_id, _event_for_work(work, identified_response))

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
        error_content = user_error or t(ERR_LLM_UNEXPECTED_ERROR)
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
        identified_response = build_identified_work_response(
            work,
            {"content": error_content},
            message_id=error_message.id,
        )

    await send_session_event(work.uid, work.session_id, _event_for_work(work, identified_response, error=True))

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
