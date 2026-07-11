from typing import Any

from sqlalchemy import func
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.session_reply_stream_event import SessionReplyStreamEvent


class CRUDSessionReplyStreamEvent:
    async def publish(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        sequence_no: int,
        event: dict[str, Any],
        commit: bool = True,
    ) -> tuple[SessionReplyStreamEvent, bool]:
        statement = (
            sqlite_insert(SessionReplyStreamEvent)
            .values(work_id=work_id, sequence_no=sequence_no, event=event)
            .on_conflict_do_nothing(
                index_elements=[
                    SessionReplyStreamEvent.work_id,
                    SessionReplyStreamEvent.sequence_no,
                ]
            )
            .returning(SessionReplyStreamEvent)
        )
        result = await db.execute(statement)
        item = result.scalars().first()
        created = item is not None
        if item is None:
            existing = await db.execute(
                select(SessionReplyStreamEvent).where(
                    SessionReplyStreamEvent.work_id == work_id,
                    SessionReplyStreamEvent.sequence_no == sequence_no,
                )
            )
            item = existing.scalars().one()
        if commit:
            await db.commit()
        return item, created

    async def get_latest_sequence(self, db: AsyncSession, *, work_id: int) -> int:
        result = await db.execute(select(func.max(SessionReplyStreamEvent.sequence_no)).where(SessionReplyStreamEvent.work_id == work_id))
        return int(result.scalar() or 0)

    async def has_events(self, db: AsyncSession, *, work_id: int) -> bool:
        result = await db.execute(select(SessionReplyStreamEvent.id).where(SessionReplyStreamEvent.work_id == work_id).limit(1))
        return result.scalar() is not None

    async def list_after_sequence(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        after_sequence_no: int,
        limit: int = 100,
    ) -> list[SessionReplyStreamEvent]:
        result = await db.execute(
            select(SessionReplyStreamEvent)
            .where(
                SessionReplyStreamEvent.work_id == work_id,
                SessionReplyStreamEvent.sequence_no > after_sequence_no,
            )
            .order_by(SessionReplyStreamEvent.sequence_no)
            .limit(limit)
        )
        return list(result.scalars().all())


session_reply_stream_event_crud = CRUDSessionReplyStreamEvent()
