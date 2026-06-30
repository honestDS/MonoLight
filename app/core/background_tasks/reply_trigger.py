import asyncio
import json

from app.core.crud.background_task import background_task_crud
from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT
from app.core.utils.dispatcher.helpers import format_exception_message
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import InternalMessage, MessageRole, MessageType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

BACKGROUND_RESULT_MESSAGE_SAVED_EXTRA_KEY = "background_result_message_saved"


def _build_background_tool_result_message(task) -> str:
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
    return json.dumps(
        {
            "type": "background_tool_result",
            "instruction": BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT,
            "task": task_result,
            "tool_call": tool_call,
            "tool_result": tool_result,
        },
        ensure_ascii=False,
    )


async def _save_background_task_result_message(db, task) -> None:
    extra = task.extra if isinstance(task.extra, dict) else {}
    if extra.get(BACKGROUND_RESULT_MESSAGE_SAVED_EXTRA_KEY):
        logger.bind(task_id=task.id, uid=task.uid, session_id=task.session_id).info(t("LOG_BACKGROUND_TASK_RESULT_MESSAGE_EXISTS"))
        return

    result_message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=_build_background_tool_result_message(task),
    )
    await save_message(
        db,
        task.session_id,
        task.uid,
        MessageRole.ASSISTANT,
        MessageType.BACKGROUND_TASK_RESULT,
        result_message,
        task.profile_id,
        is_processed=True,
    )
    task.extra = {**extra, BACKGROUND_RESULT_MESSAGE_SAVED_EXTRA_KEY: True}
    db.add(task)
    await db.commit()
    await db.refresh(task)


async def _send_session_event(uid: str, session_id: str, event: dict) -> None:
    from app.adapters.chat_ws import ws_chat_adapter

    await ws_chat_adapter.send_session_event(uid, session_id, event)


async def _save_and_notify_reply_error(task_id: int, error_message: str) -> None:
    async with AsyncSessionLocal() as db:
        task = await background_task_crud.get(db, task_id)
        if not task:
            return

        uid = task.uid
        session_id = task.session_id
        profile_id = task.profile_id
        error_content = t("ERR_BACKGROUND_TASK_PROACTIVE_REPLY_FAILED", error=error_message)
        err_message = InternalMessage(role=MessageRole.ERR, content=error_content)
        await save_message(db, session_id, uid, MessageRole.ERR, MessageType.TEXT, err_message, profile_id, is_processed=True)
        await background_task_crud.set_reply_status(db, task=task, status=BackgroundTaskReplyStatus.FAILED, error=error_message)

    await _send_session_event(
        uid,
        session_id,
        {
            "type": "proactive_reply_error",
            "source": "background_task",
            "session_id": session_id,
            "content": error_content,
            "task_id": task_id,
            "background_task_id": task_id,
        },
    )


async def trigger_background_task_reply(task_id: int) -> None:
    async with AsyncSessionLocal() as db:
        task = await background_task_crud.get(db, task_id)
        if not task or not task.auto_reply:
            logger.bind(task_id=task_id).info(t("LOG_BACKGROUND_TASK_REPLY_TRIGGER_SKIPPED"))
            return
        if task.status not in {BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED}:
            logger.bind(task_id=task_id, status=getattr(task, "status", None)).info(t("LOG_BACKGROUND_TASK_REPLY_TRIGGER_SKIPPED"))
            return
        if task.reply_status not in {BackgroundTaskReplyStatus.PENDING, BackgroundTaskReplyStatus.FAILED}:
            logger.bind(task_id=task_id, reply_status=task.reply_status).info(t("LOG_BACKGROUND_TASK_REPLY_TRIGGER_SKIPPED"))
            return

        session = await session_crud.get_by_session_id(db, task.session_id)
        if not session:
            await background_task_crud.set_reply_status(db, task=task, status=BackgroundTaskReplyStatus.FAILED, error="Session not found")
            logger.bind(task_id=task_id, session_id=task.session_id).warning(t("LOG_BACKGROUND_TASK_REPLY_SESSION_MISSING"))
            return

        await _save_background_task_result_message(db, task)
        await background_task_crud.set_reply_status(db, task=task, status=BackgroundTaskReplyStatus.RUNNING)

    try:
        from app.core.dispatcher import ChatDispatcher

        response = await ChatDispatcher.dispatch_proactive_reply(task_id)

        async with AsyncSessionLocal() as db:
            task = await background_task_crud.get(db, task_id)
            if task:
                if response.get("deferred"):
                    await background_task_crud.set_reply_status(db, task=task, status=BackgroundTaskReplyStatus.PENDING)
                    logger.bind(task_id=task_id, uid=task.uid, session_id=task.session_id).info(t("LOG_BACKGROUND_TASK_REPLY_DEFERRED"))
                    asyncio.create_task(_retry_later(task_id))
                    return
                await background_task_crud.set_reply_status(db, task=task, status=BackgroundTaskReplyStatus.SUCCEEDED)

        await _send_session_event(
            response["uid"],
            response["session_id"],
            {
                "type": "proactive_reply",
                "source": "background_task",
                "session_id": response["session_id"],
                "history": response.get("history", []),
                "content": response.get("content", ""),
                "task_id": task_id,
                "background_task_id": task_id,
            },
        )
    except Exception as exc:
        error_message = format_exception_message(exc)
        logger.bind(task_id=task_id).error(t("LOG_BACKGROUND_TASK_PROACTIVE_REPLY_FAILED", error=error_message), exc_info=True)
        await _save_and_notify_reply_error(task_id, error_message)


async def _retry_later(task_id: int) -> None:
    logger.bind(task_id=task_id).info(t("LOG_BACKGROUND_TASK_REPLY_RETRY_SCHEDULED"))
    await asyncio.sleep(2)
    await trigger_background_task_reply(task_id)

