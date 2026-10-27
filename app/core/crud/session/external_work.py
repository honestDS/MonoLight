from datetime import datetime
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.log import get_logger
from app.core.utils.time import get_local_time
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
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
