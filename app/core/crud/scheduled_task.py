from datetime import timedelta

from sqlalchemy import delete, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.core.utils.time import get_local_time
from app.models.scheduled_task import ScheduledTask, ScheduledTaskCreate, ScheduledTaskStatus


class CRUDScheduledTask(CRUDBase[ScheduledTask, ScheduledTaskCreate, ScheduledTaskCreate]):
    async def list_by_profile(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
    ) -> list[ScheduledTask]:
        result = await db.execute(
            select(ScheduledTask)
            .where(ScheduledTask.uid == uid, ScheduledTask.profile_id == profile_id)
            .order_by(ScheduledTask.id.asc())
        )
        return list(result.scalars().all())

    async def delete_by_profile(
        self,
        db: AsyncSession,
        *,
        uid: str,
        profile_id: int,
        commit: bool = False,
    ) -> int:
        result = await db.execute(
            delete(ScheduledTask).where(
                ScheduledTask.uid == uid,
                ScheduledTask.profile_id == profile_id,
            )
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return result.rowcount or 0

    async def create_scheduled_task(
        self,
        db: AsyncSession,
        *,
        name: str,
        uid: str,
        session_id: str,
        profile_id: int | None,
        message: str,
        interval_seconds: int,
    ) -> ScheduledTask:
        now = get_local_time()
        scheduled_task = ScheduledTask(
            name=name,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
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

    async def has_profile_assignment(self, db: AsyncSession, profile_id: int) -> bool:
        stmt = select(ScheduledTask.id).where(ScheduledTask.profile_id == profile_id).limit(1)
        result = await db.execute(stmt)
        return result.scalar() is not None

    async def list_due(self, db: AsyncSession, *, limit: int = 100) -> list[ScheduledTask]:
        now = get_local_time()
        stmt = select(ScheduledTask).where(ScheduledTask.status == ScheduledTaskStatus.ENABLED).where(ScheduledTask.next_run_at <= now).order_by(ScheduledTask.next_run_at.asc()).limit(limit)
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

    async def disable_task(self, db: AsyncSession, *, scheduled_task: ScheduledTask) -> ScheduledTask:
        scheduled_task.status = ScheduledTaskStatus.DISABLED
        scheduled_task.updated_at = get_local_time()
        db.add(scheduled_task)
        await db.commit()
        await db.refresh(scheduled_task)
        return scheduled_task

    async def disable_by_session(self, db: AsyncSession, *, uid: str, session_id: str) -> int:
        stmt = select(ScheduledTask).where(ScheduledTask.uid == uid).where(ScheduledTask.session_id == session_id).where(ScheduledTask.status == ScheduledTaskStatus.ENABLED)
        result = await db.execute(stmt)
        scheduled_tasks = list(result.scalars().all())
        now = get_local_time()
        for scheduled_task in scheduled_tasks:
            scheduled_task.status = ScheduledTaskStatus.DISABLED
            scheduled_task.updated_at = now
            db.add(scheduled_task)
        if scheduled_tasks:
            await db.commit()
        return len(scheduled_tasks)

    async def delete_by_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str | None = None,
        is_admin: bool = False,
        commit: bool = True,
    ) -> int:
        conditions = [ScheduledTask.session_id == session_id]
        if not is_admin:
            conditions.append(ScheduledTask.uid == uid)
        result = await db.execute(delete(ScheduledTask).where(*conditions).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        return result.rowcount or 0

    async def delete_task(self, db: AsyncSession, *, scheduled_task: ScheduledTask) -> None:
        await db.delete(scheduled_task)
        await db.commit()


scheduled_task_crud = CRUDScheduledTask(ScheduledTask)
