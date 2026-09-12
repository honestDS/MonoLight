from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_RECORD_NOT_FOUND,
    ERR_MEMORY_RESTORE_CONDITION_INVALID,
)
from app.core.crud.memory.store import (
    memory_record_crud,
    memory_revision_crud,
)
from app.core.memory.errors import MemoryConflictError, MemoryNotFoundError
from app.core.memory.normalization import (
    _normalize_uid,
    _require_non_negative,
    _require_positive,
    _validate_commit,
)
from app.core.memory.results import (
    MemoryMutationResult,
    MemoryMutationStatus,
)
from app.core.memory_jobs.manager import (
    memory_job_manager,
)
from app.models.memory import (
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryMutationStatus,
    LongTermMemoryRevision,
)

from .service_common import (
    _finish,
    _lock_active_store,
    _validate_page,
)
from .service_embedding import append_memory_embedding_delta

__all__ = [
    "LongTermMemoryResume",
]


class LongTermMemoryResume:
    async def resume_current(
        self,
        db: AsyncSession,
        uid: str,
        memory_id: int,
        expected_version: int,
        commit: bool = True,
    ) -> MemoryMutationResult:
        try:
            _validate_commit(commit)
            normalized_uid = _normalize_uid(uid)
            normalized_memory_id = _require_positive(memory_id, field="memory_id")
            normalized_expected_version = _require_non_negative(expected_version, field="expected_version")
            store = await _lock_active_store(db, normalized_uid)
            record = await memory_record_crud.get_by_id(db, uid=normalized_uid, memory_id=normalized_memory_id)
            if record is None:
                raise MemoryNotFoundError(ERR_MEMORY_RECORD_NOT_FOUND)
            if not record.is_active or record.deleted_at is not None or record.version != normalized_expected_version or record.pending_mutation_job_id is not None or not record.suppress_recall or record.suppressed_by_job_id is None:
                raise MemoryConflictError(ERR_MEMORY_RESTORE_CONDITION_INVALID)
            suppressed_job = await memory_job_manager.get_job(
                db,
                uid=normalized_uid,
                job_id=record.suppressed_by_job_id,
            )
            if suppressed_job is None or suppressed_job.status not in {
                LongTermMemoryMutationStatus.FAILED,
                LongTermMemoryMutationStatus.CANCELLED,
            }:
                raise MemoryConflictError(ERR_MEMORY_RESTORE_CONDITION_INVALID)
            resumed = await memory_record_crud.resume_suppressed_current(
                db,
                uid=normalized_uid,
                memory_id=normalized_memory_id,
                expected_version=normalized_expected_version,
                suppressed_by_job_id=record.suppressed_by_job_id,
                commit=False,
            )
            if not resumed:
                raise MemoryConflictError(ERR_MEMORY_RESTORE_CONDITION_INVALID)
            await append_memory_embedding_delta(
                db,
                store=store,
                action=LongTermMemoryEmbeddingDeltaAction.UPSERT,
                memory_id=normalized_memory_id,
                memory_version=normalized_expected_version,
                source_mutation_job_id=record.suppressed_by_job_id,
                snapshot={
                    "version": normalized_expected_version,
                    "vector_item_id": record.vector_item_id,
                    "suppress_recall": False,
                },
                commit=False,
            )
            resumed_record = await memory_record_crud.get_by_id(db, uid=normalized_uid, memory_id=normalized_memory_id)
            await _finish(db, commit=commit)
            return MemoryMutationResult(status=MemoryMutationStatus.RESUMED, record=resumed_record)
        except Exception:
            await db.rollback()
            raise

    async def list_history(
        self,
        db: AsyncSession,
        uid: str,
        memory_id: int,
        skip: int = 0,
        limit: int = 100,
    ) -> list[LongTermMemoryRevision]:
        normalized_uid = _normalize_uid(uid)
        normalized_memory_id = _require_positive(memory_id, field="memory_id")
        normalized_skip, normalized_limit = _validate_page(skip, limit)
        record = await memory_record_crud.get_by_id(db, uid=normalized_uid, memory_id=normalized_memory_id)
        revision = await memory_revision_crud.get_by_memory_id(db, uid=normalized_uid, memory_id=normalized_memory_id)
        if record is None and revision is None:
            raise MemoryNotFoundError(ERR_MEMORY_RECORD_NOT_FOUND)
        return await memory_revision_crud.list_by_memory_id(
            db,
            uid=normalized_uid,
            memory_id=normalized_memory_id,
            skip=normalized_skip,
            limit=normalized_limit,
        )
