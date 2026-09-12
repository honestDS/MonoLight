from collections.abc import Iterable
from typing import Any

from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
)
from app.providers.database.time import get_database_time

from .job_common import (
    _claimable_statement,
)

__all__ = [
    "CRUDLongTermMemoryMutationJobQuery",
]


class CRUDLongTermMemoryMutationJobQuery:
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

    async def list_unfinished_by_uid(
        self,
        db: AsyncSession,
        *,
        uid: str,
    ) -> list[LongTermMemoryMutationJob]:
        result = await db.execute(
            select(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.status.in_(
                    [
                        LongTermMemoryMutationStatus.PENDING,
                        LongTermMemoryMutationStatus.RUNNING,
                        LongTermMemoryMutationStatus.RETRY,
                    ]
                ),
            )
            .order_by(LongTermMemoryMutationJob.id.asc())
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def list_active_organization_jobs_for_admin(self, db: AsyncSession) -> list[LongTermMemoryMutationJob]:
        result = await db.execute(
            select(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.operation == LongTermMemoryMutationOperation.ORGANIZE,
                LongTermMemoryMutationJob.status.in_(
                    [
                        LongTermMemoryMutationStatus.PENDING,
                        LongTermMemoryMutationStatus.RUNNING,
                        LongTermMemoryMutationStatus.RETRY,
                    ]
                ),
            )
            .order_by(LongTermMemoryMutationJob.uid.asc(), LongTermMemoryMutationJob.id.asc())
            .execution_options(populate_existing=True)
        )
        return list(result.scalars().all())

    async def list_children_by_parent_job_id(
        self,
        db: AsyncSession,
        *,
        uid: str,
        parent_job_id: int,
        skip: int = 0,
        limit: int = 100,
    ) -> list[LongTermMemoryMutationJob]:
        result = await db.execute(
            select(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.parent_job_id == parent_job_id,
            )
            .order_by(
                LongTermMemoryMutationJob.created_at.asc(),
                LongTermMemoryMutationJob.id.asc(),
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
