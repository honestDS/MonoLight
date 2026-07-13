from datetime import timedelta
from typing import Any

from sqlalchemy import delete, or_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.core.utils.time import get_local_time
from app.models.message_platform_outbox import MessagePlatformOutbox, MessagePlatformOutboxStatus

OUTBOX_MAX_ATTEMPTS = 5
OUTBOX_LEASE_SECONDS = 330
OUTBOX_RETRY_BASE_SECONDS = 5
OUTBOX_RETRY_MAX_SECONDS = 300
OUTBOX_SENT_RETENTION_DAYS = 7
OUTBOX_FAILED_RETENTION_DAYS = 30


def calculate_retry_delay_seconds(attempt_count: int) -> int:
    exponent = max(0, attempt_count - 1)
    return min(OUTBOX_RETRY_MAX_SECONDS, OUTBOX_RETRY_BASE_SECONDS * (2**exponent))


class CRUDMessagePlatformOutbox(CRUDBase[MessagePlatformOutbox, MessagePlatformOutbox, MessagePlatformOutbox]):
    async def enqueue(
        self,
        db: AsyncSession,
        *,
        dedupe_key: str,
        uid: str,
        session_id: str,
        source: str,
        event: dict[str, Any],
    ) -> tuple[MessagePlatformOutbox, bool]:
        item = MessagePlatformOutbox(
            dedupe_key=dedupe_key,
            uid=uid,
            session_id=session_id,
            source=source,
            event=event,
        )
        db.add(item)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            existing = await self.get_by_dedupe_key(db, dedupe_key)
            if existing is None:
                raise
            return existing, False
        await db.refresh(item)
        return item, True

    async def get_by_dedupe_key(self, db: AsyncSession, dedupe_key: str) -> MessagePlatformOutbox | None:
        result = await db.execute(select(MessagePlatformOutbox).where(MessagePlatformOutbox.dedupe_key == dedupe_key))
        return result.scalars().first()

    async def list_claimable_ids(self, db: AsyncSession, *, limit: int = 20) -> list[int]:
        now = get_local_time()
        result = await db.execute(
            select(MessagePlatformOutbox.id)
            .where(
                or_(
                    (MessagePlatformOutbox.status == MessagePlatformOutboxStatus.PENDING) & (MessagePlatformOutbox.next_attempt_at <= now),
                    (MessagePlatformOutbox.status == MessagePlatformOutboxStatus.PROCESSING) & (MessagePlatformOutbox.lock_until < now),
                )
            )
            .order_by(MessagePlatformOutbox.created_at.asc())
            .limit(limit)
        )
        return [item_id for item_id in result.scalars().all() if item_id is not None]

    async def try_claim(
        self,
        db: AsyncSession,
        *,
        item_id: int,
        worker_id: str,
        lease_seconds: int = OUTBOX_LEASE_SECONDS,
    ) -> MessagePlatformOutbox | None:
        now = get_local_time()
        claimable = or_(
            (MessagePlatformOutbox.status == MessagePlatformOutboxStatus.PENDING) & (MessagePlatformOutbox.next_attempt_at <= now),
            (MessagePlatformOutbox.status == MessagePlatformOutboxStatus.PROCESSING) & (MessagePlatformOutbox.lock_until < now),
        )
        result = await db.execute(
            update(MessagePlatformOutbox)
            .where(MessagePlatformOutbox.id == item_id, claimable)
            .values(
                status=MessagePlatformOutboxStatus.PROCESSING,
                locked_by=worker_id,
                lock_until=now + timedelta(seconds=lease_seconds),
                attempt_count=MessagePlatformOutbox.attempt_count + 1,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if result.rowcount != 1:
            return None
        return await self.get(db, item_id)

    async def mark_sent(self, db: AsyncSession, *, item_id: int, worker_id: str) -> bool:
        now = get_local_time()
        result = await db.execute(
            update(MessagePlatformOutbox)
            .where(
                MessagePlatformOutbox.id == item_id,
                MessagePlatformOutbox.status == MessagePlatformOutboxStatus.PROCESSING,
                MessagePlatformOutbox.locked_by == worker_id,
            )
            .values(
                status=MessagePlatformOutboxStatus.SENT,
                sent_at=now,
                locked_by=None,
                lock_until=None,
                last_error=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def delete_by_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str | None = None,
        is_admin: bool = False,
        commit: bool = True,
    ) -> int:
        conditions = [MessagePlatformOutbox.session_id == session_id]
        if not is_admin:
            conditions.append(MessagePlatformOutbox.uid == uid)
        result = await db.execute(delete(MessagePlatformOutbox).where(*conditions).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        return result.rowcount or 0

    async def cleanup_terminal_items(
        self,
        db: AsyncSession,
        *,
        sent_retention_days: int = OUTBOX_SENT_RETENTION_DAYS,
        failed_retention_days: int = OUTBOX_FAILED_RETENTION_DAYS,
    ) -> int:
        now = get_local_time()
        result = await db.execute(
            delete(MessagePlatformOutbox)
            .where(
                or_(
                    (MessagePlatformOutbox.status == MessagePlatformOutboxStatus.SENT) & (MessagePlatformOutbox.sent_at < now - timedelta(days=sent_retention_days)),
                    (MessagePlatformOutbox.status == MessagePlatformOutboxStatus.FAILED) & (MessagePlatformOutbox.created_at < now - timedelta(days=failed_retention_days)),
                )
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount or 0

    async def mark_retry_or_failed(
        self,
        db: AsyncSession,
        *,
        item_id: int,
        worker_id: str,
        attempt_count: int,
        error: str,
        max_attempts: int = OUTBOX_MAX_ATTEMPTS,
    ) -> MessagePlatformOutboxStatus | None:
        now = get_local_time()
        failed = attempt_count >= max_attempts
        next_status = MessagePlatformOutboxStatus.FAILED if failed else MessagePlatformOutboxStatus.PENDING
        values: dict[str, Any] = {
            "status": next_status,
            "locked_by": None,
            "lock_until": None,
            "last_error": error[:1000],
        }
        if not failed:
            values["next_attempt_at"] = now + timedelta(seconds=calculate_retry_delay_seconds(attempt_count))
        result = await db.execute(
            update(MessagePlatformOutbox)
            .where(
                MessagePlatformOutbox.id == item_id,
                MessagePlatformOutbox.status == MessagePlatformOutboxStatus.PROCESSING,
                MessagePlatformOutbox.locked_by == worker_id,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return next_status if result.rowcount == 1 else None


message_platform_outbox_crud = CRUDMessagePlatformOutbox(MessagePlatformOutbox)
