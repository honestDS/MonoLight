import time
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, delete, exists, or_, true, update
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_SESSION_REPLY_DEDUPLICATION_FAILED
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.models.session import ChatSession
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
SESSION_REPLY_WORK_RETENTION_HOURS = 24
SESSION_REPLY_CLEANUP_BATCH_SIZE = 500


@dataclass(frozen=True)
class SessionReplyCleanupResult:
    work_items: int = 0
    stream_events: int = 0
    sequences: int = 0

    @property
    def total(self) -> int:
        return self.work_items + self.stream_events + self.sequences


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
                max_attempts=2,
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
                raise RuntimeError(t(ERR_SESSION_REPLY_DEDUPLICATION_FAILED))
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
        scheduled_profile_limits: dict[int, int] | None = None,
    ) -> SessionReplyWorkItem | None:
        now = int(time.time())
        earlier_work = SessionReplyWorkItem.__table__.alias("earlier_session_reply_work")
        active_running = SessionReplyWorkItem.__table__.alias("active_running_session_reply_work")
        scheduled_running = SessionReplyWorkItem.__table__.alias("scheduled_running_session_reply_work")
        scheduled_limits = scheduled_profile_limits or {}
        scheduled_limit_conditions = [
            and_(
                SessionReplyWorkItem.profile_id == profile_id,
                select(scheduled_running.c.id)
                .where(
                    scheduled_running.c.profile_id == profile_id,
                    scheduled_running.c.work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY.value,
                    scheduled_running.c.status == SessionReplyWorkStatus.RUNNING.value,
                    scheduled_running.c.lock_until > now,
                )
                .limit(limit)
                .offset(limit - 1)
                .scalar_subquery()
                .is_(None),
            )
            for profile_id, limit in scheduled_limits.items()
        ]
        scheduled_capacity_available = (
            or_(
                SessionReplyWorkItem.work_type != SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
                *scheduled_limit_conditions,
            )
            if scheduled_limits
            else true()
        )
        candidate = (
            select(SessionReplyWorkItem.id)
            .where(
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
                SessionReplyWorkItem.available_at <= now,
                scheduled_capacity_available,
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

    async def get_active_claims(
        self,
        db: AsyncSession,
        claims: dict[int, str],
    ) -> set[tuple[int, str]]:
        if not claims:
            return set()
        claim_conditions = [
            and_(
                SessionReplyWorkItem.id == work_id,
                SessionReplyWorkItem.locked_by == worker_id,
            )
            for work_id, worker_id in claims.items()
        ]
        result = await db.execute(
            select(SessionReplyWorkItem.id, SessionReplyWorkItem.locked_by).where(
                SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                or_(*claim_conditions),
            )
        )
        return {(work_id, worker_id) for work_id, worker_id in result.all() if worker_id is not None}

    async def renew_active_claims(
        self,
        db: AsyncSession,
        *,
        claims: dict[int, str],
        lease_seconds: int,
    ) -> set[tuple[int, str]]:
        active_claims = await self.get_active_claims(db, claims)
        if active_claims:
            await db.execute(
                update(SessionReplyWorkItem)
                .where(
                    or_(
                        *[
                            and_(
                                SessionReplyWorkItem.id == work_id,
                                SessionReplyWorkItem.locked_by == worker_id,
                            )
                            for work_id, worker_id in active_claims
                        ]
                    ),
                    SessionReplyWorkItem.status == SessionReplyWorkStatus.RUNNING,
                )
                .values(
                    lock_until=int(time.time()) + lease_seconds,
                    updated_at=get_local_time(),
                )
            )
            await db.commit()
        return active_claims

    async def list_ready_scheduled_profile_ids(self, db: AsyncSession) -> set[int]:
        result = await db.execute(
            select(SessionReplyWorkItem.profile_id)
            .where(
                SessionReplyWorkItem.work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
            )
            .distinct()
        )
        return set(result.scalars().all())

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

    async def delete_by_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str | None = None,
        is_admin: bool = False,
        commit: bool = True,
    ) -> int:
        work_conditions = [SessionReplyWorkItem.session_id == session_id]
        if not is_admin:
            work_conditions.append(SessionReplyWorkItem.uid == uid)

        work_ids = select(SessionReplyWorkItem.id).where(*work_conditions)
        await db.execute(delete(SessionReplyStreamEvent).where(SessionReplyStreamEvent.work_id.in_(work_ids)).execution_options(synchronize_session=False))
        result = await db.execute(delete(SessionReplyWorkItem).where(*work_conditions).execution_options(synchronize_session=False))
        await db.execute(delete(SessionReplySequence).where(SessionReplySequence.session_id == session_id).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        return result.rowcount or 0

    async def cleanup_terminal_items(
        self,
        db: AsyncSession,
        *,
        retention_hours: int = SESSION_REPLY_WORK_RETENTION_HOURS,
        batch_size: int = SESSION_REPLY_CLEANUP_BATCH_SIZE,
    ) -> SessionReplyCleanupResult:
        cutoff = get_local_time() - timedelta(hours=retention_hours)
        remaining = max(1, batch_size)
        work_item_count = 0
        stream_event_count = 0
        sequence_count = 0
        expired_work_conditions = [
            SessionReplyWorkItem.status.in_(_TERMINAL_STATUS_VALUES),
            SessionReplyWorkItem.updated_at < cutoff,
        ]

        expired_event_ids = (
            select(SessionReplyStreamEvent.id)
            .where(
                SessionReplyStreamEvent.work_id.in_(
                    select(SessionReplyWorkItem.id).where(*expired_work_conditions),
                )
            )
            .order_by(SessionReplyStreamEvent.id)
            .limit(remaining)
        )
        event_result = await db.execute(delete(SessionReplyStreamEvent).where(SessionReplyStreamEvent.id.in_(expired_event_ids)).execution_options(synchronize_session=False))
        await db.commit()
        deleted = event_result.rowcount or 0
        stream_event_count += deleted
        remaining -= deleted

        if remaining > 0:
            expired_work_ids = (
                select(SessionReplyWorkItem.id)
                .where(
                    *expired_work_conditions,
                    ~exists(
                        select(1).where(
                            SessionReplyStreamEvent.work_id == SessionReplyWorkItem.id,
                        )
                    ),
                )
                .order_by(SessionReplyWorkItem.id)
                .limit(remaining)
            )
            work_result = await db.execute(delete(SessionReplyWorkItem).where(SessionReplyWorkItem.id.in_(expired_work_ids)).execution_options(synchronize_session=False))
            await db.commit()
            deleted = work_result.rowcount or 0
            work_item_count += deleted
            remaining -= deleted

        if remaining > 0:
            orphan_event_ids = (
                select(SessionReplyStreamEvent.id)
                .where(
                    ~exists(
                        select(1).where(
                            SessionReplyWorkItem.id == SessionReplyStreamEvent.work_id,
                        )
                    )
                )
                .order_by(SessionReplyStreamEvent.id)
                .limit(remaining)
            )
            orphan_event_result = await db.execute(delete(SessionReplyStreamEvent).where(SessionReplyStreamEvent.id.in_(orphan_event_ids)).execution_options(synchronize_session=False))
            await db.commit()
            deleted = orphan_event_result.rowcount or 0
            stream_event_count += deleted
            remaining -= deleted

        if remaining > 0:
            orphan_sequence_ids = (
                select(SessionReplySequence.session_id)
                .where(
                    ~exists(
                        select(1).where(
                            ChatSession.session_id == SessionReplySequence.session_id,
                        )
                    ),
                    ~exists(
                        select(1).where(
                            SessionReplyWorkItem.session_id == SessionReplySequence.session_id,
                        )
                    ),
                )
                .order_by(SessionReplySequence.session_id)
                .limit(remaining)
            )
            orphan_sequence_result = await db.execute(delete(SessionReplySequence).where(SessionReplySequence.session_id.in_(orphan_sequence_ids)).execution_options(synchronize_session=False))
            await db.commit()
            sequence_count += orphan_sequence_result.rowcount or 0

        return SessionReplyCleanupResult(
            work_items=work_item_count,
            stream_events=stream_event_count,
            sequences=sequence_count,
        )

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

    async def recover_expired(self, db: AsyncSession) -> tuple[int, list[tuple[int, str, str]]]:
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
        terminal_result = await db.execute(
            select(
                SessionReplyWorkItem.id,
                SessionReplyWorkItem.locked_by,
                streamed_work.label("stream_started"),
            ).where(
                *expired_conditions,
                or_(
                    streamed_work,
                    SessionReplyWorkItem.attempt_count >= SessionReplyWorkItem.max_attempts,
                ),
                SessionReplyWorkItem.locked_by.is_not(None),
            )
        )
        terminal_claims = [
            (
                work_id,
                worker_id,
                "Stream interrupted after partial response" if stream_started else "Maximum retry attempts reached after worker interruption",
            )
            for work_id, worker_id, stream_started in terminal_result.all()
            if worker_id is not None
        ]
        retry_result = await db.execute(
            update(SessionReplyWorkItem)
            .where(
                *expired_conditions,
                ~streamed_work,
                SessionReplyWorkItem.attempt_count < SessionReplyWorkItem.max_attempts,
            )
            .values(
                status=SessionReplyWorkStatus.READY_FOR_LLM,
                locked_by=None,
                lock_until=None,
                available_at=now,
                updated_at=get_local_time(),
            )
        )
        await db.commit()
        return len(terminal_claims) + (retry_result.rowcount or 0), terminal_claims

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
