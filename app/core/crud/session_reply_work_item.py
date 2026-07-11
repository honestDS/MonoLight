import time
from typing import Any

from sqlalchemy import and_, exists, or_, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.utils.time import get_local_time
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SESSION_REPLY_TERMINAL_STATUSES,
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)

_TERMINAL_STATUS_VALUES = [status.value for status in SESSION_REPLY_TERMINAL_STATUSES]


class CRUDSessionReplyWorkItem:
    async def get(self, db: AsyncSession, work_id: int) -> SessionReplyWorkItem | None:
        result = await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id == work_id))
        return result.scalars().first()

    async def get_by_dedupe_key(self, db: AsyncSession, dedupe_key: str) -> SessionReplyWorkItem | None:
        result = await db.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.dedupe_key == dedupe_key))
        return result.scalars().first()

    async def allocate_sequence_no(self, db: AsyncSession, session_id: str) -> int:
        now = get_local_time()
        statement = (
            sqlite_insert(SessionReplySequence)
            .values(session_id=session_id, next_sequence_no=2, updated_at=now)
            .on_conflict_do_update(
                index_elements=[SessionReplySequence.session_id],
                set_={
                    "next_sequence_no": SessionReplySequence.next_sequence_no + 1,
                    "updated_at": now,
                },
            )
            .returning(SessionReplySequence.next_sequence_no)
        )
        result = await db.execute(statement)
        return int(result.scalar_one()) - 1

    async def enqueue(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        work_type: SessionReplyWorkType,
        source_type: SessionReplySourceType,
        source_id: str | int,
        dedupe_key: str,
        commit: bool = True,
    ) -> tuple[SessionReplyWorkItem, bool]:
        existing = await self.get_by_dedupe_key(db, dedupe_key)
        if existing is not None:
            return existing, False

        sequence_no = await self.allocate_sequence_no(db, session_id)
        now = get_local_time()
        statement = (
            sqlite_insert(SessionReplyWorkItem)
            .values(
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                sequence_no=sequence_no,
                work_type=work_type,
                source_type=source_type,
                source_id=str(source_id),
                dedupe_key=dedupe_key,
                status=SessionReplyWorkStatus.READY_FOR_LLM,
                execution_state={},
                event_sent=False,
                attempt_count=0,
                max_attempts=5,
                available_at=int(time.time()),
                created_at=now,
                updated_at=now,
            )
            .on_conflict_do_nothing(index_elements=[SessionReplyWorkItem.dedupe_key])
            .returning(SessionReplyWorkItem)
        )
        result = await db.execute(statement)
        work = result.scalars().first()
        if work is None:
            work = await self.get_by_dedupe_key(db, dedupe_key)
            if work is None:
                raise RuntimeError("Session reply work deduplication failed")
            if commit:
                await db.commit()
            return work, False
        if commit:
            await db.commit()
        return work, True

    async def claim_next(
        self,
        db: AsyncSession,
        *,
        worker_id: str,
        lease_seconds: int,
    ) -> SessionReplyWorkItem | None:
        now = int(time.time())
        earlier_work = SessionReplyWorkItem.__table__.alias("earlier_session_reply_work")
        active_running = SessionReplyWorkItem.__table__.alias("active_running_session_reply_work")
        candidate = (
            select(SessionReplyWorkItem.id)
            .where(
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
                SessionReplyWorkItem.available_at <= now,
                ~exists(
                    select(1).where(
                        earlier_work.c.session_id == SessionReplyWorkItem.session_id,
                        earlier_work.c.sequence_no < SessionReplyWorkItem.sequence_no,
                        earlier_work.c.status.not_in(_TERMINAL_STATUS_VALUES),
                    )
                ),
                ~exists(
                    select(1).where(
                        active_running.c.session_id == SessionReplyWorkItem.session_id,
                        active_running.c.status == SessionReplyWorkStatus.RUNNING.value,
                        active_running.c.lock_until > now,
                    )
                ),
            )
            .order_by(SessionReplyWorkItem.created_at, SessionReplyWorkItem.id)
            .limit(1)
            .scalar_subquery()
        )
        statement = (
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == candidate,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
            )
            .values(
                status=SessionReplyWorkStatus.RUNNING,
                locked_by=worker_id,
                lock_until=now + lease_seconds,
                attempt_count=SessionReplyWorkItem.attempt_count + 1,
                updated_at=get_local_time(),
            )
            .returning(SessionReplyWorkItem)
        )
        result = await db.execute(statement)
        claimed = result.scalars().first()
        await db.commit()
        return claimed

    async def renew_lease(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                SessionReplyWorkItem.locked_by == worker_id,
            )
            .values(lock_until=int(time.time()) + lease_seconds, updated_at=get_local_time())
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def release_for_retry(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
        error: str,
        delay_seconds: int,
    ) -> bool:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                SessionReplyWorkItem.locked_by == worker_id,
            )
            .values(
                status=SessionReplyWorkStatus.READY_FOR_LLM,
                locked_by=None,
                lock_until=None,
                available_at=int(time.time()) + delay_seconds,
                error=error,
                updated_at=get_local_time(),
            )
        )
        await db.commit()
        return (result.rowcount or 0) == 1

    async def cancel_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str | None = None,
        is_admin: bool = False,
        commit: bool = True,
    ) -> int:
        conditions = [
            SessionReplyWorkItem.session_id == session_id,
            SessionReplyWorkItem.status.not_in(_TERMINAL_STATUS_VALUES),
        ]
        if not is_admin:
            conditions.append(SessionReplyWorkItem.uid == uid)
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(*conditions)
            .values(
                status=SessionReplyWorkStatus.CANCELLED,
                locked_by=None,
                lock_until=None,
                updated_at=get_local_time(),
            )
        )
        if commit:
            await db.commit()
        return result.rowcount or 0

    async def recover_expired(self, db: AsyncSession) -> int:
        now = int(time.time())
        expired_conditions = [
            SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
            or_(SessionReplyWorkItem.lock_until.is_(None), SessionReplyWorkItem.lock_until <= now),
        ]
        streamed_work = exists(
            select(1).where(
                SessionReplyStreamEvent.work_id == SessionReplyWorkItem.id,
            )
        )
        failed_result = await db.execute(
            update(SessionReplyWorkItem)
            .where(*expired_conditions, streamed_work)
            .values(
                status=SessionReplyWorkStatus.FAILED,
                locked_by=None,
                lock_until=None,
                error="Stream interrupted after partial response",
                updated_at=get_local_time(),
            )
        )
        retry_result = await db.execute(
            update(SessionReplyWorkItem)
            .where(*expired_conditions, ~streamed_work)
            .values(
                status=SessionReplyWorkStatus.READY_FOR_LLM,
                locked_by=None,
                lock_until=None,
                available_at=now,
                updated_at=get_local_time(),
            )
        )
        await db.commit()
        return (failed_result.rowcount or 0) + (retry_result.rowcount or 0)

    async def update_claimed(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
        values: dict[str, Any],
        commit: bool = True,
    ) -> bool:
        result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                SessionReplyWorkItem.locked_by == worker_id,
            )
            .values(**values, updated_at=get_local_time())
        )
        if commit:
            await db.commit()
        return (result.rowcount or 0) == 1

    async def mark_terminal(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
        status: SessionReplyWorkStatus,
        result_message_id: int | None = None,
        error: str | None = None,
        event_sent: bool | None = None,
        commit: bool = True,
    ) -> bool:
        values: dict[str, Any] = {
            "status": status,
            "result_message_id": result_message_id,
            "error": error,
            "locked_by": None,
            "lock_until": None,
        }
        if event_sent is not None:
            values["event_sent"] = event_sent
        return await self.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values=values,
            commit=commit,
        )

    async def resolve_merged_target(self, db: AsyncSession, work_id: int) -> SessionReplyWorkItem | None:
        current = await self.get(db, work_id)
        visited: set[int] = set()
        while current and current.status == SessionReplyWorkStatus.MERGED and current.merged_into_id:
            if current.id is None or current.id in visited:
                return None
            visited.add(current.id)
            current = await self.get(db, current.merged_into_id)
        return current

    async def list_contiguous_foreground(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
    ) -> list[SessionReplyWorkItem]:
        blocking_sequence_result = await db.execute(
            select(SessionReplyWorkItem.sequence_no)
            .where(
                SessionReplyWorkItem.session_id == work.session_id,
                SessionReplyWorkItem.sequence_no > work.sequence_no,
                SessionReplyWorkItem.status.not_in(_TERMINAL_STATUS_VALUES),
                SessionReplyWorkItem.work_type != SessionReplyWorkType.FOREGROUND_REPLY,
            )
            .order_by(SessionReplyWorkItem.sequence_no)
            .limit(1)
        )
        blocking_sequence = blocking_sequence_result.scalar()
        conditions = [
            SessionReplyWorkItem.session_id == work.session_id,
            SessionReplyWorkItem.sequence_no >= work.sequence_no,
            SessionReplyWorkItem.work_type == SessionReplyWorkType.FOREGROUND_REPLY,
            SessionReplyWorkItem.status.in_(
                [
                    SessionReplyWorkStatus.READY_FOR_LLM,
                    SessionReplyWorkStatus.RUNNING,
                ]
            ),
        ]
        if blocking_sequence is not None:
            conditions.append(SessionReplyWorkItem.sequence_no < blocking_sequence)
        result = await db.execute(select(SessionReplyWorkItem).where(and_(*conditions)).order_by(SessionReplyWorkItem.sequence_no))
        return list(result.scalars().all())


session_reply_work_item_crud = CRUDSessionReplyWorkItem()
