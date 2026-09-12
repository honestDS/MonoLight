from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationStatus,
)
from app.providers.database.time import get_database_time

from .job_common import (
    _resolve_owner,
)

__all__ = [
    "CRUDLongTermMemoryMutationJobTerminal",
]


class CRUDLongTermMemoryMutationJobTerminal:
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
                LongTermMemoryMutationJob.payload,
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
                payload=job_row[2] if job_row is not None else None,
                updated_at=now,
            )
        if commit:
            await db.commit()
        else:
            await db.flush()
        return changed
