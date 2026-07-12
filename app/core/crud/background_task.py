from typing import Any

from sqlalchemy import String, case, cast, delete, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.base import CRUDBase
from app.core.utils.background_task_result import build_background_task_failure_result
from app.core.utils.time import get_local_time
from app.models.background_task import (
    BackgroundTask,
    BackgroundTaskCreate,
    BackgroundTaskReplyStatus,
    BackgroundTaskStatus,
)
from app.providers.database.time import get_database_time, get_database_timestamp


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

    async def list_pending_replies(self, db: AsyncSession, *, limit: int = 100) -> list[BackgroundTask]:
        stmt = (
            select(BackgroundTask)
            .where(
                BackgroundTask.status.in_([BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED]),
                BackgroundTask.auto_reply.is_(True),
                BackgroundTask.reply_status == BackgroundTaskReplyStatus.PENDING,
            )
            .order_by(BackgroundTask.finished_at.asc(), BackgroundTask.id.asc())
            .limit(limit)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def try_claim_reply(self, db: AsyncSession, *, task_id: int, worker_id: str, lease_seconds: int = 300) -> BackgroundTask | None:
        now = await get_database_timestamp(db)
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.status.in_([BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED]),
                BackgroundTask.auto_reply.is_(True),
                BackgroundTask.reply_status == BackgroundTaskReplyStatus.PENDING,
            )
            .values(
                reply_status=BackgroundTaskReplyStatus.RUNNING,
                reply_locked_by=worker_id,
                reply_lock_until=now + lease_seconds,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        if result.rowcount != 1:
            return None
        return await self.get(db, task_id)

    async def renew_reply_lease(self, db: AsyncSession, *, task_id: int, worker_id: str, lease_seconds: int = 300) -> bool:
        now = await get_database_timestamp(db)
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.reply_status == BackgroundTaskReplyStatus.RUNNING,
                BackgroundTask.reply_locked_by == worker_id,
                BackgroundTask.reply_lock_until >= now,
            )
            .values(reply_lock_until=now + lease_seconds)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def release_reply_claim(self, db: AsyncSession, *, task_id: int, worker_id: str) -> bool:
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.reply_status == BackgroundTaskReplyStatus.RUNNING,
                BackgroundTask.reply_locked_by == worker_id,
            )
            .values(
                reply_status=BackgroundTaskReplyStatus.PENDING,
                reply_locked_by=None,
                reply_lock_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def complete_reply_claim(
        self,
        db: AsyncSession,
        *,
        task_id: int,
        worker_id: str,
        status: BackgroundTaskReplyStatus,
        error: str | None = None,
    ) -> bool:
        now = await get_database_timestamp(db)
        values: dict[str, Any] = {
            "reply_status": status,
            "reply_locked_by": None,
            "reply_lock_until": None,
        }
        if error is not None:
            values["error"] = error
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.reply_status == BackgroundTaskReplyStatus.RUNNING,
                BackgroundTask.reply_locked_by == worker_id,
                BackgroundTask.reply_lock_until >= now,
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def recover_expired_replies(self, db: AsyncSession) -> int:
        now = await get_database_timestamp(db)
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.status.in_([BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED]),
                BackgroundTask.auto_reply.is_(True),
                BackgroundTask.reply_status == BackgroundTaskReplyStatus.RUNNING,
                BackgroundTask.reply_lock_until < now,
            )
            .values(
                reply_status=BackgroundTaskReplyStatus.PENDING,
                reply_locked_by=None,
                reply_lock_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount

    async def cleanup_by_session(
        self,
        db: AsyncSession,
        *,
        session_id: str,
        uid: str | None = None,
        is_admin: bool = False,
        commit: bool = True,
    ) -> int:
        conditions = [BackgroundTask.session_id == session_id]
        if not is_admin:
            conditions.append(BackgroundTask.uid == uid)

        cancelled_result = await db.execute(
            update(BackgroundTask)
            .where(
                *conditions,
                BackgroundTask.status.in_(
                    [
                        BackgroundTaskStatus.PENDING,
                        BackgroundTaskStatus.RUNNING,
                    ]
                ),
            )
            .values(
                status=BackgroundTaskStatus.CANCELLED,
                session_id=case(
                    (
                        BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                        "deleted-session:" + cast(BackgroundTask.id, String),
                    ),
                    else_=BackgroundTask.session_id,
                ),
                auto_reply=False,
                reply_status=BackgroundTaskReplyStatus.NONE,
                finished_at=get_local_time(),
                locked_by=None,
                lock_until=None,
                reply_locked_by=None,
                reply_lock_until=None,
            )
            .returning(BackgroundTask.session_id)
            .execution_options(synchronize_session=False)
        )
        retained_count = sum(
            returned_session_id != session_id
            for returned_session_id in cancelled_result.scalars().all()
        )
        delete_result = await db.execute(
            delete(BackgroundTask)
            .where(*conditions)
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        return retained_count + (delete_result.rowcount or 0)

    async def list_active_user_tasks(self, db: AsyncSession, *, uid: str, session_id: str | None = None, skip: int = 0, limit: int = 100) -> list[BackgroundTask]:
        stmt = select(BackgroundTask).where(BackgroundTask.uid == uid).where(BackgroundTask.status.in_([BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING])).order_by(BackgroundTask.created_at.desc()).offset(skip).limit(limit)
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
        now = await get_database_timestamp(db)
        started_at = await get_database_time(db)
        stmt = (
            update(BackgroundTask)
            .where(BackgroundTask.id == task_id)
            .where(BackgroundTask.status == BackgroundTaskStatus.PENDING)
            .values(
                status=BackgroundTaskStatus.RUNNING,
                locked_by=worker_id,
                lock_until=now + lease_seconds,
                attempt_count=BackgroundTask.attempt_count + 1,
                started_at=started_at,
            )
        )
        result = await db.execute(stmt)
        await db.commit()
        if result.rowcount != 1:
            return None
        return await self.get(db, task_id)

    async def renew_lease(self, db: AsyncSession, *, task_id: int, worker_id: str, lease_seconds: int = 300) -> bool:
        now = await get_database_timestamp(db)
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.locked_by == worker_id,
                BackgroundTask.lock_until >= now,
            )
            .values(lock_until=now + lease_seconds)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def release_claim(self, db: AsyncSession, *, task_id: int, worker_id: str) -> bool:
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.locked_by == worker_id,
            )
            .values(
                status=BackgroundTaskStatus.PENDING,
                locked_by=None,
                lock_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def mark_succeeded(self, db: AsyncSession, *, task_id: int, worker_id: str, result: dict[str, Any], auto_reply: bool) -> bool:
        now = await get_database_timestamp(db)
        finished_at = await get_database_time(db)
        update_result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.locked_by == worker_id,
                BackgroundTask.lock_until >= now,
            )
            .values(
                status=BackgroundTaskStatus.SUCCEEDED,
                result=result,
                error=None,
                finished_at=finished_at,
                lock_until=None,
                locked_by=None,
                reply_status=BackgroundTaskReplyStatus.PENDING if auto_reply else BackgroundTaskReplyStatus.NONE,
                reply_locked_by=None,
                reply_lock_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return update_result.rowcount == 1

    async def mark_failed(self, db: AsyncSession, *, task_id: int, worker_id: str, error: str, result: dict[str, Any], auto_reply: bool) -> bool:
        now = await get_database_timestamp(db)
        finished_at = await get_database_time(db)
        update_result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.locked_by == worker_id,
                BackgroundTask.lock_until >= now,
            )
            .values(
                status=BackgroundTaskStatus.FAILED,
                result=result,
                error=error,
                finished_at=finished_at,
                lock_until=None,
                locked_by=None,
                reply_status=BackgroundTaskReplyStatus.PENDING if auto_reply else BackgroundTaskReplyStatus.NONE,
                reply_locked_by=None,
                reply_lock_until=None,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return update_result.rowcount == 1

    async def cancel_user_task(self, db: AsyncSession, *, task_id: int, uid: str) -> BackgroundTask | None:
        task = await self.get_user_task(db, task_id=task_id, uid=uid)
        if not task or task.status in {BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED, BackgroundTaskStatus.CANCELLED}:
            return task
        task.status = BackgroundTaskStatus.CANCELLED
        task.reply_status = BackgroundTaskReplyStatus.NONE
        task.finished_at = get_local_time()
        task.lock_until = None
        task.locked_by = None
        task.reply_locked_by = None
        task.reply_lock_until = None
        db.add(task)
        await db.commit()
        await db.refresh(task)
        return task

    async def requeue_expired_running(self, db: AsyncSession, *, profile_id: int, max_attempts_error: str, max_attempts: int = 3) -> list[int]:
        now = await get_database_timestamp(db)
        finished_at = await get_database_time(db)
        result = await db.execute(
            select(
                BackgroundTask.id,
                BackgroundTask.locked_by,
                BackgroundTask.lock_until,
                BackgroundTask.attempt_count,
                BackgroundTask.auto_reply,
                BackgroundTask.tool_name,
            ).where(
                BackgroundTask.profile_id == profile_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.lock_until < now,
            )
        )
        expired_tasks = list(result.all())
        reply_task_ids: list[int] = []
        for task_id, locked_by, lock_until, attempt_count, auto_reply, tool_name in expired_tasks:
            conditions = (
                BackgroundTask.id == task_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.locked_by == locked_by,
                BackgroundTask.lock_until == lock_until,
                BackgroundTask.lock_until < now,
            )
            if attempt_count >= max_attempts:
                update_result = await db.execute(
                    update(BackgroundTask)
                    .where(*conditions)
                    .values(
                        status=BackgroundTaskStatus.FAILED,
                        error=max_attempts_error,
                        result=build_background_task_failure_result(tool_name, max_attempts_error),
                        finished_at=finished_at,
                        locked_by=None,
                        lock_until=None,
                        reply_status=BackgroundTaskReplyStatus.PENDING if auto_reply else BackgroundTaskReplyStatus.NONE,
                        reply_locked_by=None,
                        reply_lock_until=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if update_result.rowcount == 1 and auto_reply:
                    reply_task_ids.append(task_id)
            else:
                await db.execute(
                    update(BackgroundTask)
                    .where(*conditions)
                    .values(
                        status=BackgroundTaskStatus.PENDING,
                        locked_by=None,
                        lock_until=None,
                    )
                    .execution_options(synchronize_session=False)
                )
        if expired_tasks:
            await db.commit()
        return reply_task_ids


background_task_crud = CRUDBackgroundTask(BackgroundTask)
