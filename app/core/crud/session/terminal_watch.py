import asyncio
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_SESSION_REPLY_LEASE_LOST, SESSION_REPLY_WORK_CLAIM_INFO_KEY, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim, session_reply_work_item_crud
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.providers.database.time import get_database_timestamp

__all__ = ["CRUDTerminalWatchReply", "terminal_watch_reply_crud"]


_REPLYABLE_TASK_STATUSES = (BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED)
_ACTIVE_WORK_STATUSES = (
    SessionReplyWorkStatus.RUNNING,
    SessionReplyWorkStatus.READY_FOR_LLM,
    SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
)
_MANUAL_WATCH_STATUSES = (
    BackgroundTaskStatus.PENDING,
    BackgroundTaskStatus.RUNNING,
    BackgroundTaskStatus.SUCCEEDED,
    BackgroundTaskStatus.FAILED,
    BackgroundTaskStatus.CANCELLED,
)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _origin_binding(task: BackgroundTask) -> tuple[int, str] | None:
    extra = task.extra if isinstance(task.extra, dict) else {}
    work_id = extra.get("origin_work_id")
    identity = extra.get("origin_work_identity")
    if not _is_positive_int(work_id) or not isinstance(identity, str) or not identity.strip():
        return None
    return work_id, identity


def _status_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _contains_task_id(value: Any, task_id: int) -> bool:
    if isinstance(value, dict):
        candidate = value.get("task_id")
        return _is_positive_int(candidate) and candidate == task_id
    if _is_positive_int(value):
        return value == task_id
    return False


def _state_contains_task_id(state: Any, task_id: int) -> bool:
    if not isinstance(state, dict):
        return False
    return any(_contains_task_id(item, task_id) for key in ("pending_tools", "external_resume_context") for item in (state.get(key) if isinstance(state.get(key), list) else []))


