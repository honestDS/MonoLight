import asyncio
from typing import Any

from sqlalchemy import String, and_, cast, func, inspect, literal, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_SESSION_REPLY_LEASE_LOST, SESSION_REPLY_WORK_CLAIM_INFO_KEY, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim, session_reply_work_item_crud
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.terminal_session import TerminalSession
from app.providers.database.time import get_database_timestamp

__all__ = [
    "CRUDTerminalWatchReply",
    "get_terminal_watch_deliveries",
    "terminal_watch_pending_delivery_condition",
    "terminal_watch_reply_crud",
]


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


def get_terminal_watch_deliveries(task: BackgroundTask) -> dict[str, dict[str, Any]]:
    extra = task.extra if isinstance(task.extra, dict) else {}
    raw_deliveries = extra.get("terminal_watch_deliveries")
    if isinstance(raw_deliveries, dict):
        deliveries: dict[str, dict[str, Any]] = {}
        for raw_work_id, raw_delivery in raw_deliveries.items():
            if isinstance(raw_work_id, bool):
                continue
            if isinstance(raw_work_id, int):
                work_id = raw_work_id
            elif isinstance(raw_work_id, str):
                try:
                    work_id = int(raw_work_id)
                except ValueError:
                    continue
            else:
                continue
            if not _is_positive_int(work_id) or not isinstance(raw_delivery, dict):
                continue
            identity = raw_delivery.get("identity")
            if not isinstance(identity, str) or not identity.strip():
                continue
            deliveries[str(work_id)] = {
                "identity": identity,
                "consumed": raw_delivery.get("consumed") is True,
                "routed": raw_delivery.get("routed") is True,
            }
        return deliveries

    origin = _origin_binding(task)
    if origin is None:
        return {}
    work_id, identity = origin
    return {
        str(work_id): {
            "identity": identity,
            "consumed": _status_value(task.reply_status) == BackgroundTaskReplyStatus.SUCCEEDED.value,
            "routed": extra.get("terminal_auto_watch_routed") is True,
        }
    }


