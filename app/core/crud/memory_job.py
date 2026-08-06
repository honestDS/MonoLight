from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_MEMORY_ACTIVE_MUTATION_KEY_CLEAR_STATUS_INVALID,
    ERR_MEMORY_JOB_FIELD_REQUIRED,
    ERR_MEMORY_JOB_OWNER_MISMATCH,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
)
from app.providers.database.time import get_database_time


def _input_data(obj_in: Any) -> dict[str, Any]:
    if obj_in is None:
        return {}
    if isinstance(obj_in, dict):
        return dict(obj_in)
    return obj_in.model_dump(exclude_unset=True)


def _resolve_owner(owner: str | None, worker_id: str | None) -> str:
    if owner is None:
        owner = worker_id
    elif worker_id is not None and worker_id != owner:
        raise ValueError(t(ERR_MEMORY_JOB_OWNER_MISMATCH))
    if owner is None:
        raise TypeError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="owner"))
    if not owner:
        raise ValueError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="owner"))
    return owner


def _validate_duration(value: int, *, field: str, minimum: int = 0) -> int:
    if value < minimum:
        error = ERR_VALUE_MUST_BE_POSITIVE if minimum >= 1 else ERR_VALUE_MUST_BE_NON_NEGATIVE
        raise ValueError(t(error, field=field))
    return value


def _nullable_equal(column: Any, value: Any) -> Any:
    return column.is_(None) if value is None else column == value


def _claimable_statement(
    *,
    uid: str | None,
    operations: list[LongTermMemoryMutationOperation],
    now: datetime,
    limit: int,
):
    conditions: list[Any] = [
        LongTermMemoryMutationJob.status.in_(
            [
                LongTermMemoryMutationStatus.PENDING,
                LongTermMemoryMutationStatus.RETRY,
            ]
        ),
        LongTermMemoryMutationJob.available_at <= now,
        LongTermMemoryMutationJob.cancel_requested_at.is_(None),
        LongTermMemoryMutationJob.operation.in_(operations),
    ]
    if uid is not None:
        conditions.insert(0, LongTermMemoryMutationJob.uid == uid)
    return select(LongTermMemoryMutationJob).where(*conditions).order_by(LongTermMemoryMutationJob.available_at.asc(), LongTermMemoryMutationJob.id.asc()).limit(limit)


@dataclass(frozen=True, slots=True)
class MemoryJobRecoveryResult:
    retried: int = 0
    failed: int = 0
    cancelled: int = 0
    terminal_jobs: tuple["MemoryJobRecoveryTerminal", ...] = ()

    @property
    def recovered(self) -> int:
        return self.retried + self.failed + self.cancelled


@dataclass(frozen=True, slots=True)
class MemoryJobRecoveryTerminal:
    job: LongTermMemoryMutationJob
    status: LongTermMemoryMutationStatus
    error: str | None = None


@dataclass(frozen=True, slots=True)
class MemoryJobCancelResult:
    job: LongTermMemoryMutationJob | None
    accepted: bool
    changed: bool
    error: str | None = None


