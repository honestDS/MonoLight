from typing import Any

from sqlalchemy import String, case, cast, delete, not_, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION, ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN
from app.core.crud.audit.audit import audit_crud
from app.core.crud.base import CRUDBase
from app.core.i18n import t
from app.core.utils.background_task_result import build_background_task_failure_result
from app.core.utils.time import get_local_time
from app.models.audit import AUDIT_TERMINAL_STATUSES, AuditRecord, AuditRecordStatus
from app.models.background_task import (
    BackgroundTask,
    BackgroundTaskCreate,
    BackgroundTaskReplyStatus,
    BackgroundTaskStatus,
)
from app.providers.database.time import get_database_time, get_database_timestamp


class CRUDBackgroundTask(CRUDBase[BackgroundTask, BackgroundTaskCreate, BackgroundTaskCreate]):
    @staticmethod
    def _not_unknown_audit_condition():
        unknown_audit = select(AuditRecord.id).where(
            AuditRecord.id == BackgroundTask.audit_record_id,
            AuditRecord.status == AuditRecordStatus.EXECUTION_UNKNOWN,
        )
        return not_(unknown_audit.exists())

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
        audit_record_id: int | None = None,
        audit_execution_record_id: int | None = None,
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
            audit_record_id=audit_record_id,
            audit_execution_record_id=audit_execution_record_id,
        )
        db.add(task)
        try:
            await db.commit()
        except IntegrityError:
            await db.rollback()
            if audit_execution_record_id is None:
                raise
            existing = await self.get_by_audit_execution_record_id(db, audit_execution_record_id)
            if existing is None:
                raise
            return existing
        await db.refresh(task)
        return task

    async def get_by_audit_execution_record_id(self, db: AsyncSession, audit_execution_record_id: int) -> BackgroundTask | None:
        result = await db.execute(select(BackgroundTask).where(BackgroundTask.audit_execution_record_id == audit_execution_record_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def list_by_audit_record(self, db: AsyncSession, audit_record_id: int) -> list[BackgroundTask]:
        result = await db.execute(select(BackgroundTask).where(BackgroundTask.audit_record_id == audit_record_id).order_by(BackgroundTask.id).execution_options(populate_existing=True))
        return list(result.scalars().all())

    async def list_pending(self, db: AsyncSession, *, profile_id: int | None = None, limit: int = 100) -> list[BackgroundTask]:
        stmt = select(BackgroundTask).where(BackgroundTask.status == BackgroundTaskStatus.PENDING, self._not_unknown_audit_condition()).order_by(BackgroundTask.created_at.asc()).limit(limit)
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
        """按审计轮次先取消未执行任务，再关闭运行中任务并一次提交。"""
        conditions = [BackgroundTask.session_id == session_id]
        if not is_admin:
            conditions.append(BackgroundTask.uid == uid)

        bound_result = await db.execute(
            select(BackgroundTask).where(
                *conditions,
                BackgroundTask.status.in_([BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING]),
                BackgroundTask.audit_record_id.is_not(None),
                BackgroundTask.audit_execution_record_id.is_not(None),
            )
        )
        bound_tasks = list(bound_result.scalars().all())
        pending_tasks = [task for task in bound_tasks if task.status == BackgroundTaskStatus.PENDING]
        running_tasks = [task for task in bound_tasks if task.status == BackgroundTaskStatus.RUNNING]

        unknown_audit_ids: set[int] = set()
        for task in pending_tasks:
            cancelled = await self._cancel_task_conditionally(db, task, deleted_session=True, require_unexpired_lease=False)
            if not cancelled:
                continue
            audit_marked = await self._mark_audit_cancelled(db, task, error_reason=t(ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION))
            if audit_marked:
                await db.execute(delete(BackgroundTask).where(BackgroundTask.id == task.id))

        for task in running_tasks:
            cancelled = await self._cancel_task_conditionally(db, task, deleted_session=True, require_unexpired_lease=False)
            if not cancelled or task.audit_record_id in unknown_audit_ids:
                continue
            audit_marked = await self._mark_audit_unknown(db, task, error_reason=t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
            if audit_marked and task.audit_record_id is not None:
                unknown_audit_ids.add(task.audit_record_id)

        unbound_conditions = [
            *conditions,
            BackgroundTask.audit_record_id.is_(None),
            BackgroundTask.audit_execution_record_id.is_(None),
        ]
        cancelled_result = await db.execute(
            update(BackgroundTask)
            .where(
                *unbound_conditions,
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
        retained_count = sum(returned_session_id != session_id for returned_session_id in cancelled_result.scalars().all())
        delete_result = await db.execute(delete(BackgroundTask).where(*unbound_conditions).execution_options(synchronize_session=False))
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
            .where(self._not_unknown_audit_condition())
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

    async def mark_execution_started(self, db: AsyncSession, *, task_id: int, worker_id: str, extra: dict[str, Any]) -> bool:
        now = await get_database_timestamp(db)
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.locked_by == worker_id,
                BackgroundTask.lock_until >= now,
            )
            .values(extra=extra)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
        return result.rowcount == 1

    async def release_claim(self, db: AsyncSession, *, task_id: int, worker_id: str, expected_lock_until: int | None = None) -> bool:
        task = await self.get(db, task_id)
        if task is None:
            return False
        if task.audit_execution_record_id is not None:
            if task.status != BackgroundTaskStatus.RUNNING or task.locked_by != worker_id or task.lock_until is None or (expected_lock_until is not None and task.lock_until != expected_lock_until):
                return False
            return await self.mark_execution_unknown(
                db,
                task_id=task_id,
                worker_id=worker_id,
                error=t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN),
                result=build_background_task_failure_result(task.tool_name, t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN)),
                auto_reply=task.auto_reply,
                expected_lock_until=expected_lock_until if expected_lock_until is not None else task.lock_until,
            )
        result = await db.execute(
            update(BackgroundTask)
            .where(
                BackgroundTask.id == task_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.locked_by == worker_id,
                BackgroundTask.lock_until == (expected_lock_until if expected_lock_until is not None else task.lock_until),
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

    async def mark_succeeded(
        self,
        db: AsyncSession,
        *,
        task_id: int,
        worker_id: str,
        result: dict[str, Any],
        auto_reply: bool,
        commit: bool = True,
    ) -> bool:
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
        if commit:
            await db.commit()
        return update_result.rowcount == 1

    async def mark_failed(
        self,
        db: AsyncSession,
        *,
        task_id: int,
        worker_id: str,
        error: str,
        result: dict[str, Any],
        auto_reply: bool,
        extra: dict[str, Any] | None = None,
        commit: bool = True,
        require_unexpired_lease: bool = True,
        expected_lock_until: int | None = None,
    ) -> bool:
        now = await get_database_timestamp(db)
        finished_at = await get_database_time(db)
        values: dict[str, Any] = {
            "status": BackgroundTaskStatus.FAILED,
            "result": result,
            "error": error,
            "finished_at": finished_at,
            "lock_until": None,
            "locked_by": None,
            "reply_status": BackgroundTaskReplyStatus.PENDING if auto_reply else BackgroundTaskReplyStatus.NONE,
            "reply_locked_by": None,
            "reply_lock_until": None,
        }
        if extra is not None:
            values["extra"] = extra
        conditions = [
            BackgroundTask.id == task_id,
            BackgroundTask.status == BackgroundTaskStatus.RUNNING,
            BackgroundTask.locked_by == worker_id,
        ]
        if expected_lock_until is not None:
            conditions.append(BackgroundTask.lock_until == expected_lock_until)
        if require_unexpired_lease:
            conditions.append(BackgroundTask.lock_until >= now)
        update_result = await db.execute(update(BackgroundTask).where(*conditions).values(**values).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        return update_result.rowcount == 1

    async def _mark_audit_unknown(self, db: AsyncSession, task: BackgroundTask, *, error_reason: str) -> bool:
        if task.audit_record_id is None or task.audit_execution_record_id is None:
            return False
        execution = await audit_crud.get_execution_record(db, task.audit_execution_record_id)
        if execution is None:
            return False
        return await audit_crud.mark_execution_unknown(
            db,
            audit_record_id=task.audit_record_id,
            claim_token=execution.claim_token,
            execution_record_id=task.audit_execution_record_id,
            error_reason=error_reason,
            commit=False,
        )

    async def _mark_audit_cancelled(self, db: AsyncSession, task: BackgroundTask, *, error_reason: str) -> bool:
        if task.audit_record_id is None or task.audit_execution_record_id is None:
            return False
        execution = await audit_crud.get_execution_record(db, task.audit_execution_record_id)
        if execution is None:
            return False
        cancelled = await audit_crud.cancel_execution_attempt(
            db,
            audit_record_id=task.audit_record_id,
            execution_record_id=task.audit_execution_record_id,
            claim_token=execution.claim_token,
            error_reason=error_reason,
            commit=False,
        )
        if not cancelled:
            return False
        await audit_crud.finish_execution_round_if_complete(
            db,
            audit_record_id=task.audit_record_id,
            claim_token=execution.claim_token,
            commit=False,
        )
        return True

    async def mark_execution_unknown(
        self,
        db: AsyncSession,
        *,
        task_id: int,
        worker_id: str,
        error: str,
        result: dict[str, Any],
        auto_reply: bool,
        require_unexpired_lease: bool = False,
        expected_lock_until: int | None = None,
    ) -> bool:
        task = await self.get(db, task_id)
        if task is None or task.audit_execution_record_id is None:
            return False
        extra = {
            **(task.extra if isinstance(task.extra, dict) else {}),
            "audit_execution_unknown": True,
        }
        marked = await self.mark_failed(
            db,
            task_id=task_id,
            worker_id=worker_id,
            error=error,
            result=result,
            auto_reply=auto_reply,
            extra=extra,
            commit=False,
            require_unexpired_lease=require_unexpired_lease,
            expected_lock_until=expected_lock_until,
        )
        if not marked:
            await db.rollback()
            return False
        audit_marked = await self._mark_audit_unknown(db, task, error_reason=error)
        if not audit_marked:
            execution = await audit_crud.get_execution_record(db, task.audit_execution_record_id)
            record = await audit_crud.get_record(db, task.audit_record_id) if task.audit_record_id is not None else None
            audit_marked = execution is not None and execution.status == "execution_unknown" and record is not None and record.status == "execution_unknown"
        if not audit_marked:
            await db.rollback()
            return False
        await db.commit()
        return True

    async def _cancel_task_conditionally(self, db: AsyncSession, task: BackgroundTask, *, deleted_session: bool = False, require_unexpired_lease: bool = True) -> bool:
        conditions = [
            BackgroundTask.id == task.id,
            BackgroundTask.status == task.status,
        ]
        values: dict[str, Any] = {
            "status": BackgroundTaskStatus.CANCELLED,
            "auto_reply": False,
            "reply_status": BackgroundTaskReplyStatus.NONE,
            "finished_at": get_local_time(),
            "locked_by": None,
            "lock_until": None,
            "reply_locked_by": None,
            "reply_lock_until": None,
        }
        if task.status == BackgroundTaskStatus.RUNNING:
            conditions.extend([BackgroundTask.locked_by == task.locked_by, BackgroundTask.lock_until == task.lock_until])
            if require_unexpired_lease:
                conditions.append(BackgroundTask.lock_until >= await get_database_timestamp(db))
            if deleted_session and task.id is not None:
                values["session_id"] = f"deleted-session:{task.id}"
        result = await db.execute(update(BackgroundTask).where(*conditions).values(**values).execution_options(synchronize_session=False))
        return (result.rowcount or 0) == 1

    async def cancel_user_task(self, db: AsyncSession, *, task_id: int, uid: str) -> BackgroundTask | None:
        task = await self.get_user_task(db, task_id=task_id, uid=uid)
        if not task or task.status in {BackgroundTaskStatus.SUCCEEDED, BackgroundTaskStatus.FAILED, BackgroundTaskStatus.CANCELLED}:
            return task
        was_running = task.status == BackgroundTaskStatus.RUNNING
        cancelled = await self._cancel_task_conditionally(db, task)
        if not cancelled:
            await db.rollback()
            return await self.get_user_task(db, task_id=task_id, uid=uid)
        if task.audit_execution_record_id is not None:
            audit_marked = await self._mark_audit_unknown(db, task, error_reason=t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN)) if was_running else await self._mark_audit_cancelled(db, task, error_reason=t(ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION))
            if not audit_marked:
                await db.rollback()
                return await self.get_user_task(db, task_id=task_id, uid=uid)
        await db.commit()
        await db.refresh(task)
        return task

    async def fail_tasks_for_terminal_audits(self, db: AsyncSession, *, error: str) -> int:
        terminal_audit_ids = select(AuditRecord.id).where(AuditRecord.status.in_(AUDIT_TERMINAL_STATUSES))
        result = await db.execute(
            select(BackgroundTask).where(
                BackgroundTask.audit_record_id.in_(terminal_audit_ids),
                BackgroundTask.audit_execution_record_id.is_not(None),
                BackgroundTask.status.in_([BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING]),
            )
        )
        tasks = list(result.scalars().all())
        failed_count = 0
        for task in tasks:
            conditions = [
                BackgroundTask.id == task.id,
                BackgroundTask.status == task.status,
            ]
            if task.status == BackgroundTaskStatus.RUNNING:
                conditions.extend([BackgroundTask.locked_by == task.locked_by, BackgroundTask.lock_until == task.lock_until])
            update_result = await db.execute(
                update(BackgroundTask)
                .where(*conditions)
                .values(
                    status=BackgroundTaskStatus.FAILED,
                    error=error,
                    result=build_background_task_failure_result(task.tool_name, error),
                    finished_at=get_local_time(),
                    locked_by=None,
                    lock_until=None,
                    reply_status=BackgroundTaskReplyStatus.PENDING if task.auto_reply else BackgroundTaskReplyStatus.NONE,
                    reply_locked_by=None,
                    reply_lock_until=None,
                )
                .execution_options(synchronize_session=False)
            )
            failed_count += update_result.rowcount or 0
        await db.commit()
        return failed_count

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
                BackgroundTask.audit_record_id,
                BackgroundTask.audit_execution_record_id,
            ).where(
                BackgroundTask.profile_id == profile_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.lock_until < now,
            )
        )
        expired_tasks = list(result.all())
        reply_task_ids: list[int] = []
        for task_id, locked_by, lock_until, attempt_count, auto_reply, tool_name, audit_record_id, audit_execution_record_id in expired_tasks:
            conditions = (
                BackgroundTask.id == task_id,
                BackgroundTask.status == BackgroundTaskStatus.RUNNING,
                BackgroundTask.locked_by == locked_by,
                BackgroundTask.lock_until == lock_until,
                BackgroundTask.lock_until < now,
            )
            if audit_execution_record_id is not None:
                update_result = await db.execute(
                    update(BackgroundTask)
                    .where(*conditions)
                    .values(
                        status=BackgroundTaskStatus.FAILED,
                        error=t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN),
                        result=build_background_task_failure_result(tool_name, t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN)),
                        finished_at=finished_at,
                        locked_by=None,
                        lock_until=None,
                        reply_status=BackgroundTaskReplyStatus.PENDING if auto_reply else BackgroundTaskReplyStatus.NONE,
                        reply_locked_by=None,
                        reply_lock_until=None,
                    )
                    .execution_options(synchronize_session=False)
                )
                if update_result.rowcount == 1:
                    task = await self.get(db, task_id)
                    audit_marked = task is not None and await self._mark_audit_unknown(db, task, error_reason=t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
                    if not audit_marked and task is not None:
                        execution = await audit_crud.get_execution_record(db, task.audit_execution_record_id)
                        record = await audit_crud.get_record(db, task.audit_record_id) if task.audit_record_id is not None else None
                        audit_marked = execution is not None and execution.status == "execution_unknown" and record is not None and record.status == "execution_unknown"
                    if not audit_marked:
                        await db.rollback()
                        continue
                    if auto_reply:
                        reply_task_ids.append(task_id)
                    await db.commit()
            elif attempt_count >= max_attempts:
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
