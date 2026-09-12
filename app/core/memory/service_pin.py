from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_MUTATION_PENDING,
    ERR_MEMORY_RECORD_NOT_FOUND,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import (
    memory_record_crud,
)
from app.core.memory.errors import MemoryConflictError, MemoryNotFoundError, MemoryValidationError
from app.core.memory.normalization import (
    _normalize_uid,
    _require_positive,
    _validate_commit,
)
from app.models.memory import (
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
)

from .service_common import (
    _finish,
)

__all__ = [
    "LongTermMemoryPin",
]


class LongTermMemoryPin:
    async def set_pinned(
        self,
        db: AsyncSession,
        uid: str,
        memory_id: int,
        pinned: bool,
        commit: bool = True,
    ) -> LongTermMemoryRecord:
        try:
            _validate_commit(commit)
            normalized_uid = _normalize_uid(uid)
            normalized_memory_id = _require_positive(memory_id, field="memory_id")
            if not isinstance(pinned, bool):
                raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, params={"field": "pinned"})
            current_record = await memory_record_crud.get_by_id(
                db,
                uid=normalized_uid,
                memory_id=normalized_memory_id,
            )
            if current_record is None or not current_record.is_active or current_record.deleted_at is not None:
                raise MemoryNotFoundError(ERR_MEMORY_RECORD_NOT_FOUND)
            if current_record.pending_mutation_job_id is not None:
                pending_job = await memory_job_crud.get_by_id(
                    db,
                    uid=normalized_uid,
                    job_id=current_record.pending_mutation_job_id,
                )
                if pending_job is not None and pending_job.status in {
                    LongTermMemoryMutationStatus.PENDING,
                    LongTermMemoryMutationStatus.RUNNING,
                    LongTermMemoryMutationStatus.RETRY,
                }:
                    raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
            if current_record.pinned == pinned:
                await _finish(db, commit=commit)
                return current_record
            record = await memory_record_crud.set_pinned(
                db,
                uid=normalized_uid,
                memory_id=normalized_memory_id,
                pinned=pinned,
                commit=commit,
            )
            if record is None:
                current_record = await memory_record_crud.get_by_id(
                    db,
                    uid=normalized_uid,
                    memory_id=normalized_memory_id,
                )
                if current_record is not None and current_record.is_active and current_record.deleted_at is None and current_record.pending_mutation_job_id is not None:
                    pending_job = await memory_job_crud.get_by_id(
                        db,
                        uid=normalized_uid,
                        job_id=current_record.pending_mutation_job_id,
                    )
                    if pending_job is not None and pending_job.status in {
                        LongTermMemoryMutationStatus.PENDING,
                        LongTermMemoryMutationStatus.RUNNING,
                        LongTermMemoryMutationStatus.RETRY,
                    }:
                        raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
                raise MemoryNotFoundError(ERR_MEMORY_RECORD_NOT_FOUND)
            return record
        except Exception:
            await db.rollback()
            raise

    async def pin(
        self,
        db: AsyncSession,
        uid: str,
        memory_id: int,
        commit: bool = True,
    ) -> LongTermMemoryRecord:
        return await self.set_pinned(
            db,
            uid=uid,
            memory_id=memory_id,
            pinned=True,
            commit=commit,
        )

    async def unpin(
        self,
        db: AsyncSession,
        uid: str,
        memory_id: int,
        commit: bool = True,
    ) -> LongTermMemoryRecord:
        return await self.set_pinned(
            db,
            uid=uid,
            memory_id=memory_id,
            pinned=False,
            commit=commit,
        )
