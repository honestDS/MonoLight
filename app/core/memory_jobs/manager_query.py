from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.memory.job import memory_job_crud
from app.models.memory import (
    LongTermMemoryMutationJob,
)

from .manager_common import (
    _organization_job_target_identity,
)

__all__ = [
    "MemoryJobQuery",
]


class MemoryJobQuery:
    async def has_unfinished_target_identity(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_key: str,
        content_hash: str,
        exclude_job_id: int | None = None,
    ) -> bool:
        unfinished_jobs = await memory_job_crud.list_unfinished_by_uid(db, uid=uid)
        for job in unfinished_jobs:
            if exclude_job_id is not None and job.id == exclude_job_id:
                continue
            identity = _organization_job_target_identity(job)
            if identity is not None and (identity[0] == memory_key or identity[1] == content_hash):
                return True
        return False

    async def get_job(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
    ) -> LongTermMemoryMutationJob | None:
        return await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)

    async def get_job_by_dedupe_key(
        self,
        db: AsyncSession,
        *,
        uid: str,
        dedupe_key: str,
    ) -> LongTermMemoryMutationJob | None:
        return await memory_job_crud.get_by_dedupe_key(db, uid=uid, dedupe_key=dedupe_key)

    async def get_job_by_active_mutation_key(
        self,
        db: AsyncSession,
        *,
        uid: str,
        active_mutation_key: str,
    ) -> LongTermMemoryMutationJob | None:
        return await memory_job_crud.get_by_active_mutation_key(db, uid=uid, active_mutation_key=active_mutation_key)

    async def list_jobs(
        self,
        db: AsyncSession,
        *,
        uid: str,
        skip: int = 0,
        limit: int = 100,
    ) -> list[LongTermMemoryMutationJob]:
        return await memory_job_crud.list_by_uid(db, uid=uid, skip=skip, limit=limit)