class CRUDTerminalWatchReply:
    async def lock_origin(self, db: AsyncSession, task: BackgroundTask) -> SessionReplyWorkItem | None:
        origin = _origin_binding(task)
        if origin is None:
            return None
        work_id, identity = origin
        statement = (
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.uid == task.uid,
                SessionReplyWorkItem.session_id == task.session_id,
                SessionReplyWorkItem.profile_id == task.profile_id,
                SessionReplyWorkItem.dedupe_key == identity,
            )
            .values(updated_at=SessionReplyWorkItem.updated_at)
            .returning(SessionReplyWorkItem)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        result = await db.execute(statement)
        return result.scalars().first()

    async def list_pending_results(self, db: AsyncSession, work: SessionReplyWorkItem) -> list[BackgroundTask]:
        if not _is_positive_int(work.id) or not isinstance(work.dedupe_key, str) or not work.dedupe_key.strip():
            return []
        statement = (
            select(BackgroundTask)
            .where(
                BackgroundTask.uid == work.uid,
                BackgroundTask.session_id == work.session_id,
                BackgroundTask.profile_id == work.profile_id,
                BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
                BackgroundTask.extra["auto_terminal_watch"].as_boolean().is_(True),
                BackgroundTask.extra["origin_work_id"].as_integer() == work.id,
                BackgroundTask.extra["origin_work_identity"].as_string() == work.dedupe_key,
                BackgroundTask.status.in_(_REPLYABLE_TASK_STATUSES),
                BackgroundTask.auto_reply.is_(True),
                BackgroundTask.reply_status == BackgroundTaskReplyStatus.PENDING,
            )
            .order_by(BackgroundTask.id.asc())
            .execution_options(populate_existing=True)
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def list_watch_states(self, db: AsyncSession, *, uid: str, session_id: str, profile_id: int) -> list[dict]:
        statement = (
            select(
                BackgroundTask.id,
                BackgroundTask.tool_call_id,
                BackgroundTask.arguments,
                BackgroundTask.status,
                BackgroundTask.reply_status,
                BackgroundTask.auto_reply,
                BackgroundTask.created_at,
                BackgroundTask.extra["auto_terminal_watch"].as_boolean().label("auto_terminal_watch"),
                BackgroundTask.result["content"]["event"].as_string().label("event"),
            )
            .where(
                BackgroundTask.uid == uid,
                BackgroundTask.session_id == session_id,
                BackgroundTask.profile_id == profile_id,
                BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
            )
            .order_by(BackgroundTask.id.asc())
        )
        result = await db.execute(statement)
        return [dict(row) for row in result.mappings().all()]

    async def record_terminal_read(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        terminal_session_id: str,
        start_offset: int,
        next_offset: int,
    ) -> None:
        claim = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
        if not isinstance(claim, tuple) or len(claim) != 2 or not _is_positive_int(claim[0]) or not isinstance(claim[1], str) or not claim[1].strip():
            return

        work_id, worker_id = claim
        await ensure_session_reply_work_claim(db)
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None:
            raise asyncio.CancelledError(t(ERR_SESSION_REPLY_LEASE_LOST))
        await db.refresh(work)
        if work.uid != uid or work.session_id != session_id or work.profile_id != profile_id or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id:
            raise asyncio.CancelledError(t(ERR_SESSION_REPLY_LEASE_LOST))

        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        terminal_reads = dict(state.get("terminal_reads")) if isinstance(state.get("terminal_reads"), dict) else {}
        terminal_reads[terminal_session_id] = {
            "start_offset": start_offset,
            "next_offset": next_offset,
        }
        state["terminal_reads"] = terminal_reads
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"execution_state": state},
            commit=True,
        )
        if not updated:
            raise asyncio.CancelledError(t(ERR_SESSION_REPLY_LEASE_LOST))
        work.execution_state = state

    async def has_delivered_terminal_read(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        terminal_session_id: str,
        start_offset: int,
        next_offset: int,
    ) -> bool:
        statement = (
            select(SessionReplyWorkItem.id)
            .join(Message, Message.id == SessionReplyWorkItem.result_message_id)
            .where(
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.profile_id == profile_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.SUCCEEDED,
                SessionReplyWorkItem.event_sent.is_(True),
                SessionReplyWorkItem.result_message_id.is_not(None),
                SessionReplyWorkItem.execution_state["terminal_reads"][terminal_session_id]["start_offset"].as_integer() <= next_offset,
                SessionReplyWorkItem.execution_state["terminal_reads"][terminal_session_id]["next_offset"].as_integer() >= next_offset,
                Message.uid == uid,
                Message.session_id == session_id,
                Message.profile_id == profile_id,
                Message.role == MessageRole.ASSISTANT,
                Message.type == MessageType.TEXT,
            )
            .limit(1)
        )
        result = await db.execute(statement)
        return result.scalar() is not None

    async def manual_watch_covers(self, db: AsyncSession, task: BackgroundTask) -> bool:
        arguments = task.arguments if isinstance(task.arguments, dict) else {}
        terminal_session_id = arguments.get("terminal_session_id")
        if not isinstance(terminal_session_id, str) or not terminal_session_id.strip():
            return False

        statement = (
            select(BackgroundTask)
            .where(
                BackgroundTask.uid == task.uid,
                BackgroundTask.session_id == task.session_id,
                BackgroundTask.profile_id == task.profile_id,
                BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
                BackgroundTask.arguments["terminal_session_id"].as_string() == terminal_session_id,
                BackgroundTask.status.in_(_MANUAL_WATCH_STATUSES),
            )
            .order_by(BackgroundTask.id.desc())
            .execution_options(populate_existing=True)
        )
        result = await db.execute(statement)
        for manual_task in result.scalars():
            extra = manual_task.extra if isinstance(manual_task.extra, dict) else {}
            if extra.get("auto_terminal_watch") is True:
                continue
            manual_arguments = manual_task.arguments if isinstance(manual_task.arguments, dict) else {}
            if manual_arguments.get("terminal_session_id") != terminal_session_id:
                continue
            if _status_value(manual_task.status) in {
                BackgroundTaskStatus.PENDING.value,
                BackgroundTaskStatus.RUNNING.value,
            }:
                return True

            if _status_value(manual_task.status) not in {
                BackgroundTaskStatus.SUCCEEDED.value,
                BackgroundTaskStatus.FAILED.value,
                BackgroundTaskStatus.CANCELLED.value,
            } or not _is_positive_int(manual_task.id):
                continue
            origin = _origin_binding(manual_task)
            if origin is None:
                continue
            origin_work_id, origin_identity = origin
            work_result = await db.execute(
                select(SessionReplyWorkItem)
                .where(
                    SessionReplyWorkItem.id == origin_work_id,
                    SessionReplyWorkItem.uid == task.uid,
                    SessionReplyWorkItem.session_id == task.session_id,
                    SessionReplyWorkItem.profile_id == task.profile_id,
                    SessionReplyWorkItem.dedupe_key == origin_identity,
                )
                .execution_options(populate_existing=True)
            )
            origin_work = work_result.scalars().first()
            if origin_work is None or _status_value(origin_work.status) not in {status.value for status in _ACTIVE_WORK_STATUSES}:
                continue
            if _state_contains_task_id(origin_work.execution_state, manual_task.id):
                return True
        return False

    async def consume_result(self, db: AsyncSession, task: BackgroundTask) -> bool:
        if not _is_positive_int(task.id):
            return False
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task.id,
                BackgroundTask.status.in_(_REPLYABLE_TASK_STATUSES),
                BackgroundTask.auto_reply.is_(True),
                BackgroundTask.reply_status.in_(
                    [
                        BackgroundTaskReplyStatus.PENDING,
                        BackgroundTaskReplyStatus.RUNNING,
                    ]
                ),
            )
            .values(
                reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
                reply_locked_by=None,
                reply_lock_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def reopen_completed(self, db: AsyncSession, work: SessionReplyWorkItem) -> bool:
        if not _is_positive_int(work.id) or _status_value(work.status) != SessionReplyWorkStatus.SUCCEEDED.value:
            return False
        if not isinstance(work.dedupe_key, str) or not work.dedupe_key.strip():
            return False

        current_result = await db.execute(
            select(SessionReplyWorkItem.id)
            .where(
                SessionReplyWorkItem.id == work.id,
                SessionReplyWorkItem.uid == work.uid,
                SessionReplyWorkItem.session_id == work.session_id,
                SessionReplyWorkItem.profile_id == work.profile_id,
                SessionReplyWorkItem.dedupe_key == work.dedupe_key,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.SUCCEEDED,
            )
            .limit(1)
        )
        if current_result.scalar() is None:
            return False

        sequence_no = await session_reply_work_item_crud.allocate_sequence_no(db, work.session_id)
        available_at = await get_database_timestamp(db)
        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        state.pop("response", None)
        state.pop("pending_tools", None)
        state.pop("external_resume_context", None)
        generation = state.get("terminal_resume_generation")
        if not isinstance(generation, int) or isinstance(generation, bool):
            generation = 0
        state.update(
            {
                "terminal_resume_generation": generation + 1,
                "terminal_auto_resume": True,
                "external_resume_pending": True,
                "dispatcher_checkpoint": {
                    "messages": [],
                    "turn_messages": [],
                    "reload_history": True,
                },
            }
        )
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work.id,
                SessionReplyWorkItem.uid == work.uid,
                SessionReplyWorkItem.session_id == work.session_id,
                SessionReplyWorkItem.profile_id == work.profile_id,
                SessionReplyWorkItem.dedupe_key == work.dedupe_key,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.SUCCEEDED,
            )
            .values(
                status=SessionReplyWorkStatus.READY_FOR_LLM,
                sequence_no=sequence_no,
                available_at=available_at,
                locked_by=None,
                lock_until=None,
                result_message_id=None,
                error=None,
                event_sent=False,
                attempt_count=0,
                execution_state=state,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1


terminal_watch_reply_crud = CRUDTerminalWatchReply()
