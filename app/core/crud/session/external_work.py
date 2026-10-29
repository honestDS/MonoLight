from datetime import datetime
from typing import Any

from sqlalchemy import and_, func, or_, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import GOAL_EXECUTION_PHASE_RUNNING, SESSION_REPLY_WORK_CLAIM_INFO_KEY, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim, session_reply_work_item_crud
from app.core.crud.session.terminal_watch import get_terminal_watch_deliveries
from app.core.log import get_logger
from app.core.utils.time import get_local_time
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.providers.database.time import get_database_timestamp

__all__ = ["CRUDExternalWork", "external_work_crud"]

logger = get_logger(__name__)


def _request_ids(state: dict[str, Any]) -> list[str]:
    request_ids = state.get("request_ids")
    if not isinstance(request_ids, list):
        return []

    result: list[str] = []
    seen: set[str] = set()
    for request_id in request_ids:
        if isinstance(request_id, str) and request_id and request_id not in seen:
            seen.add(request_id)
            result.append(request_id)
    return result


class CRUDExternalWork:
    async def record_stopped_tasks(
        self,
        db: AsyncSession,
        *,
        task_ids: list[int],
    ) -> None:
        if not task_ids:
            return

        claim = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
        if not claim:
            return

        await ensure_session_reply_work_claim(db)
        work_id, _ = claim
        work = await session_reply_work_item_crud.get(db, work_id)
        await db.refresh(work)

        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        existing_task_ids = state.get("stopped_task_ids")
        existing_task_ids = existing_task_ids if isinstance(existing_task_ids, list) else []
        state["stopped_task_ids"] = list(dict.fromkeys([*existing_task_ids, *task_ids]))
        work.execution_state = state
        await db.commit()

    async def list_stopped_task_replies(
        self,
        db: AsyncSession,
        *,
        uid: str,
        task_ids: list[int],
        exclude_work_id: int,
    ) -> tuple[list[BackgroundTask], list[SessionReplyWorkItem]]:
        if not task_ids:
            return [], []

        task_result = await db.execute(select(BackgroundTask).where(BackgroundTask.id.in_(task_ids), BackgroundTask.uid == uid).execution_options(populate_existing=True))
        tasks = list(task_result.scalars().all())
        if not tasks:
            return [], []

        work_conditions = []
        for task in tasks:
            if task.id is None:
                continue
            work_conditions.append(
                and_(
                    SessionReplyWorkItem.uid == task.uid,
                    SessionReplyWorkItem.session_id == task.session_id,
                    SessionReplyWorkItem.profile_id == task.profile_id,
                )
            )

        if not work_conditions:
            return tasks, []

        candidate_result = await db.execute(
            select(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id != exclude_work_id,
                SessionReplyWorkItem.status.in_(SESSION_REPLY_ACTIVE_STATUSES),
                or_(*work_conditions),
            )
            .execution_options(populate_existing=True)
        )
        candidates = list(candidate_result.scalars().all())
        matching_works: list[SessionReplyWorkItem] = []
        for work in candidates:
            for task in tasks:
                if task.id is None or work.uid != task.uid or work.session_id != task.session_id or work.profile_id != task.profile_id:
                    continue

                matched = any(work.id == int(delivery_work_id) and work.dedupe_key == delivery["identity"] for delivery_work_id, delivery in get_terminal_watch_deliveries(task).items())
                if not matched:
                    matched = work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY and work.source_type == SessionReplySourceType.BACKGROUND_TASK and work.source_id == str(task.id)
                if not matched and work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK and task.tool_name == TERMINAL_WATCH_TOOL_NAME:
                    state = work.execution_state if isinstance(work.execution_state, dict) else {}
                    matched = any(
                        isinstance(slot, dict)
                        and (
                            (isinstance(slot.get("task_id"), int) and not isinstance(slot.get("task_id"), bool) and slot["task_id"] > 0 and slot["task_id"] == task.id)
                            or (
                                state_key == "external_resume_context"
                                and any(
                                    isinstance(nested_slot, dict) and isinstance(nested_slot.get("task_id"), int) and not isinstance(nested_slot.get("task_id"), bool) and nested_slot["task_id"] > 0 and nested_slot["task_id"] == task.id
                                    for nested_slot in (slot.get("tasks") if isinstance(slot.get("tasks"), list) else [])
                                )
                            )
                        )
                        for state_key in ("pending_tools", "terminal_watch_waits", "external_resume_context")
                        for slot in (state.get(state_key) if isinstance(state.get(state_key), list) else [])
                    )
                if matched:
                    matching_works.append(work)
                    break

        return tasks, matching_works

    async def list_result_source_works(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
    ) -> list[SessionReplyWorkItem]:
        result = await db.execute(
            select(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.uid == work.uid,
                SessionReplyWorkItem.session_id == work.session_id,
                SessionReplyWorkItem.profile_id == work.profile_id,
                SessionReplyWorkItem.work_type.in_(
                    [
                        SessionReplyWorkType.FOREGROUND_REPLY,
                        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
                        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
                    ]
                ),
                SessionReplyWorkItem.status.in_(
                    [
                        SessionReplyWorkStatus.READY_FOR_LLM,
                        SessionReplyWorkStatus.RUNNING,
                        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                    ]
                ),
            )
            .order_by(SessionReplyWorkItem.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def list_completed_reply_tasks(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        task_ids: list[int],
    ) -> list[BackgroundTask]:
        result = await db.execute(
            select(BackgroundTask)
            .where(
                BackgroundTask.uid == work.uid,
                BackgroundTask.session_id == work.session_id,
                BackgroundTask.profile_id == work.profile_id,
                BackgroundTask.status.in_(
                    [
                        BackgroundTaskStatus.SUCCEEDED,
                        BackgroundTaskStatus.FAILED,
                        BackgroundTaskStatus.CANCELLED,
                    ]
                ),
                or_(
                    BackgroundTask.id.in_(task_ids),
                    and_(
                        BackgroundTask.auto_reply.is_(True),
                        BackgroundTask.reply_status.in_(
                            [
                                BackgroundTaskReplyStatus.PENDING,
                                BackgroundTaskReplyStatus.RUNNING,
                            ]
                        ),
                    ),
                    BackgroundTask.extra["reply_owner"]["status"].as_string() == "received",
                ),
            )
            .order_by(BackgroundTask.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def finish_received_results(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        commit: bool = False,
    ) -> None:
        execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
        received_task_ids = {task_id for task_id in execution_state.get("received_external_task_ids", []) if isinstance(task_id, int) and not isinstance(task_id, bool) and task_id > 0}
        if not received_task_ids:
            return

        result = await db.execute(
            select(BackgroundTask)
            .where(
                BackgroundTask.id.in_(received_task_ids),
                BackgroundTask.uid == work.uid,
                BackgroundTask.session_id == work.session_id,
                BackgroundTask.profile_id == work.profile_id,
            )
            .order_by(BackgroundTask.id.asc())
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        tasks = list(result.scalars().all())
        changed = False
        for task in tasks:
            extra = task.extra if isinstance(task.extra, dict) else {}
            reply_owner = extra.get("reply_owner")
            if not isinstance(reply_owner, dict) or (reply_owner.get("work_id") != work.id or reply_owner.get("identity") != work.dedupe_key or reply_owner.get("sequence_no") != work.sequence_no or reply_owner.get("status") != "received"):
                continue

            updated_extra = dict(extra)
            updated_reply_owner = dict(reply_owner)
            updated_reply_owner["status"] = "handled"
            updated_extra["reply_owner"] = updated_reply_owner
            task.extra = updated_extra
            if task.auto_reply is True:
                task.reply_status = BackgroundTaskReplyStatus.SUCCEEDED
                task.reply_locked_by = None
                task.reply_lock_until = None
            db.add(task)
            changed = True

        if changed:
            await db.flush()
        if commit:
            await db.commit()

    async def update_active_reply(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        values: dict[str, Any],
    ) -> bool:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work.id,
                SessionReplyWorkItem.uid == work.uid,
                SessionReplyWorkItem.session_id == work.session_id,
                SessionReplyWorkItem.profile_id == work.profile_id,
                SessionReplyWorkItem.dedupe_key == work.dedupe_key,
                SessionReplyWorkItem.status.in_(SESSION_REPLY_ACTIVE_STATUSES),
            )
            .values(**{**values, "updated_at": get_local_time()})
            .execution_options(synchronize_session=False)
        )
        return (result.rowcount or 0) == 1

    async def mark_stopped_results_delivered(self, db: AsyncSession, *, uid: str, task_ids: list[int]) -> None:
        if not task_ids:
            return

        await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id.in_(task_ids),
                BackgroundTask.uid == uid,
                BackgroundTask.auto_reply.is_(True),
                BackgroundTask.status.in_([BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED]),
                BackgroundTask.reply_status.in_([BackgroundTaskReplyStatus.PENDING, BackgroundTaskReplyStatus.RUNNING]),
            )
            .values(
                reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
                reply_locked_by=None,
                reply_lock_until=None,
            )
            .execution_options(synchronize_session=False)
        )

    async def suspend_claimed(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        worker_id: str,
        pending_tools: list[dict],
        commit: bool = True,
    ) -> bool:
        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        state.pop("external_results", None)
        state.pop("completed_task_ids", None)
        state.pop("external_resume_context", None)
        existing_message_ids = state.get("external_tool_result_message_ids")
        message_ids = list(existing_message_ids) if isinstance(existing_message_ids, list) else []
        message_ids.extend(pending_tool.get("tool_result_message_id") for pending_tool in pending_tools if isinstance(pending_tool, dict))
        external_tool_result_message_ids: list[int] = []
        seen_message_ids: set[int] = set()
        for message_id in message_ids:
            if isinstance(message_id, int) and not isinstance(message_id, bool) and message_id > 0 and message_id not in seen_message_ids:
                seen_message_ids.add(message_id)
                external_tool_result_message_ids.append(message_id)
        state["external_tool_result_message_ids"] = external_tool_result_message_ids

        terminal_watch_slots: list[dict] = []
        current_pending_tools: list[dict] = []
        for slot in pending_tools:
            if slot.get("terminal_watch") is True or slot.get("tool_name") == TERMINAL_WATCH_TOOL_NAME:
                terminal_watch_slots.append(slot)
            else:
                current_pending_tools.append(slot)

        pending_tools_by_task_id: dict[Any, dict] = {}
        previous_pending_tools = state.get("pending_tools")
        if isinstance(previous_pending_tools, list):
            for slot in previous_pending_tools:
                if not isinstance(slot, dict):
                    continue
                if slot.get("terminal_watch") is True or slot.get("tool_name") == TERMINAL_WATCH_TOOL_NAME:
                    continue
                pending_tools_by_task_id[slot.get("task_id")] = slot
        for slot in current_pending_tools:
            pending_tools_by_task_id[slot.get("task_id")] = slot
        pending_tool_waits = list(pending_tools_by_task_id.values())

        terminal_watch_waits_by_key: dict[tuple[Any, Any], dict] = {}
        previous_terminal_watch_waits = state.get("terminal_watch_waits")
        if isinstance(previous_terminal_watch_waits, list):
            for slot in previous_terminal_watch_waits:
                if isinstance(slot, dict):
                    key = (slot.get("task_id"), slot.get("tool_call_id"))
                    terminal_watch_waits_by_key[key] = slot

        current_terminal_session_ids = {slot.get("terminal_session_id") for slot in terminal_watch_slots if slot.get("terminal_session_id") is not None}
        if current_terminal_session_ids:
            terminal_watch_waits_by_key = {key: slot for key, slot in terminal_watch_waits_by_key.items() if not (slot.get("terminal_auto_watch") is True and slot.get("terminal_session_id") in current_terminal_session_ids)}
        for slot in terminal_watch_slots:
            key = (slot.get("task_id"), slot.get("tool_call_id"))
            terminal_watch_waits_by_key[key] = slot
        terminal_watch_waits = list(terminal_watch_waits_by_key.values())

        state["terminal_watch_waits"] = terminal_watch_waits
        state["pending_tools"] = pending_tool_waits
        state["external_resume_pending"] = False
        state.pop("resume_trigger_message_id", None)

        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={
                "status": SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                "locked_by": None,
                "lock_until": None,
                "execution_state": state,
            },
            commit=False,
        )
        if not updated:
            await db.rollback()
            return False

        latest_sequence = await session_reply_stream_event_crud.get_latest_sequence(db, work_id=work.id)
        await session_reply_stream_event_crud.publish(
            db,
            work_id=work.id,
            sequence_no=latest_sequence + 1,
            event={
                "type": "work_suspended",
                "work_id": work.id,
                "session_id": work.session_id,
                "work_sequence_no": work.sequence_no,
                "event_sequence_no": latest_sequence + 1,
                "request_ids": _request_ids(state),
            },
            commit=False,
        )
        if commit:
            await db.commit()
        logger.bind(
            work_id=work.id,
            session_id=work.session_id,
            terminal_watch_count=len(terminal_watch_waits),
            pending_tool_count=len(pending_tool_waits),
        ).info("Session reply suspended for external work")
        return True

    async def lock_waiting(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        uid: str,
        session_id: str,
        identity: str,
    ) -> SessionReplyWorkItem | None:
        statement = (
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.uid == uid,
                SessionReplyWorkItem.session_id == session_id,
                SessionReplyWorkItem.dedupe_key == identity,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
            )
            .values(updated_at=get_local_time())
            .returning(SessionReplyWorkItem)
            .execution_options(synchronize_session=False, populate_existing=True)
        )
        result = await db.execute(statement)
        return result.scalars().first()

    async def update_waiting_state(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        execution_state: dict,
        commit: bool = False,
    ) -> bool:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
            )
            .values(execution_state=execution_state, updated_at=get_local_time())
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        return (result.rowcount or 0) == 1

    async def reopen_completed_waiting(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
    ) -> bool:
        if work.status != SessionReplyWorkStatus.SUCCEEDED:
            return False

        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        state.pop("response", None)
        state.pop("external_resume_context", None)
        state.pop("external_resume_pending", None)
        state.pop("terminal_auto_resume", None)
        state.pop("received_external_task_ids", None)

        checkpoint = state.get("dispatcher_checkpoint")
        checkpoint = dict(checkpoint) if isinstance(checkpoint, dict) else {}
        checkpoint.pop("files_to_user", None)
        checkpoint.pop("reply_skipped", None)
        checkpoint.pop("deferred_external_results", None)
        checkpoint.pop("pending_resume_message_ids", None)
        checkpoint["messages"] = []
        checkpoint["turn_messages"] = []
        checkpoint["reload_history"] = True
        checkpoint["execution_phase"] = GOAL_EXECUTION_PHASE_RUNNING
        checkpoint["current_turn"] = 0
        state["dispatcher_checkpoint"] = checkpoint

        generation = state.get("terminal_resume_generation")
        state["terminal_resume_generation"] = generation + 1 if isinstance(generation, int) and not isinstance(generation, bool) else 1

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
                status=SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                result_message_id=None,
                event_sent=False,
                locked_by=None,
                lock_until=None,
                error=None,
                attempt_count=0,
                execution_state=state,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        updated = (result.rowcount or 0) == 1
        await db.flush()
        return updated

    async def wake_waiting(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        execution_state: dict,
        commit: bool = False,
    ) -> bool:
        sequence_no = await session_reply_work_item_crud.allocate_sequence_no(db, work.session_id)
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work.id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
            )
            .values(
                sequence_no=sequence_no,
                status=SessionReplyWorkStatus.READY_FOR_LLM,
                locked_by=None,
                lock_until=None,
                available_at=await get_database_timestamp(db),
                attempt_count=0,
                error=None,
                execution_state=execution_state,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        return (result.rowcount or 0) == 1

    async def interrupt_waiting(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        execution_state: dict,
        error: str,
        commit: bool = False,
    ) -> bool:
        sequence_no = await session_reply_work_item_crud.allocate_sequence_no(db, work.session_id)
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work.id,
                SessionReplyWorkItem.uid == work.uid,
                SessionReplyWorkItem.session_id == work.session_id,
                SessionReplyWorkItem.dedupe_key == work.dedupe_key,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
            )
            .values(
                status=SessionReplyWorkStatus.CANCELLED,
                sequence_no=sequence_no,
                execution_state=execution_state,
                error=error,
                locked_by=None,
                lock_until=None,
                updated_at=get_local_time(),
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        return (result.rowcount or 0) == 1

    async def list_waiting(
        self,
        db: AsyncSession,
        limit: int = 100,
        *,
        after_id: int = 0,
    ) -> list[SessionReplyWorkItem]:
        result = await db.execute(
            select(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                SessionReplyWorkItem.id > after_id,
            )
            .order_by(SessionReplyWorkItem.id.asc())
            .limit(limit)
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def cancel_all_waiting(
        self,
        db: AsyncSession,
        *,
        error: str,
        before: datetime | None = None,
        commit: bool = True,
    ) -> int:
        max_id_statement = select(func.max(SessionReplyWorkItem.id)).where(SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK)
        if before is not None:
            max_id_statement = max_id_statement.where(SessionReplyWorkItem.created_at <= before)
        result = await db.execute(max_id_statement)
        max_id = result.scalar()
        if max_id is None:
            if commit:
                await db.commit()
            return 0

        cancelled = 0
        after_id = 0
        while True:
            waiting_statement = (
                select(SessionReplyWorkItem)
                .where(
                    SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
                    SessionReplyWorkItem.id > after_id,
                    SessionReplyWorkItem.id <= max_id,
                )
                .order_by(SessionReplyWorkItem.id.asc())
                .limit(100)
                .execution_options(populate_existing=True)
            )
            if before is not None:
                waiting_statement = waiting_statement.where(SessionReplyWorkItem.created_at <= before)
            result = await db.execute(waiting_statement)
            waiting = list(result.scalars().all())
            if not waiting:
                break
            for work in waiting:
                after_id = work.id or after_id
                if await self.interrupt_waiting(
                    db,
                    work=work,
                    execution_state=dict(work.execution_state) if isinstance(work.execution_state, dict) else {},
                    error=error,
                    commit=False,
                ):
                    cancelled += 1
        if commit:
            await db.commit()
        return cancelled


external_work_crud = CRUDExternalWork()
