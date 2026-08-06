from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import MEMORY_CONTENT_MAX_TOKENS
from app.core.crud.memory import memory_record_crud
from app.core.crud.memory_job import memory_job_crud


@dataclass(frozen=True, slots=True)
class MemoryCapacitySnapshot:
    active_count: int
    pending_create_count: int
    oversized_count: int
    max_active_records: int

    @property
    def occupied_count(self) -> int:
        return self.active_count + self.pending_create_count

    @property
    def is_over_limit(self) -> bool:
        return self.active_count > self.max_active_records or self.oversized_count > 0


async def load_memory_capacity_snapshot(
    db: AsyncSession,
    uid: str,
    max_active_records: int,
) -> MemoryCapacitySnapshot:
    active_count = await memory_record_crud.count_active(db, uid=uid)
    pending_create_count = await memory_job_crud.count_pending_create(db, uid=uid)
    oversized_count = await memory_record_crud.count_active_oversized(
        db,
        uid=uid,
        max_tokens=MEMORY_CONTENT_MAX_TOKENS,
    )
    return MemoryCapacitySnapshot(
        active_count=active_count,
        pending_create_count=pending_create_count,
        oversized_count=oversized_count,
        max_active_records=max_active_records,
    )