class CRUDLongTermMemoryMutationJob:
    async def get_by_id(self, db: AsyncSession, *, uid: str, job_id: int) -> LongTermMemoryMutationJob | None:
        result = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_by_dedupe_key(self, db: AsyncSession, *, uid: str, dedupe_key: str) -> LongTermMemoryMutationJob | None:
        result = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.dedupe_key == dedupe_key).execution_options(populate_existing=True))
        return result.scalars().first()

    async def get_by_active_mutation_key(self, db: AsyncSession, *, uid: str, active_mutation_key: str) -> LongTermMemoryMutationJob | None:
        result = await db.execute(
            select(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.active_mutation_key == active_mutation_key,
            )
            .execution_options(populate_existing=True)
        )
        return result.scalars().first()

    async def list_by_uid(
        self,
        db: AsyncSession,
        *,
        uid: str,
        skip: int = 0,
        limit: int = 100,
        status: LongTermMemoryMutationStatus | str | None = None,
        operation: LongTermMemoryMutationOperation | str | None = None,
        memory_id: int | None = None,
    ) -> list[LongTermMemoryMutationJob]:
        conditions: list[Any] = [LongTermMemoryMutationJob.uid == uid]
        if status is not None:
            conditions.append(LongTermMemoryMutationJob.status == status)
        if operation is not None:
            conditions.append(LongTermMemoryMutationJob.operation == operation)
        if memory_id is not None:
            conditions.append(LongTermMemoryMutationJob.memory_id == memory_id)
        result = await db.execute(
            select(LongTermMemoryMutationJob)
            .where(*conditions)
            .order_by(
                LongTermMemoryMutationJob.created_at.desc(),
                LongTermMemoryMutationJob.id.desc(),
            )
            .offset(skip)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def get_page(
        self,
        db: AsyncSession,
        *,
        uid: str,
        skip: int = 0,
        limit: int = 100,
        status: LongTermMemoryMutationStatus | str | None = None,
        operation: LongTermMemoryMutationOperation | str | None = None,
        memory_id: int | None = None,
    ) -> list[LongTermMemoryMutationJob]:
        return await self.list_by_uid(
            db,
            uid=uid,
            skip=skip,
            limit=limit,
            status=status,
            operation=operation,
            memory_id=memory_id,
        )

    async def count(
        self,
        db: AsyncSession,
        *,
        uid: str,
        status: LongTermMemoryMutationStatus | str | None = None,
        operation: LongTermMemoryMutationOperation | str | None = None,
        memory_id: int | None = None,
    ) -> int:
        conditions: list[Any] = [LongTermMemoryMutationJob.uid == uid]
        if status is not None:
            conditions.append(LongTermMemoryMutationJob.status == status)
        if operation is not None:
            conditions.append(LongTermMemoryMutationJob.operation == operation)
        if memory_id is not None:
            conditions.append(LongTermMemoryMutationJob.memory_id == memory_id)
        result = await db.execute(select(func.count()).select_from(LongTermMemoryMutationJob).where(*conditions))
        return int(result.scalar_one() or 0)

    async def count_pending_create(self, db: AsyncSession, *, uid: str) -> int:
        result = await db.execute(
            select(func.count())
            .select_from(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.operation == LongTermMemoryMutationOperation.CREATE,
                LongTermMemoryMutationJob.status.in_(
                    [
                        LongTermMemoryMutationStatus.PENDING,
                        LongTermMemoryMutationStatus.RUNNING,
                        LongTermMemoryMutationStatus.RETRY,
                    ]
                ),
            )
        )
        return int(result.scalar_one() or 0)

    async def list_claimable(
        self,
        db: AsyncSession,
        *,
        uid: str,
        enabled_operations: Iterable[LongTermMemoryMutationOperation | str],
        limit: int = 20,
    ) -> list[LongTermMemoryMutationJob]:
        limit = min(max(limit, 0), 100)
        if limit == 0:
            return []
        operations = [LongTermMemoryMutationOperation(operation) for operation in enabled_operations]
        if not operations:
            return []
        now = await get_database_time(db)
        result = await db.execute(_claimable_statement(uid=uid, operations=operations, now=now, limit=limit))
        return list(result.scalars().all())

    async def list_claimable_for_worker(
        self,
        db: AsyncSession,
        *,
        enabled_operations: Iterable[LongTermMemoryMutationOperation | str],
        limit: int = 20,
    ) -> list[LongTermMemoryMutationJob]:
        limit = min(max(limit, 0), 100)
        if limit == 0:
            return []
        operations = [LongTermMemoryMutationOperation(operation) for operation in enabled_operations]
        if not operations:
            return []
        now = await get_database_time(db)
        result = await db.execute(_claimable_statement(uid=None, operations=operations, now=now, limit=limit))
        return list(result.scalars().all())

    async def create(
        self,
        db: AsyncSession,
        *,
        uid: str,
        obj_in: Any = None,
        commit: bool = True,
        **values: Any,
    ) -> tuple[LongTermMemoryMutationJob, bool]:
        data = _input_data(obj_in)
        data.pop("uid", None)
        data.update(values)
        job = LongTermMemoryMutationJob.model_validate({"uid": uid, **data})

        try:
            async with db.begin_nested():
                db.add(job)
                await db.flush()
        except IntegrityError:
            existing = await self.get_by_dedupe_key(db, uid=uid, dedupe_key=job.dedupe_key)
            if existing is not None:
                return existing, False
            raise

        if commit:
            await db.commit()
        await db.refresh(job)
        return job, True

    async def create_job(self, db: AsyncSession, **kwargs: Any) -> tuple[LongTermMemoryMutationJob, bool]:
        return await self.create(db, **kwargs)

    async def _clear_pending_mutation_job_reference(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_id: int | None,
        job_id: int,
        operation: LongTermMemoryMutationOperation,
        updated_at: datetime,
    ) -> None:
        if memory_id is None:
            return
        if operation == LongTermMemoryMutationOperation.CREATE:
            await db.execute(
                delete(LongTermMemoryRecord)
                .where(
                    LongTermMemoryRecord.uid == uid,
                    LongTermMemoryRecord.id == memory_id,
                    LongTermMemoryRecord.pending_mutation_job_id == job_id,
                    LongTermMemoryRecord.version == 0,
                    LongTermMemoryRecord.is_active.is_(False),
                )
                .execution_options(synchronize_session=False)
            )
            return
        await db.execute(
            update(LongTermMemoryRecord)
            .where(
                LongTermMemoryRecord.uid == uid,
                LongTermMemoryRecord.id == memory_id,
                LongTermMemoryRecord.pending_mutation_job_id == job_id,
            )
            .values(
                pending_mutation_job_id=None,
                updated_at=updated_at,
            )
            .execution_options(synchronize_session=False)
        )

    async def try_claim(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        enabled_operations: Iterable[LongTermMemoryMutationOperation | str] | None = None,
        commit: bool = True,
    ) -> LongTermMemoryMutationJob | None:
        owner = _resolve_owner(owner, worker_id)
        lease_seconds = _validate_duration(lease_seconds, field="lease_seconds", minimum=1)
        now = await get_database_time(db)
        conditions: list[Any] = [
            LongTermMemoryMutationJob.uid == uid,
            LongTermMemoryMutationJob.id == job_id,
            LongTermMemoryMutationJob.status.in_(
                [
                    LongTermMemoryMutationStatus.PENDING,
                    LongTermMemoryMutationStatus.RETRY,
                ]
            ),
            LongTermMemoryMutationJob.available_at <= now,
            LongTermMemoryMutationJob.cancel_requested_at.is_(None),
        ]
        if enabled_operations is not None:
            operations = [LongTermMemoryMutationOperation(operation) for operation in enabled_operations]
            if not operations:
                return None
            conditions.append(LongTermMemoryMutationJob.operation.in_(operations))
        result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(*conditions)
            .values(
                status=LongTermMemoryMutationStatus.RUNNING,
                locked_by=owner,
                lock_until=now + timedelta(seconds=lease_seconds),
                attempt_count=LongTermMemoryMutationJob.attempt_count + 1,
                started_at=func.coalesce(LongTermMemoryMutationJob.started_at, now),
                error=None,
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        if (result.rowcount or 0) != 1:
            return None
        refreshed = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).execution_options(populate_existing=True))
        return refreshed.scalars().first()

    async def renew_lease(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        lease_seconds: int = 300,
        commit: bool = True,
    ) -> bool:
        owner = _resolve_owner(owner, worker_id)
        lease_seconds = _validate_duration(lease_seconds, field="lease_seconds", minimum=1)
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
                LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                LongTermMemoryMutationJob.locked_by == owner,
                LongTermMemoryMutationJob.lock_until >= now,
            )
            .values(
                lock_until=now + timedelta(seconds=lease_seconds),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def assign_create_memory_id(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        memory_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        commit: bool = True,
    ) -> bool:
        owner = _resolve_owner(owner, worker_id)
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
                LongTermMemoryMutationJob.operation == LongTermMemoryMutationOperation.CREATE,
                LongTermMemoryMutationJob.memory_id.is_(None),
            )
            .values(memory_id=memory_id, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def mark_succeeded(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        result: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> bool:
        return await self._mark_terminal(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            worker_id=worker_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
            result=result,
            commit=commit,
        )

    async def mark_failed(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> bool:
        return await self._mark_terminal(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            worker_id=worker_id,
            status=LongTermMemoryMutationStatus.FAILED,
            error=error,
            result=result,
            commit=commit,
        )

    async def mark_cancelled(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None = None,
        worker_id: str | None = None,
        commit: bool = True,
    ) -> bool:
        return await self._mark_terminal(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            worker_id=worker_id,
            status=LongTermMemoryMutationStatus.CANCELLED,
            commit=commit,
        )

    async def _mark_terminal(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        owner: str | None,
        worker_id: str | None,
        status: LongTermMemoryMutationStatus,
        error: str | None = None,
        result: dict[str, Any] | None = None,
        commit: bool = True,
    ) -> bool:
        owner = _resolve_owner(owner, worker_id)
        now = await get_database_time(db)
        job_result = await db.execute(
            select(
                LongTermMemoryMutationJob.memory_id,
                LongTermMemoryMutationJob.operation,
            ).where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
            )
        )
        job_row = job_result.one_or_none()
        memory_id = job_row[0] if job_row is not None else None
        operation = job_row[1] if job_row is not None else None
        update_values: dict[str, Any] = {
            "status": status,
            "error": error,
            "result": result,
            "finished_at": now,
            "locked_by": None,
            "lock_until": None,
            "active_mutation_key": None,
            "updated_at": now,
        }
        conditions: list[Any] = [
            LongTermMemoryMutationJob.uid == uid,
            LongTermMemoryMutationJob.id == job_id,
            LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
            LongTermMemoryMutationJob.locked_by == owner,
            LongTermMemoryMutationJob.lock_until >= now,
        ]
        if status != LongTermMemoryMutationStatus.CANCELLED:
            conditions.append(LongTermMemoryMutationJob.cancel_requested_at.is_(None))
        update_result = await db.execute(update(LongTermMemoryMutationJob).where(*conditions).values(**update_values).execution_options(synchronize_session=False))
        changed = (update_result.rowcount or 0) == 1
        if changed:
            await self._clear_pending_mutation_job_reference(
                db,
                uid=uid,
                memory_id=memory_id,
                job_id=job_id,
                operation=operation,
                updated_at=now,
            )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return changed

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
        if job.operation == LongTermMemoryMutationOperation.DELETE_CLEANUP or job.status in {
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
        if job.operation == LongTermMemoryMutationOperation.DELETE_CLEANUP or job.status in {
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
                updated_at=update_values["updated_at"],
            )
        if commit:
            await db.commit()
        else:
            await db.flush()
        refreshed = await db.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).execution_options(populate_existing=True))
        return refreshed.scalars().first()


memory_job_crud = CRUDLongTermMemoryMutationJob()
long_term_memory_mutation_job_crud = memory_job_crud
