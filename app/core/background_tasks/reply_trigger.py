import asyncio
import json
import uuid
from time import monotonic

from app.core import constants
from app.core.crud.background_task import background_task_crud
from app.core.crud.session import session_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT
from app.core.utils.dispatcher.helpers import format_exception_message
from app.core.utils.dispatcher.save_message import save_message
from app.models.background_task import BackgroundTaskReplyStatus
from app.models.message import InternalMessage, MessageRole, MessageType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

BACKGROUND_RESULT_MESSAGE_SAVED_EXTRA_KEY = "background_result_message_saved"
BACKGROUND_TASK_REPLY_LEASE_SECONDS = 300
BACKGROUND_TASK_REPLY_LEASE_RENEW_INTERVAL_SECONDS = 100
BACKGROUND_TASK_REPLY_LEASE_RETRY_MAX_SECONDS = 10
BACKGROUND_TASK_REPLY_LEASE_SAFETY_MARGIN_SECONDS = 10
BACKGROUND_TASK_REPLY_STATE_RETRY_MAX_SECONDS = 10


def _build_background_message_dedupe_key(task_id: int, purpose: str) -> str:
    return f"background-task:{task_id}:{purpose}"


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
        dedupe_key=_build_background_message_dedupe_key(task.id, "result"),
    )
    task.extra = {**extra, BACKGROUND_RESULT_MESSAGE_SAVED_EXTRA_KEY: True}
    db.add(task)
    await db.commit()
    await db.refresh(task)


async def _send_session_event(uid: str, session_id: str, event: dict) -> None:
    from app.core.message_platforms.notifier import send_session_event

    await send_session_event(uid, session_id, event)


async def _renew_reply_lease(task_id: int, worker_id: str) -> bool:
    lease_deadline = monotonic() + BACKGROUND_TASK_REPLY_LEASE_SECONDS
    retry_delay = 1.0
    await asyncio.sleep(BACKGROUND_TASK_REPLY_LEASE_RENEW_INTERVAL_SECONDS)
    while True:
        try:
            async with AsyncSessionLocal() as db:
                renewed = await background_task_crud.renew_reply_lease(
                    db,
                    task_id=task_id,
                    worker_id=worker_id,
                    lease_seconds=BACKGROUND_TASK_REPLY_LEASE_SECONDS,
                )
            if not renewed:
                logger.bind(task_id=task_id, worker_id=worker_id).warning(t("LOG_BACKGROUND_TASK_REPLY_LEASE_LOST"))
                return False
            lease_deadline = monotonic() + BACKGROUND_TASK_REPLY_LEASE_SECONDS
            retry_delay = 1.0
            await asyncio.sleep(BACKGROUND_TASK_REPLY_LEASE_RENEW_INTERVAL_SECONDS)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            remaining = lease_deadline - monotonic() - BACKGROUND_TASK_REPLY_LEASE_SAFETY_MARGIN_SECONDS
            logger.bind(task_id=task_id, worker_id=worker_id).error(
                t(
                    "LOG_BACKGROUND_TASK_REPLY_LEASE_RENEW_FAILED",
                    error=format_exception_message(exc),
                    retry_seconds=max(0, remaining),
                ),
                exc_info=True,
            )
            if remaining <= 0:
                return False
            await asyncio.sleep(min(retry_delay, remaining))
            retry_delay = min(retry_delay * 2, BACKGROUND_TASK_REPLY_LEASE_RETRY_MAX_SECONDS)


async def _release_reply_claim(task_id: int, worker_id: str) -> None:
    async with AsyncSessionLocal() as db:
        await background_task_crud.release_reply_claim(
            db,
            task_id=task_id,
            worker_id=worker_id,
        )


async def _converge_persisted_reply_state(
    task_id: int,
    worker_id: str,
    status: BackgroundTaskReplyStatus,
    error: str | None = None,
) -> None:
    retry_delay = 1.0
    while True:
        try:
            async with AsyncSessionLocal() as db:
                completed = await background_task_crud.complete_reply_claim(
                    db,
                    task_id=task_id,
                    worker_id=worker_id,
                    status=status,
                    error=error,
                )
            if not completed:
                logger.bind(task_id=task_id, worker_id=worker_id).warning(t("LOG_BACKGROUND_TASK_REPLY_STATE_CONVERGENCE_LOST"))
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.bind(task_id=task_id, worker_id=worker_id).error(
                t(
                    "LOG_BACKGROUND_TASK_REPLY_STATE_COMMIT_FAILED",
                    error=format_exception_message(exc),
                    retry_seconds=retry_delay,
                ),
                exc_info=True,
            )
            await asyncio.sleep(retry_delay)
            retry_delay = min(retry_delay * 2, BACKGROUND_TASK_REPLY_STATE_RETRY_MAX_SECONDS)


