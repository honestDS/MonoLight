from datetime import timedelta
from typing import Any

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import func, select

from app.core.crud.base import CRUDBase
from app.core.utils.time import get_local_time
from app.models.session_event import SessionEvent

SESSION_EVENT_RETENTION_HOURS = 24


class CRUDSessionEvent(CRUDBase[SessionEvent, SessionEvent, SessionEvent]):
    async def publish(self, db: AsyncSession, *, uid: str, session_id: str, event: dict[str, Any]) -> SessionEvent:
        item = SessionEvent(uid=uid, session_id=session_id, event=event)
        db.add(item)
        await db.commit()
        await db.refresh(item)
        return item

    async def get_latest_id(self, db: AsyncSession) -> int:
        result = await db.execute(select(func.max(SessionEvent.id)))
        return int(result.scalar() or 0)

    async def list_after_id(self, db: AsyncSession, *, after_id: int, limit: int) -> list[SessionEvent]:
        result = await db.execute(select(SessionEvent).where(SessionEvent.id > after_id).order_by(SessionEvent.id).limit(limit))
        return list(result.scalars().all())

    async def cleanup_expired(self, db: AsyncSession, *, retention_hours: int = SESSION_EVENT_RETENTION_HOURS) -> int:
        cutoff = get_local_time() - timedelta(hours=retention_hours)
        result = await db.execute(delete(SessionEvent).where(SessionEvent.created_at < cutoff).execution_options(synchronize_session=False))
        await db.commit()
        return result.rowcount or 0


session_event_crud = CRUDSessionEvent(SessionEvent)
