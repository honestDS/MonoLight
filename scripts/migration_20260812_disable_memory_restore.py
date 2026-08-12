from __future__ import annotations

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_MEMORY_RESTORE_JOB_DISABLED
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
)
from app.providers.database.time import get_database_time

MIGRATION_ID = "20260812_disable_memory_restore_v1"


async def migrate(session: AsyncSession) -> None:
    unfinished_statuses = {
        LongTermMemoryMutationStatus.PENDING,
        LongTermMemoryMutationStatus.RUNNING,
        LongTermMemoryMutationStatus.RETRY,
    }
    result = await session.execute(
        select(LongTermMemoryMutationJob).where(
            LongTermMemoryMutationJob.operation == LongTermMemoryMutationOperation.RESTORE,
            LongTermMemoryMutationJob.status.in_(unfinished_statuses),
        )
    )
    jobs = list(result.scalars().all())
    if not jobs:
        return

    now = await get_database_time(session)
    for job in jobs:
        job.status = LongTermMemoryMutationStatus.FAILED
        job.error = t(ERR_MEMORY_RESTORE_JOB_DISABLED)
        job.updated_at = now
        job.finished_at = now
        job.locked_by = None
        job.lock_until = None
        job.active_mutation_key = None
        if job.id is not None:
            await session.execute(
                update(LongTermMemoryRecord)
                .where(
                    LongTermMemoryRecord.uid == job.uid,
                    LongTermMemoryRecord.pending_mutation_job_id == job.id,
                )
                .values(pending_mutation_job_id=None, updated_at=now)
            )
    await session.flush()
