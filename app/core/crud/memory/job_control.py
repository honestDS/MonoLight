from datetime import timedelta
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_MEMORY_ACTIVE_MUTATION_KEY_CLEAR_STATUS_INVALID,
)
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
)
from app.providers.database.time import get_database_time

from .job_common import (
    MemoryJobCancelResult,
    MemoryJobRecoveryResult,
    MemoryJobRecoveryTerminal,
    _nullable_equal,
    _resolve_owner,
    _validate_duration,
)

__all__ = [
    "CRUDLongTermMemoryMutationJobControl",
]


class CRUDLongTermMemoryMutationJobControl:
    async def release_for_retry(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        error: str | None = None,
        delay_seconds: int = 0,
        commit: bool = True,
    ) -> bool:
        owner = _resolve_owner(owner, worker_id)
        delay_seconds = _validate_duration(delay_seconds, field="delay_seconds")
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
                LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                LongTermMemoryMutationJob.locked_by == owner,
                LongTermMemoryMutationJob.lock_until >= now,
                LongTermMemoryMutationJob.cancel_requested_at.is_(None),
            )
            .values(
                status=LongTermMemoryMutationStatus.RETRY,
                available_at=now + timedelta(seconds=delay_seconds),
                error=error,
                locked_by=None,
                lock_until=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def recover_expired(
        self,
        db: AsyncSession,
        *,
        delay_seconds: int = 0,
        max_attempts_error: str | None = None,
        commit: bool = True,
    ) -> MemoryJobRecoveryResult:
        delay_seconds = _validate_duration(delay_seconds, field="delay_seconds")
        now = await get_database_time(db)
        result = await db.execute(
            select(LongTermMemoryMutationJob).where(
                LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                LongTermMemoryMutationJob.lock_until < now,
            )
        )
        expired_jobs = list(result.scalars().all())
        retried = 0
        failed = 0
        cancelled = 0
        terminal_jobs: list[MemoryJobRecoveryTerminal] = []
        for job in expired_jobs:
            if job.id is None or job.locked_by is None or job.lock_until is None:
                continue
            if job.cancel_requested_at is not None:
                next_status = LongTermMemoryMutationStatus.CANCELLED
                values: dict[str, Any] = {
                    "status": next_status,
                    "finished_at": now,
                    "active_mutation_key": None,
                }
            elif job.attempt_count >= job.max_attempts:
                next_status = LongTermMemoryMutationStatus.FAILED
                values = {
                    "status": next_status,
                    "finished_at": now,
                    "active_mutation_key": None,
                }
                if max_attempts_error is not None:
                    values["error"] = max_attempts_error
            else:
                next_status = LongTermMemoryMutationStatus.RETRY
                values = {
                    "status": next_status,
                    "available_at": now + timedelta(seconds=delay_seconds),
                }
            values.update(
                {
                    "locked_by": None,
                    "lock_until": None,
                    "updated_at": now,
                }
            )
            update_result = await db.execute(
                update(LongTermMemoryMutationJob)
                .where(
                    LongTermMemoryMutationJob.id == job.id,
                    LongTermMemoryMutationJob.uid == job.uid,
                    LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                    LongTermMemoryMutationJob.locked_by == job.locked_by,
                    LongTermMemoryMutationJob.lock_until == job.lock_until,
                    LongTermMemoryMutationJob.lock_until < now,
                    _nullable_equal(LongTermMemoryMutationJob.cancel_requested_at, job.cancel_requested_at),
                )
                .values(**values)
                .execution_options(synchronize_session=False)
            )
            if (update_result.rowcount or 0) != 1:
                continue
            if next_status in {
                LongTermMemoryMutationStatus.FAILED,
                LongTermMemoryMutationStatus.CANCELLED,
            }:
                await self._clear_pending_mutation_job_reference(
                    db,
                    uid=job.uid,
                    memory_id=job.memory_id,
                    job_id=job.id,
                    operation=job.operation,
                    payload=job.payload,
                    updated_at=now,
                )
                terminal_jobs.append(
                    MemoryJobRecoveryTerminal(
                        job=job,
                        status=next_status,
                        error=values.get("error"),
                    )
                )
            if next_status == LongTermMemoryMutationStatus.RETRY:
                retried += 1
            elif next_status == LongTermMemoryMutationStatus.FAILED:
                failed += 1
            else:
                cancelled += 1
        if commit:
            await db.commit()
        else:
            await db.flush()
        return MemoryJobRecoveryResult(
            retried=retried,
            failed=failed,
            cancelled=cancelled,
            terminal_jobs=tuple(terminal_jobs),
        )

    async def request_cancel(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        commit: bool = True,
    ) -> MemoryJobCancelResult:
        async def read_current() -> LongTermMemoryMutationJob | None:
            job_result = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).execution_options(populate_existing=True))
            return job_result.scalars().first()

        job = await read_current()
        if job is None:
            return MemoryJobCancelResult(job=None, accepted=False, changed=False)
        if job.operation in {
            LongTermMemoryMutationOperation.DELETE_CLEANUP,
            LongTermMemoryMutationOperation.VECTOR_CLEANUP,
        } or job.status in {
            LongTermMemoryMutationStatus.SUCCEEDED,
            LongTermMemoryMutationStatus.FAILED,
            LongTermMemoryMutationStatus.CANCELLED,
        }:
            return MemoryJobCancelResult(job=job, accepted=False, changed=False)

        now = await get_database_time(db)
        update_result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
                LongTermMemoryMutationJob.status.in_(
                    [
                        LongTermMemoryMutationStatus.PENDING,
                        LongTermMemoryMutationStatus.RETRY,
                    ]
                ),
            )
            .values(
                status=LongTermMemoryMutationStatus.CANCELLED,
                finished_at=now,
                locked_by=None,
                lock_until=None,
                active_mutation_key=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (update_result.rowcount or 0) == 1:
            await self._clear_pending_mutation_job_reference(
                db,
                uid=uid,
                memory_id=job.memory_id,
                job_id=job_id,
                operation=job.operation,
                payload=job.payload,
                updated_at=now,
            )
            if commit:
                await db.commit()
            else:
                await db.flush()
            return MemoryJobCancelResult(job=await read_current(), accepted=True, changed=True)

        job = await read_current()
        if job is None:
            return MemoryJobCancelResult(job=None, accepted=False, changed=False)
        if job.operation in {
            LongTermMemoryMutationOperation.DELETE_CLEANUP,
            LongTermMemoryMutationOperation.VECTOR_CLEANUP,
        } or job.status in {
            LongTermMemoryMutationStatus.SUCCEEDED,
            LongTermMemoryMutationStatus.FAILED,
            LongTermMemoryMutationStatus.CANCELLED,
        }:
            return MemoryJobCancelResult(job=job, accepted=False, changed=False)
        if job.status != LongTermMemoryMutationStatus.RUNNING:
            return MemoryJobCancelResult(job=job, accepted=False, changed=False)
        if job.cancel_requested_at is not None:
            return MemoryJobCancelResult(job=job, accepted=True, changed=False)

        now = await get_database_time(db)
        update_result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
                LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                LongTermMemoryMutationJob.locked_by == job.locked_by,
                _nullable_equal(LongTermMemoryMutationJob.lock_until, job.lock_until),
                LongTermMemoryMutationJob.cancel_requested_at.is_(None),
            )
            .values(cancel_requested_at=now, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if (update_result.rowcount or 0) == 1:
            if commit:
                await db.commit()
            else:
                await db.flush()
            return MemoryJobCancelResult(job=await read_current(), accepted=True, changed=True)

        current = await read_current()
        if current is not None and current.status == LongTermMemoryMutationStatus.RUNNING and current.cancel_requested_at is not None:
            return MemoryJobCancelResult(job=current, accepted=True, changed=False)
        return MemoryJobCancelResult(job=current, accepted=False, changed=False)

    async def get_active_claim(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
    ) -> LongTermMemoryMutationJob | None:
        owner = _resolve_owner(owner, worker_id)
        now = await get_database_time(db)
        result = await db.execute(
            select(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
                LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                LongTermMemoryMutationJob.locked_by == owner,
                LongTermMemoryMutationJob.lock_until >= now,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def is_cancel_requested(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
    ) -> bool:
        job = await self.get_active_claim(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            worker_id=worker_id,
        )
        return job is not None and job.cancel_requested_at is not None

    async def release_claim_for_shutdown(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        delay_seconds: int = 5,
        max_attempts_error: str | None = None,
        commit: bool = True,
    ) -> bool:
        owner = _resolve_owner(owner, worker_id)
        delay_seconds = _validate_duration(delay_seconds, field="delay_seconds")
        now = await get_database_time(db)
        job_result = await db.execute(
            select(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
                LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                LongTermMemoryMutationJob.locked_by == owner,
            )
            .execution_options(populate_existing=True)
        )
        job = job_result.scalars().first()
        if job is None:
            return False
        cancel_requested = job.cancel_requested_at is not None
        if cancel_requested:
            next_status = LongTermMemoryMutationStatus.CANCELLED
            values: dict[str, Any] = {
                "status": next_status,
                "finished_at": now,
                "active_mutation_key": None,
                "locked_by": None,
                "lock_until": None,
                "updated_at": now,
            }
        elif job.attempt_count >= job.max_attempts:
            next_status = LongTermMemoryMutationStatus.FAILED
            values = {
                "status": next_status,
                "finished_at": now,
                "active_mutation_key": None,
                "locked_by": None,
                "lock_until": None,
                "updated_at": now,
            }
            if max_attempts_error is not None:
                values["error"] = max_attempts_error
        else:
            next_status = LongTermMemoryMutationStatus.RETRY
            values = {
                "status": next_status,
                "available_at": now + timedelta(seconds=delay_seconds),
                "locked_by": None,
                "lock_until": None,
                "updated_at": now,
            }
        result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
                LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                LongTermMemoryMutationJob.locked_by == owner,
                _nullable_equal(LongTermMemoryMutationJob.lock_until, job.lock_until),
                _nullable_equal(LongTermMemoryMutationJob.cancel_requested_at, job.cancel_requested_at),
            )
            .values(**values)
            .execution_options(synchronize_session=False)
        )
        changed = (result.rowcount or 0) == 1
        if changed and next_status in {
            LongTermMemoryMutationStatus.CANCELLED,
            LongTermMemoryMutationStatus.FAILED,
        }:
            await self._clear_pending_mutation_job_reference(
                db,
                uid=uid,
                memory_id=job.memory_id,
                job_id=job_id,
                operation=job.operation,
                payload=job.payload,
                updated_at=now,
            )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return changed

    async def update_status(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        status: LongTermMemoryMutationStatus,
        commit: bool = True,
        clear_active_mutation_key: bool = False,
        **values: Any,
    ) -> LongTermMemoryMutationJob | None:
        status = LongTermMemoryMutationStatus(status)
        if clear_active_mutation_key and status not in {
            LongTermMemoryMutationStatus.SUCCEEDED,
            LongTermMemoryMutationStatus.FAILED,
            LongTermMemoryMutationStatus.CANCELLED,
        }:
            raise ValueError(t(ERR_MEMORY_ACTIVE_MUTATION_KEY_CLEAR_STATUS_INVALID, field="status"))
        allowed = {
            "result",
            "error",
            "available_at",
            "attempt_count",
            "locked_by",
            "lock_until",
            "cancel_requested_at",
            "started_at",
            "finished_at",
        }
        update_values = {key: value for key, value in values.items() if key in allowed}
        update_values["status"] = status
        update_values["updated_at"] = await get_database_time(db)
        if clear_active_mutation_key:
            update_values["active_mutation_key"] = None
        result = await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).values(**update_values).execution_options(synchronize_session=False))
        if (result.rowcount or 0) != 1:
            return None
        if clear_active_mutation_key:
            job_result = await db.execute(
                select(
                    LongTermMemoryMutationJob.memory_id,
                    LongTermMemoryMutationJob.operation,
                    LongTermMemoryMutationJob.payload,
                ).where(
                    LongTermMemoryMutationJob.uid == uid,
                    LongTermMemoryMutationJob.id == job_id,
                )
            )
            job_row = job_result.one_or_none()
            await self._clear_pending_mutation_job_reference(
                db,
                uid=uid,
                memory_id=job_row[0] if job_row is not None else None,
                job_id=job_id,
                operation=job_row[1] if job_row is not None else None,
                payload=job_row[2] if job_row is not None else None,
                updated_at=update_values["updated_at"],
            )
        if commit:
            await db.commit()
        else:
            await db.flush()
        refreshed = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).execution_options(populate_existing=True))
        return refreshed.scalars().first()
