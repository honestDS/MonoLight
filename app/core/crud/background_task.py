from datetime import timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.core.utils.time import get_local_time
from app.models.background_task import (
    BackgroundTask,
    BackgroundTaskCreate,
    BackgroundTaskReplyStatus,
    BackgroundTaskStatus,
)


class CRUDBackgroundTask(CRUDBase[BackgroundTask, BackgroundTaskCreate, BackgroundTaskCreate]):
    async def create_task(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        tool_call_id: str,
        tool_name: str,
        arguments: dict[str, Any],
        auto_reply: bool = True,
        extra: dict[str, Any] | None = None,
    ) -> BackgroundTask:
        task = BackgroundTask(
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            arguments=arguments,
            auto_reply=auto_reply,
            reply_status=BackgroundTaskReplyStatus.PENDING if auto_reply else BackgroundTaskReplyStatus.NONE,
            extra=extra or {},
        )
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task

    async def list_pending(self, db: AsyncSession, *, profile_id: int | None = None, limit: int = 100) -> list[BackgroundTask]:
        stmt = select(BackgroundTask).where(BackgroundTask.status == BackgroundTaskStatus.PENDING).order_by(BackgroundTask.created_at.asc()).limit(limit)
        if profile_id is not None:
            stmt = stmt.where(BackgroundTask.profile_id == profile_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_user_tasks(self, db: AsyncSession, *, uid: str, session_id: str | None = None, skip: int = 0, limit: int = 100) -> list[BackgroundTask]:
        stmt = select(BackgroundTask).where(BackgroundTask.uid == uid).order_by(BackgroundTask.created_at.desc()).offset(skip).limit(limit)
        if session_id:
            stmt = stmt.where(BackgroundTask.session_id == session_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_active_user_tasks(self, db: AsyncSession, *, uid: str, session_id: str | None = None, skip: int = 0, limit: int = 100) -> list[BackgroundTask]:
        stmt = (
            select(BackgroundTask)
            .where(BackgroundTask.uid == uid)
            .where(BackgroundTask.status.in_([BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING]))
            .order_by(BackgroundTask.created_at.desc())
            .offset(skip)
            .limit(limit)
        )
        if session_id:
            stmt = stmt.where(BackgroundTask.session_id == session_id)
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def list_running_by_session(self, db: AsyncSession, *, session_id: str) -> list[BackgroundTask]:
        stmt = select(BackgroundTask).where(BackgroundTask.session_id == session_id).where(BackgroundTask.status == BackgroundTaskStatus.RUNNING).order_by(BackgroundTask.created_at.asc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def get_user_task(self, db: AsyncSession, *, task_id: int, uid: str) -> BackgroundTask | None:
        stmt = select(BackgroundTask).where(BackgroundTask.id == task_id).where(BackgroundTask.uid == uid)
        result = await db.execute(stmt)
        return result.scalars().first()

    async def try_claim(self, db: AsyncSession, *, task_id: int, worker_id: str, lease_seconds: int = 300) -> BackgroundTask | None:
        now = get_local_time()
        stmt = (
            update(BackgroundTask)
            .where(BackgroundTask.id == task_id)
            .where(BackgroundTask.status == BackgroundTaskStatus.PENDING)
            .values(
                status=BackgroundTaskStatus.RUNNING,
                locked_by=worker_id,
                lock_until=now + timedelta(seconds=lease_seconds),
                attempt_count=BackgroundTask.attempt_count + 1,
                started_at=now,
            )
        )
        result = await db.execute(stmt)
        await db.commit()
        if result.rowcount != 1:
            return None
        return await self.get(db, task_id)

    async def mark_succeeded(self, db: AsyncSession, *, task: BackgroundTask, result: dict[str, Any]) -> BackgroundTask:
        task.status = BackgroundTaskStatus.SUCCEEDED
        task.result = result
        task.error = None
        task.finished_at = get_local_time()
        task.lock_until = None
        task.locked_by = None
        if task.auto_reply:
            task.reply_status = BackgroundTaskReplyStatus.PENDING
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task

    async def mark_failed(self, db: AsyncSession, *, task: BackgroundTask, error: str) -> BackgroundTask:
        task.status = BackgroundTaskStatus.FAILED
        task.error = error
        task.finished_at = get_local_time()
        task.lock_until = None
        task.locked_by = None
        if task.auto_reply:
            task.reply_status = BackgroundTaskReplyStatus.PENDING
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task

    async def cancel_user_task(self, db: AsyncSession, *, task_id: int, uid: str) -> BackgroundTask | None:
        task = await self.get_user_task(db, task_id=task_id, uid=uid)
        if not task or task.status in {BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED, BackgroundTaskStatus.CANCELLED}:
            return task
        task.status = BackgroundTaskStatus.CANCELLED
        task.reply_status = BackgroundTaskReplyStatus.NONE
        task.finished_at = get_local_time()
        task.lock_until = None
        task.locked_by = None
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task

    async def requeue_expired_running(self, db: AsyncSession, *, profile_id: int, max_attempts: int = 3) -> list[int]:
        now = get_local_time()
        stmt = select(BackgroundTask).where(BackgroundTask.profile_id == profile_id).where(BackgroundTask.status == BackgroundTaskStatus.RUNNING).where(BackgroundTask.lock_until < now)
        result = await db.execute(stmt)
        tasks = list(result.scalars().all())
        reply_task_ids: list[int] = []
        for task in tasks:
            if task.attempt_count >= max_attempts:
                task.status = BackgroundTaskStatus.FAILED
                task.error = "Background task lease expired and max attempts exceeded"
                task.finished_at = now
                task.locked_by = None
                task.lock_until = None
                if task.auto_reply:
                    task.reply_status = BackgroundTaskReplyStatus.PENDING
                    if task.id is not None:
                        reply_task_ids.append(task.id)
            else:
                task.status = BackgroundTaskStatus.PENDING
                task.locked_by = None
                task.lock_until = None
            db.add(task)
        if tasks:
            await db.commit()
        return reply_task_ids

    async def set_reply_status(self, db: AsyncSession, *, task: BackgroundTask, status: BackgroundTaskReplyStatus, error: str | None = None) -> BackgroundTask:
        task.reply_status = status
        if error:
            task.error = error
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task


background_task_crud = CRUDBackgroundTask(BackgroundTask)
