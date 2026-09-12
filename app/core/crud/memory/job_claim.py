from collections.abc import Iterable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import (
    ERR_MEMORY_JOB_FIELD_REQUIRED,
)
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
)
from app.providers.database.time import get_database_time

from .job_common import (
    _input_data,
    _resolve_owner,
    _validate_duration,
)

__all__ = [
    "CRUDLongTermMemoryMutationJobClaim",
]


class CRUDLongTermMemoryMutationJobClaim:
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
        payload: dict[str, Any] | None,
        updated_at: datetime,
    ) -> None:
        if operation == LongTermMemoryMutationOperation.CREATE:
            if memory_id is None:
                return
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
        if operation == LongTermMemoryMutationOperation.CREATE_WITH_EVICTION:
            if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id < 1:
                memory_id = None
            if memory_id is not None:
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
            candidate = payload.get("candidate") if isinstance(payload, dict) else None
            candidate_memory_id = candidate.get("memory_id") if isinstance(candidate, dict) else None
            if isinstance(candidate_memory_id, bool) or not isinstance(candidate_memory_id, int) or candidate_memory_id < 1:
                return
            await db.execute(
                update(LongTermMemoryRecord)
                .where(
                    LongTermMemoryRecord.uid == uid,
                    LongTermMemoryRecord.id == candidate_memory_id,
                    LongTermMemoryRecord.pending_mutation_job_id == job_id,
                )
                .values(
                    pending_mutation_job_id=None,
                    updated_at=updated_at,
                )
                .execution_options(synchronize_session=False)
            )
            return
        if operation == LongTermMemoryMutationOperation.ORGANIZE_MERGE:
            source_ids: set[int] = set()
            sources = payload.get("sources") if isinstance(payload, dict) else None
            if isinstance(sources, list):
                for source in sources:
                    if not isinstance(source, dict):
                        source_ids.clear()
                        break
                    memory_id = source.get("memory_id")
                    if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id < 1:
                        source_ids.clear()
                        break
                    source_ids.add(memory_id)
            if not source_ids:
                return
            await db.execute(
                update(LongTermMemoryRecord)
                .where(
                    LongTermMemoryRecord.uid == uid,
                    LongTermMemoryRecord.id.in_(source_ids),
                    LongTermMemoryRecord.pending_mutation_job_id == job_id,
                )
                .values(
                    pending_mutation_job_id=None,
                    updated_at=updated_at,
                )
                .execution_options(synchronize_session=False)
            )
            return
        if memory_id is None:
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

    async def update_running_payload(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        payload: dict[str, Any],
        owner: str | None = None,
        worker_id: str | None = None,
        commit: bool = True,
    ) -> bool:
        if not isinstance(uid, str) or not uid:
            raise ValueError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="uid"))
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
            raise ValueError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="job_id"))
        if not isinstance(payload, dict):
            raise TypeError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="payload"))
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
            )
            .values(payload=payload, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1

    async def update_running_result(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        result: dict[str, Any],
        owner: str | None = None,
        worker_id: str | None = None,
        commit: bool = True,
    ) -> bool:
        if not isinstance(uid, str) or not uid:
            raise ValueError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="uid"))
        if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
            raise ValueError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="job_id"))
        if not isinstance(result, dict):
            raise TypeError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="result"))
        owner = _resolve_owner(owner, worker_id)
        now = await get_database_time(db)
        update_result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job_id,
                LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                LongTermMemoryMutationJob.locked_by == owner,
                LongTermMemoryMutationJob.lock_until >= now,
                LongTermMemoryMutationJob.cancel_requested_at.is_(None),
            )
            .values(result=result, updated_at=now)
            .execution_options(synchronize_session=False)
        )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (update_result.rowcount or 0) == 1

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
        unclaimed_replacement = owner is None and worker_id is None
        if not unclaimed_replacement:
            owner = _resolve_owner(owner, worker_id)
        now = await get_database_time(db)
        conditions: list[Any] = [
            LongTermMemoryMutationJob.uid == uid,
            LongTermMemoryMutationJob.id == job_id,
            LongTermMemoryMutationJob.cancel_requested_at.is_(None),
            LongTermMemoryMutationJob.memory_id.is_(None),
        ]
        if unclaimed_replacement:
            conditions.extend(
                [
                    LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.PENDING,
                    LongTermMemoryMutationJob.operation == LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
                ]
            )
        else:
            conditions.extend(
                [
                    LongTermMemoryMutationJob.status == LongTermMemoryMutationStatus.RUNNING,
                    LongTermMemoryMutationJob.locked_by == owner,
                    LongTermMemoryMutationJob.lock_until >= now,
                    LongTermMemoryMutationJob.operation.in_(
                        [
                            LongTermMemoryMutationOperation.CREATE,
                            LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
                        ]
                    ),
                ]
            )
        result = await db.execute(update(LongTermMemoryMutationJob).where(*conditions).values(memory_id=memory_id, updated_at=now).execution_options(synchronize_session=False))
        if commit:
            await db.commit()
        else:
            await db.flush()
        return (result.rowcount or 0) == 1
