import json

from sqlalchemy import update

from app.core.crud.session.session import session_crud
from app.core.crud.task.background import background_task_crud
from app.core.log import get_logger
from app.core.prompts import BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

BACKGROUND_RESULT_MESSAGE_SAVED_EXTRA_KEY = "background_result_message_saved"


def _build_background_message_dedupe_key(task_id: int, purpose: str) -> str:
    return f"background-task:{task_id}:{purpose}"


def _build_background_tool_result_message(task: BackgroundTask) -> str:
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

        result_message = Message(
            session_id=task.session_id,
            uid=task.uid,
            role=MessageRole.ASSISTANT,
            type=MessageType.BACKGROUND_TASK_RESULT,
            content=_build_background_tool_result_message(task),
            profile_id=task.profile_id,
            is_processed=True,
            dedupe_key=_build_background_message_dedupe_key(task_id, "result"),
        )
        db.add(result_message)
        await db.flush()
        await session_reply_queue_manager.enqueue_background_summary(
            db,
            uid=task.uid,
            session_id=task.session_id,
            profile_id=task.profile_id,
            background_task_id=task_id,
            commit=False,
        )
        task.extra = {
            **(task.extra if isinstance(task.extra, dict) else {}),
            BACKGROUND_RESULT_MESSAGE_SAVED_EXTRA_KEY: True,
        }
        db.add(task)
        await db.commit()
        logger.bind(task_id=task_id, uid=task.uid, session_id=task.session_id).info("Background task summary queued")
