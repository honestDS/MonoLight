from datetime import timedelta

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.core.utils.time import get_local_time
from app.models.scheduled_task import ScheduledTask, ScheduledTaskCreate, ScheduledTaskStatus


class CRUDScheduledTask(CRUDBase[ScheduledTask, ScheduledTaskCreate, ScheduledTaskCreate]):
    async def create_scheduled_task(
        self,
        db: AsyncSession,
        *,
        name: str,
        uid: str,
        session_id: str,
        message: str,
        interval_seconds: int,
    ) -> ScheduledTask:
        now = get_local_time()
        scheduled_task = ScheduledTask(
            name=name,
            uid=uid,
            session_id=session_id,
            message=message,
            interval_seconds=interval_seconds,
            next_run_at=now + timedelta(seconds=interval_seconds),
        )
        db.add(scheduled_task)
        await db.commit()
        await db.refresh(scheduled_task)
        return scheduled_task

    async def list_tasks(self, db: AsyncSession, *, skip: int = 0, limit: int = 100, status: ScheduledTaskStatus | None = None) -> list[ScheduledTask]:
        stmt = select(ScheduledTask).order_by(ScheduledTask.created_at.desc()).offset(skip).limit(limit)
        if status is not None:
            stmt = stmt.where(ScheduledTask.status == status)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def count_tasks(self, db: AsyncSession, *, status: ScheduledTaskStatus | None = None) -> int:
        stmt = select(func.count()).select_from(ScheduledTask)
        if status is not None:
            stmt = stmt.where(ScheduledTask.status == status)
        result = await db.execute(stmt)
        return result.scalar() or 0

    async def list_due(self, db: AsyncSession, *, limit: int = 100) -> list[ScheduledTask]:
        now = get_local_time()
        stmt = (
            select(ScheduledTask)
            .where(ScheduledTask.status == ScheduledTaskStatus.ENABLED)
            .where(ScheduledTask.next_run_at <= now)
            .order_by(ScheduledTask.next_run_at.asc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def update_scheduled_task(self, db: AsyncSession, *, scheduled_task: ScheduledTask, obj_in: dict) -> ScheduledTask:
        for field, value in obj_in.items():
            setattr(scheduled_task, field, value)
        scheduled_task.updated_at = get_local_time()
        db.add(scheduled_task)
        await db.commit()
        await db.refresh(scheduled_task)
        return scheduled_task

    async def mark_dispatched(self, db: AsyncSession, *, scheduled_task: ScheduledTask, message_id: int) -> ScheduledTask:
        now = get_local_time()
        scheduled_task.last_run_at = now
        scheduled_task.last_message_id = message_id
        scheduled_task.run_count += 1
        scheduled_task.next_run_at = now + timedelta(seconds=scheduled_task.interval_seconds)
        scheduled_task.updated_at = now
        db.add(scheduled_task)
        await db.commit()
        await db.refresh(scheduled_task)
        return scheduled_task

    async def mark_skipped(self, db: AsyncSession, *, scheduled_task: ScheduledTask) -> ScheduledTask:
        now = get_local_time()
        scheduled_task.next_run_at = now + timedelta(seconds=scheduled_task.interval_seconds)
        scheduled_task.updated_at = now
        db.add(scheduled_task)
        await db.commit()
        await db.refresh(scheduled_task)
        return scheduled_task

    async def delete_task(self, db: AsyncSession, *, scheduled_task: ScheduledTask) -> None:
        await db.delete(scheduled_task)
        await db.commit()


scheduled_task_crud = CRUDScheduledTask(ScheduledTask)
