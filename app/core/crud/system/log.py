from datetime import timedelta
from typing import Any

from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import desc, select

from app.core.crud.base import CRUDBase
from app.core.crud.system.setting import system_setting_crud
from app.core.log.redaction import redact_log_text
from app.core.utils.time import get_local_time
from app.models.system_log import SystemLog, SystemLogCreate


class CRUDSystemLog(CRUDBase[SystemLog, SystemLogCreate, SystemLogCreate]):
    async def create(
        self,
        db: AsyncSession,
        *,
        obj_in: SystemLogCreate | dict[str, Any],
        update_dict: dict[str, Any] = None,
        commit: bool = True,
    ) -> SystemLog:
        data = dict(obj_in) if isinstance(obj_in, dict) else obj_in.model_dump()
        if update_dict:
            data.update(update_dict)

        if await system_setting_crud.get_log_redaction_enabled(db):
            if isinstance(data.get("message"), str):
                data["message"] = redact_log_text(data["message"])
            if isinstance(data.get("extra"), str):
                data["extra"] = redact_log_text(data["extra"])

        return await super().create(db, obj_in=data, commit=commit)

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

    async def get_latest_id(self, db: AsyncSession) -> int:
        result = await db.execute(select(func.max(SystemLog.id)))
        return result.scalar() or 0

    async def get_recent_through_id(self, db: AsyncSession, *, through_id: int, limit: int = 100) -> list[SystemLog]:
        result = await db.execute(select(SystemLog).where(SystemLog.id <= through_id).order_by(SystemLog.id.desc()).limit(limit))
        return list(result.scalars().all())

    async def list_after_id(self, db: AsyncSession, *, after_id: int, limit: int = 100) -> list[SystemLog]:
        result = await db.execute(select(SystemLog).where(SystemLog.id > after_id).order_by(SystemLog.id.asc()).limit(limit))
        return list(result.scalars().all())

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
