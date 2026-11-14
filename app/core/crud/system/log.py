from datetime import datetime
from typing import Any

from sqlalchemy import and_, delete, func, or_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement
from sqlmodel import desc, select

from app.core.crud.base import CRUDBase
from app.core.crud.system.setting import system_setting_crud
from app.core.log.redaction import redact_log_text
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
        stmt = select(SystemLog).order_by(desc(SystemLog.created_at), desc(SystemLog.id))
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

    @staticmethod
    def _build_over_limit_condition(
        boundary: tuple[datetime | None, int] | None,
    ) -> ColumnElement[bool] | None:
        if boundary is None:
            return None

        boundary_created_at, boundary_id = boundary
        if boundary_created_at is None:
            return and_(
                SystemLog.created_at.is_(None),
                SystemLog.id <= boundary_id,
            )

        return or_(
            SystemLog.created_at < boundary_created_at,
            and_(
                SystemLog.created_at == boundary_created_at,
                SystemLog.id <= boundary_id,
            ),
            SystemLog.created_at.is_(None),
        )

    async def list_old_log_ids(
        self,
        db: AsyncSession,
        *,
        cutoff: datetime,
        max_entries: int,
        batch_size: int,
        written_before: datetime,
    ) -> list[int]:
        boundary_statement = select(SystemLog.created_at, SystemLog.id).order_by(desc(SystemLog.created_at), desc(SystemLog.id)).offset(max_entries).limit(1)
        boundary_result = await db.execute(boundary_statement)
        boundary = boundary_result.one_or_none()
        over_limit_condition = self._build_over_limit_condition(boundary)
        old_log_conditions: list[ColumnElement[bool]] = [SystemLog.created_at < cutoff]
        if over_limit_condition is not None:
            old_log_conditions.append(over_limit_condition)

        old_enough_for_realtime = or_(
            SystemLog.created_at.is_(None),
            SystemLog.created_at < written_before,
        )
        candidate_statement = select(SystemLog.id).where(or_(*old_log_conditions), old_enough_for_realtime).order_by(SystemLog.created_at.asc(), SystemLog.id.asc()).limit(batch_size)
        candidate_result = await db.execute(candidate_statement)
        return list(candidate_result.scalars().all())

    async def delete_log_ids(self, db: AsyncSession, *, ids: list[int]) -> int:
        if not ids:
            return 0

        delete_statement = delete(SystemLog).where(SystemLog.id.in_(ids)).execution_options(synchronize_session=False)
        result = await db.execute(delete_statement)
        return result.rowcount or 0


system_log_crud = CRUDSystemLog(SystemLog)
