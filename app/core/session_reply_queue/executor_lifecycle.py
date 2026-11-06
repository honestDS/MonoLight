import time

from sqlalchemy import update

from app.core.background_tasks.external_work import (
    complete_stopped_task_replies,
    prepare_external_work_resume,
    resume_external_work_for_task,
)
from app.core.background_tasks.terminal_watch_reply import prepare_terminal_auto_watch_resume
from app.core.constants import (
    ERR_INTERNAL_SERVER_ERROR,
    ERR_LLM_UNEXPECTED_ERROR,
    ERR_SESSION_REPLY_FINAL_MESSAGE_NOT_PERSISTED,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.event import session_event_crud
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim, session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.message_platforms.notifier import send_session_event
from app.core.session_reply_queue.child_checkpoint import recover_child_work_wait
from app.core.session_reply_queue.child_delivery import deliver_child_work_results, resume_child_work_for_parent
from app.core.session_reply_queue.child_lifecycle import finish_child_work, prepare_child_completion
from app.core.session_reply_queue.executor_agent_work import execute_agent_work, load_agent_work_spec
from app.core.session_reply_queue.executor_common import (
    _error_message_dedupe_key,
    _event_for_work,
    _get_persisted_result,
    _response_from_persisted_message,
    _result_message_dedupe_key,
)
from app.core.session_reply_queue.executor_confirmed import _execute_confirmed_tools
from app.core.session_reply_queue.executor_replies import (
    _execute_background,
    _execute_background_task_resume,
    _execute_external_resume,
    _execute_foreground,
)
from app.core.session_reply_queue.manager import build_identified_work_response
from app.core.utils.dispatcher.save_message import save_message
from app.models.agent_work import AgentWorkReportTarget
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

__all__ = [
    "execute_session_reply_work",
    "fail_session_reply_work",
    "retry_delay_seconds",
    "lease_deadline",
]


async def _resume_pending_external_results(work: SessionReplyWorkItem) -> None:
    try:
        async with AsyncSessionLocal() as db:
            pending_results = await background_task_crud.list_pending_terminal_watch_results(
                db,
                uid=work.uid,
                session_id=work.session_id,
                profile_id=work.profile_id,
                origin_work_id=work.id,
                origin_work_identity=work.dedupe_key,
            )
            task_ids: list[int] = []
            seen_task_ids: set[int] = set()
            for task in pending_results:
                task_id = task.id
                if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0 or task_id in seen_task_ids:
                    continue
                seen_task_ids.add(task_id)
                task_ids.append(task_id)
            execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
            for field in ("pending_tools", "terminal_watch_waits"):
                pending_slots = execution_state.get(field)
                if not isinstance(pending_slots, list):
                    continue
                for pending_slot in pending_slots:
                    if not isinstance(pending_slot, dict):
                        continue
                    task_id = pending_slot.get("task_id")
                    if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0 or task_id in seen_task_ids:
                        continue
                    seen_task_ids.add(task_id)
                    task_ids.append(task_id)
    except Exception:
        logger.exception("Failed to list pending external results for work {}", work.id)
        return

    for task_id in task_ids:
        try:
            await resume_external_work_for_task(task_id)
        except Exception:
            logger.exception("Failed to resume external work for task {}", task_id)


async def _promote_independent_work_message(db, work: SessionReplyWorkItem, message_id: int | None) -> bool:
    message_work_id = await session_reply_work_item_crud.get_message_work_id(db, work)
    if message_work_id is None:
        return False
    if work.subagent_id is not None:
        if message_id is None:
            return False
        message = await db.get(Message, message_id)
        if message is None:
            raise RuntimeError(t(ERR_SESSION_REPLY_FINAL_MESSAGE_NOT_PERSISTED))
        if message.type != MessageType.AUDIT_CONFIRMATION:
            return False
    elif work.work_type == SessionReplyWorkType.AGENT_WORK and load_agent_work_spec(work).report_target != AgentWorkReportTarget.SESSION:
        return False
    await ensure_session_reply_work_claim(db)
    if message_id is None or not await message_crud.promote_work_message_to_session(db, message_id=message_id, work_id=message_work_id, commit=False):
        raise RuntimeError(t(ERR_SESSION_REPLY_FINAL_MESSAGE_NOT_PERSISTED))
    return True


async def execute_session_reply_work(work_id: int, worker_id: str) -> None:
    """执行已领取的会话回复工作并完成结果投递。"""
    async with AsyncSessionLocal() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id:
            return
        db.info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, worker_id)
        is_independent_work = (await session_reply_work_item_crud.get_message_work_id(db, work)) is not None

        if isinstance(work.execution_state, dict) and work.execution_state.get("terminal_auto_resume") is True:
            if not await prepare_terminal_auto_watch_resume(db, work, worker_id):
                return

        execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
        saved_response = execution_state.get("response")
        saved_skip_response = isinstance(saved_response, dict) and saved_response.get("skip_reply") is True
        persisted_result = None
        skip_reply = saved_skip_response
        recovered_child_wait = None
        if execution_state.get("has_child_work") is True:
            recovered_child_wait = await recover_child_work_wait(db, work=work, worker_id=worker_id)
        if recovered_child_wait is not None:
            response = recovered_child_wait
        elif saved_skip_response:
            response = saved_response
        else:
            persisted_result = await _get_persisted_result(db, work)
            if persisted_result is not None:
                response = execution_state.get("response") or _response_from_persisted_message(work, persisted_result)
            elif execution_state.get("external_resume_pending") is True:
                response = await _execute_external_resume(db, work, worker_id)
            elif work.work_type == SessionReplyWorkType.FOREGROUND_REPLY:
                response = await _execute_foreground(db, work, worker_id)
            elif work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
                response = await _execute_confirmed_tools(db, work, worker_id)
            elif work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
                response = await _execute_background(db, work, worker_id)
            elif work.work_type == SessionReplyWorkType.BACKGROUND_TASK_RESUME:
                response = await _execute_background_task_resume(db, work, worker_id)
            elif work.work_type == SessionReplyWorkType.AGENT_WORK:
                response = await execute_agent_work(db, work, worker_id)
            else:
                raise RuntimeError(t(ERR_INTERNAL_SERVER_ERROR))
            skip_reply = isinstance(response, dict) and response.get("skip_reply") is True

        if isinstance(response, dict) and response.get("status") == "waiting_external_work":
            pending_tools = response.get("pending_tools")
            if not isinstance(pending_tools, list) or not pending_tools or any(not isinstance(pending_tool, dict) for pending_tool in pending_tools):
                raise RuntimeError(t(ERR_INTERNAL_SERVER_ERROR))
            if not await external_work_crud.suspend_claimed(
                db,
                work=work,
                worker_id=worker_id,
                pending_tools=response["pending_tools"],
                commit=True,
            ):
                return
            child_work_id = next(
                (pending_tool.get("child_work_id") for pending_tool in pending_tools if isinstance(pending_tool.get("child_work_id"), int) and not isinstance(pending_tool.get("child_work_id"), bool) and pending_tool.get("child_work_id") > 0),
                None,
            )
            if child_work_id is not None:
                async with AsyncSessionLocal() as delivery_db:
                    await resume_child_work_for_parent(delivery_db, parent_work_id=work.id)
            for pending_tool in pending_tools:
                task_id = pending_tool.get("task_id")
                if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
                    continue
                try:
                    await resume_external_work_for_task(task_id)
                except Exception:
                    logger.exception("Failed to resume external work for task {}", task_id)
            await _resume_pending_external_results(work)
            return

        direct_independent_work = False
        if skip_reply:
            result_message = None
            identified_response = response if saved_skip_response else build_identified_work_response(work, response, message_id=None)
        else:
            result_message = persisted_result or await message_crud.get_by_dedupe_key(db, _result_message_dedupe_key(work))
            if result_message is None:
                raise RuntimeError(t(ERR_SESSION_REPLY_FINAL_MESSAGE_NOT_PERSISTED))
            direct_independent_work = await _promote_independent_work_message(db, work, result_message.id)
            if direct_independent_work:
                response = {**response, "history": []}
            identified_response = build_identified_work_response(work, response, message_id=result_message.id)

        latest_execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
        state = {**latest_execution_state, "response": identified_response}
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"result_message_id": result_message.id if result_message is not None else None, "execution_state": state},
            commit=False,
        )
        if not updated:
            return

        event_sent = False
        if skip_reply:
            if not is_independent_work:
                event = _event_for_work(work, identified_response)
                event.update({"type": "done", "skip_reply": True, "response": identified_response})
                await session_event_crud.publish(
                    db,
                    dedupe_key=event["event_id"],
                    uid=work.uid,
                    session_id=work.session_id,
                    event=event,
                    commit=False,
                )
                event_sent = True
        else:
            event_sent = not is_independent_work or direct_independent_work
        await db.commit()

    if not skip_reply and event_sent:
        await send_session_event(work.uid, work.session_id, _event_for_work(work, identified_response))

    async with AsyncSessionLocal() as db:
        actual = await session_reply_work_item_crud.get(db, work_id)
        if actual is None:
            await db.rollback()
            return
        if actual.subagent_id is not None:
            requeued = await prepare_child_completion(
                db,
                work=actual,
                worker_id=worker_id,
                result_message_id=result_message.id if result_message is not None else None,
                skip_reply=skip_reply,
            )
            if requeued:
                await db.commit()
                return
        updated = await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=work_id,
            worker_id=worker_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            result_message_id=result_message.id if result_message is not None else None,
            event_sent=event_sent,
            commit=False,
        )
        if not updated:
            await db.rollback()
            return

        if actual.subagent_id is not None:
            await finish_child_work(db, work=actual)

        await external_work_crud.finish_received_results(db, work=work, commit=False)

        if result_message is not None and (work.execution_state or {}).get("stopped_task_ids"):
            await complete_stopped_task_replies(db, work_id=work_id)

        if work.work_type in {SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.BACKGROUND_TASK_RESUME}:
            await db.execute(
                update(BackgroundTask)
                .where(
                    BackgroundTask.id == int(work.source_id),
                    BackgroundTask.reply_status == BackgroundTaskReplyStatus.RUNNING,
                )
                .values(
                    reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
                    reply_locked_by=None,
                    reply_lock_until=None,
                )
            )
        deliver_child_results = actual.subagent_id is not None
        child_work_id = None
        if deliver_child_results:
            execution_state = actual.execution_state if isinstance(actual.execution_state, dict) else {}
            child_work_id = execution_state.get("child_work_id", actual.id)
        await db.commit()
        if deliver_child_results:
            async with AsyncSessionLocal() as delivery_db:
                await deliver_child_work_results(
                    delivery_db,
                    child_work_id=child_work_id,
                )
        else:
            execution_state = actual.execution_state if isinstance(actual.execution_state, dict) else {}
            child_work_waits = execution_state.get("child_work_waits")
            if isinstance(child_work_waits, list) and child_work_waits:
                async with AsyncSessionLocal() as delivery_db:
                    await resume_child_work_for_parent(delivery_db, parent_work_id=work_id)

    await _resume_pending_external_results(work)


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
        db_info = getattr(db, "info", None)
        if isinstance(db_info, dict):
            db_info[SESSION_REPLY_WORK_CLAIM_INFO_KEY] = (work_id, worker_id)
        is_independent_work = (await session_reply_work_item_crud.get_message_work_id(db, work)) is not None
        checkpoint = await session_reply_work_item_crud.get_checkpoint(db, work.id)
        if isinstance(checkpoint, dict) and checkpoint.get("deferred_external_results") is True:
            await prepare_external_work_resume(
                db,
                uid=work.uid,
                session_id=work.session_id,
                profile_id=work.profile_id,
                checkpoint=checkpoint,
            )
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
            commit=False,
        )
        direct_independent_work = await _promote_independent_work_message(db, work, error_message.id)
        identified_response = build_identified_work_response(
            work,
            {"content": error_content, "history": []} if direct_independent_work else {"content": error_content},
            message_id=error_message.id,
        )
        event_sent = not is_independent_work or direct_independent_work
        await db.commit()

    if event_sent:
        await send_session_event(work.uid, work.session_id, _event_for_work(work, identified_response, error=True))

    async with AsyncSessionLocal() as db:
        if work.work_type in {SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.BACKGROUND_TASK_RESUME}:
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
        updated = await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=work_id,
            worker_id=worker_id,
            status=SessionReplyWorkStatus.FAILED,
            result_message_id=error_message.id,
            error=error,
            event_sent=event_sent,
            commit=False,
        )
        if updated and work.subagent_id is not None:
            await finish_child_work(db, work=work)
        deliver_child_results = updated and work.subagent_id is not None
        child_work_id = None
        if deliver_child_results:
            execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
            child_work_id = execution_state.get("child_work_id", work.id)
        await db.commit()
        if deliver_child_results:
            async with AsyncSessionLocal() as delivery_db:
                await deliver_child_work_results(
                    delivery_db,
                    child_work_id=child_work_id,
                )


def retry_delay_seconds(attempt_count: int) -> int:
    return min(300, 2 ** max(0, attempt_count - 1))


def lease_deadline() -> int:
    return int(time.time())
