from datetime import timedelta

from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import desc, select

from app.core.crud.base import CRUDBase
from app.core.utils.time import get_local_time
from app.models.system_log import SystemLog, SystemLogCreate


class CRUDSystemLog(CRUDBase[SystemLog, SystemLogCreate, SystemLogCreate]):
    async def get_multi_filtered(self, db: AsyncSession, *, level: str | None = None, uid: str | None = None, session_id: str | None = None, skip: int = 0, limit: int = 100) -> list[SystemLog]:
        stmt = select(SystemLog).order_by(desc(SystemLog.created_at))
        if level:
            stmt = stmt.where(SystemLog.level == level)
        if uid:
            stmt = stmt.where(SystemLog.uid == uid)
        if session_id:
            stmt = stmt.where(SystemLog.session_id == session_id)

        stmt = stmt.offset(skip).limit(limit)
        result = await db.execute(stmt)
        return result.scalars().all()

    async def count_filtered(self, db: AsyncSession, *, level: str | None = None, uid: str | None = None, session_id: str | None = None) -> int:
        stmt = select(func.count()).select_from(SystemLog)
        if level:
            stmt = stmt.where(SystemLog.level == level)
        if uid:
            stmt = stmt.where(SystemLog.uid == uid)
        if session_id:
            stmt = stmt.where(SystemLog.session_id == session_id)

        result = await db.execute(stmt)
        return result.scalar()

    async def clear_expired_logs(self, db: AsyncSession, days: int = 7) -> int:
        cutoff = get_local_time() - timedelta(days=days)
        stmt = delete(SystemLog).where(SystemLog.created_at < cutoff)
        result = await db.execute(stmt)
        return result.rowcount or 0


system_log_crud = CRUDSystemLog(SystemLog)