async def _converge_persisted_reply_success(task_id: int, worker_id: str) -> None:
    await _converge_persisted_reply_state(
        task_id,
        worker_id,
        BackgroundTaskReplyStatus.SUCCEEDED,
    )


async def _converge_persisted_reply_failure(task_id: int, worker_id: str, error: str) -> None:
    await _converge_persisted_reply_state(
        task_id,
        worker_id,
        BackgroundTaskReplyStatus.FAILED,
        error,
    )


async def _save_and_notify_reply_error(task_id: int, worker_id: str, error_message: str) -> None:
    async with AsyncSessionLocal() as db:
        task = await background_task_crud.get(db, task_id)
        if not task:
            return

        uid = task.uid
        session_id = task.session_id
        profile_id = task.profile_id
        error_content = t(constants.ERR_BACKGROUND_TASK_PROACTIVE_REPLY_FAILED, error=error_message)
        err_message = InternalMessage(role=MessageRole.ERR, content=error_content)
        await save_message(
            db,
            session_id,
            uid,
            MessageRole.ERR,
            MessageType.TEXT,
            err_message,
            profile_id,
            is_processed=True,
            dedupe_key=_build_background_message_dedupe_key(task_id, "reply-error"),
        )

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

    await _converge_persisted_reply_failure(task_id, worker_id, error_message)


async def _execute_claimed_reply(task_id: int, worker_id: str) -> None:
    async with AsyncSessionLocal() as db:
        task = await background_task_crud.get(db, task_id)
        if not task:
            return

        session = await session_crud.get_by_session_id(db, task.session_id)
        if not session:
            error_message = t(constants.ERR_SESSION_NOT_FOUND)
            await background_task_crud.complete_reply_claim(
                db,
                task_id=task_id,
                worker_id=worker_id,
                status=BackgroundTaskReplyStatus.FAILED,
                error=error_message,
            )
            logger.bind(task_id=task_id, session_id=task.session_id).warning(t("LOG_BACKGROUND_TASK_REPLY_SESSION_MISSING"))
            return

        await _save_background_task_result_message(db, task)

    try:
        from app.core.dispatcher import ChatDispatcher

        response = await ChatDispatcher.dispatch_proactive_reply(task_id)

        async with AsyncSessionLocal() as db:
            if response.get("deferred"):
                completed = await background_task_crud.complete_reply_claim(
                    db,
                    task_id=task_id,
                    worker_id=worker_id,
                    status=BackgroundTaskReplyStatus.PENDING,
                )
                if completed:
                    logger.bind(task_id=task_id, uid=response["uid"], session_id=response["session_id"]).info(t("LOG_BACKGROUND_TASK_REPLY_DEFERRED"))
                return

        await _send_session_event(
            response["uid"],
            response["session_id"],
            {
                "type": "proactive_reply",
                "source": "background_task",
                "session_id": response["session_id"],
                "history": response.get("history", []),
                "content": response.get("content", ""),
                "files": response.get("files", []),
                "task_id": task_id,
                "background_task_id": task_id,
            },
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_message = format_exception_message(exc)
        logger.bind(task_id=task_id, worker_id=worker_id).error(t("LOG_BACKGROUND_TASK_PROACTIVE_REPLY_FAILED", error=error_message), exc_info=True)
        await _save_and_notify_reply_error(task_id, worker_id, error_message)
        return

    await _converge_persisted_reply_success(task_id, worker_id)


async def trigger_background_task_reply(task_id: int) -> None:
    worker_id = uuid.uuid4().hex
    async with AsyncSessionLocal() as db:
        task = await background_task_crud.try_claim_reply(
            db,
            task_id=task_id,
            worker_id=worker_id,
            lease_seconds=BACKGROUND_TASK_REPLY_LEASE_SECONDS,
        )
    if not task:
        logger.bind(task_id=task_id).info(t("LOG_BACKGROUND_TASK_REPLY_TRIGGER_SKIPPED"))
        return

    execution_task = asyncio.create_task(_execute_claimed_reply(task_id, worker_id))
    lease_task = asyncio.create_task(_renew_reply_lease(task_id, worker_id))
    try:
        done, _pending = await asyncio.wait(
            {execution_task, lease_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if execution_task in done:
            await execution_task
        else:
            lease_renewed = await lease_task
            if not lease_renewed:
                execution_task.cancel()
                await asyncio.gather(execution_task, return_exceptions=True)
                await asyncio.shield(_release_reply_claim(task_id, worker_id))
    except asyncio.CancelledError:
        execution_task.cancel()
        await asyncio.gather(execution_task, return_exceptions=True)
        await asyncio.shield(_release_reply_claim(task_id, worker_id))
        raise
    finally:
        lease_task.cancel()
        await asyncio.gather(lease_task, return_exceptions=True)