def terminal_watch_pending_delivery_condition(work_id: int, identity: str) -> Any:
    ledger = BackgroundTask.extra["terminal_watch_deliveries"]
    delivery = ledger[str(work_id)]
    return or_(
        and_(
            delivery["identity"].as_string() == identity,
            delivery["consumed"].as_boolean().is_(False),
        ),
        and_(
            ledger.as_string().is_(None),
            BackgroundTask.extra["origin_work_id"].as_integer() == work_id,
            BackgroundTask.extra["origin_work_identity"].as_string() == identity,
        ),
    )


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
    async def lock_origin(
        self,
        db: AsyncSession,
        task: BackgroundTask,
        *,
        work_id: int | None = None,
        identity: str | None = None,
    ) -> SessionReplyWorkItem | None:
        origin = _origin_binding(task)
        if work_id is None:
            work_id = origin[0] if origin is not None else None
        if identity is None:
            identity = origin[1] if origin is not None else None
        if not _is_positive_int(work_id) or not isinstance(identity, str) or not identity.strip():
            return None
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

    async def lock_result(self, db: AsyncSession, task: BackgroundTask) -> BackgroundTask | None:
        if not _is_positive_int(task.id):
            return None
        statement = (
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task.id,
                BackgroundTask.uid == task.uid,
                BackgroundTask.session_id == task.session_id,
                BackgroundTask.profile_id == task.profile_id,
            )
            .values(id=BackgroundTask.id)
            .returning(BackgroundTask)
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
                terminal_watch_pending_delivery_condition(work.id, work.dedupe_key),
                BackgroundTask.status.in_(_REPLYABLE_TASK_STATUSES),
                BackgroundTask.auto_reply.is_(True),
                BackgroundTask.reply_status == BackgroundTaskReplyStatus.PENDING,
            )
            .order_by(BackgroundTask.id.asc())
            .execution_options(populate_existing=True)
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

    async def supersede_pending_timeouts(self, db: AsyncSession, task: BackgroundTask) -> int:
        if not _is_positive_int(task.id):
            return 0

        arguments = task.arguments if isinstance(task.arguments, dict) else {}
        terminal_session_id = arguments.get("terminal_session_id")
        if not isinstance(terminal_session_id, str) or not terminal_session_id.strip():
            return 0

        result_payload = task.result if isinstance(task.result, dict) else {}
        content = result_payload.get("content")
        event = content.get("event") if isinstance(content, dict) else None
        if not isinstance(event, str) or event not in {
            "timeout",
            "process_exit",
            "terminal_error",
            "observation_error",
            "output",
            "output_contains",
            "quiet_for",
        }:
            return 0

        deliveries = get_terminal_watch_deliveries(task)
        if not deliveries:
            return 0

        pending_tasks: dict[int, BackgroundTask] = {}
        for raw_work_id, delivery in deliveries.items():
            work_id = int(raw_work_id)
            identity = delivery["identity"]
            result = await db.execute(
                select(BackgroundTask)
                .where(
                    BackgroundTask.uid == task.uid,
                    BackgroundTask.session_id == task.session_id,
                    BackgroundTask.profile_id == task.profile_id,
                    BackgroundTask.tool_name == TERMINAL_WATCH_TOOL_NAME,
                    BackgroundTask.arguments["terminal_session_id"].as_string() == terminal_session_id,
                    BackgroundTask.extra["auto_terminal_watch"].as_boolean().is_(True),
                    BackgroundTask.auto_reply.is_(True),
                    BackgroundTask.status.in_(_REPLYABLE_TASK_STATUSES),
                    BackgroundTask.id < task.id,
                    BackgroundTask.result["content"]["event"].as_string() == "timeout",
                    BackgroundTask.reply_status.in_(
                        [
                            BackgroundTaskReplyStatus.PENDING,
                            BackgroundTaskReplyStatus.RUNNING,
                        ]
                    ),
                    terminal_watch_pending_delivery_condition(work_id, identity),
                )
                .order_by(BackgroundTask.id.asc())
                .execution_options(populate_existing=True)
            )
            for previous_task in result.scalars().all():
                if _is_positive_int(previous_task.id):
                    pending_tasks[previous_task.id] = previous_task

        replaced = 0
        for previous_task in sorted(pending_tasks.values(), key=lambda candidate: candidate.id or 0):
            replaced_task = False
            for raw_work_id, delivery in deliveries.items():
                if await self.consume_result(
                    db,
                    previous_task,
                    work_id=int(raw_work_id),
                    identity=delivery["identity"],
                ):
                    replaced_task = True
            if replaced_task:
                replaced += 1
        return replaced

    async def list_watch_states(self, db: AsyncSession, *, uid: str, session_id: str, profile_id: int) -> list[dict]:
        terminal_session_table_exists = await db.run_sync(lambda sync_session: inspect(sync_session.connection()).has_table(TerminalSession.__tablename__))
        if not terminal_session_table_exists:
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
                    literal(None).label("terminal_status"),
                    literal(None).label("command"),
                    literal(None).label("working_directory"),
                    literal(None).label("next_output_offset"),
                    literal(None).label("request_context"),
                    literal(None).label("purpose"),
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
                func.lower(
                    cast(
                        func.coalesce(
                            TerminalSession.status,
                            BackgroundTask.result["content"]["session_status"].as_string(),
                        ),
                        String,
                    )
                ).label("terminal_status"),
                TerminalSession.command.label("command"),
                TerminalSession.working_directory.label("working_directory"),
                TerminalSession.next_output_offset.label("next_output_offset"),
                BackgroundTask.extra["request_context"].label("request_context"),
                BackgroundTask.extra["purpose"].label("purpose"),
            )
            .outerjoin(
                TerminalSession,
                and_(
                    TerminalSession.terminal_session_id == BackgroundTask.arguments["terminal_session_id"].as_string(),
                    TerminalSession.uid == BackgroundTask.uid,
                    TerminalSession.session_id == BackgroundTask.session_id,
                    TerminalSession.profile_id == BackgroundTask.profile_id,
                ),
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

    async def list_terminal_watch_waiting_work(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
    ) -> list[SessionReplyWorkItem]:
        statement = (
            select(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.profile_id == profile_id,
                SessionReplyWorkItem.status.in_(
                    [
                        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                        SessionReplyWorkStatus.READY_FOR_LLM,
                        SessionReplyWorkStatus.RUNNING,
                    ]
                ),
            )
            .order_by(SessionReplyWorkItem.id.asc())
            .execution_options(populate_existing=True)
        )
        result = await db.execute(statement)
        return list(result.scalars().all())

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

    async def consume_result(
        self,
        db: AsyncSession,
        task: BackgroundTask,
        *,
        work_id: int | None = None,
        identity: str | None = None,
    ) -> bool:
        locked_task = await self.lock_result(db, task)
        if locked_task is None:
            return False
        await db.refresh(locked_task)
        task = locked_task
        if (
            _status_value(task.status) not in {status.value for status in _REPLYABLE_TASK_STATUSES}
            or task.auto_reply is not True
            or _status_value(task.reply_status)
            not in {
                BackgroundTaskReplyStatus.PENDING.value,
                BackgroundTaskReplyStatus.RUNNING.value,
            }
        ):
            return False

        specified_work = work_id is not None or identity is not None
        if specified_work and (not _is_positive_int(work_id) or not isinstance(identity, str) or not identity.strip()):
            return False

        extra = task.extra if isinstance(task.extra, dict) else {}
        raw_deliveries = extra.get("terminal_watch_deliveries")
        deliveries = get_terminal_watch_deliveries(task)
        if specified_work:
            delivery = deliveries.get(str(work_id))
            if delivery is None or delivery["identity"] != identity or delivery["consumed"]:
                return False
            updated_deliveries = {key: dict(value) for key, value in deliveries.items()}
            updated_deliveries[str(work_id)] = {
                "identity": delivery["identity"],
                "consumed": True,
                "routed": True,
            }
        else:
            if isinstance(raw_deliveries, dict) and deliveries and all(delivery["consumed"] for delivery in deliveries.values()):
                return False
            updated_deliveries = {
                key: {
                    "identity": delivery["identity"],
                    "consumed": True,
                    "routed": True,
                }
                for key, delivery in deliveries.items()
            }

        all_consumed = not updated_deliveries or all(delivery["consumed"] for delivery in updated_deliveries.values())
        updated_extra = dict(extra)
        if specified_work or isinstance(raw_deliveries, dict):
            updated_extra["terminal_watch_deliveries"] = updated_deliveries
        updated_extra["terminal_auto_watch_routed"] = all(delivery["routed"] or delivery["consumed"] for delivery in updated_deliveries.values())
        task.extra = updated_extra
        task.reply_status = BackgroundTaskReplyStatus.SUCCEEDED if all_consumed else BackgroundTaskReplyStatus.PENDING
        task.reply_locked_by = None
        task.reply_lock_until = None
        db.add(task)
        await db.flush()
        return True

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
