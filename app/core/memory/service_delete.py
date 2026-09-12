from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_MUTATION_PENDING,
    ERR_MEMORY_PUBLICATION_CONFLICT,
    ERR_MEMORY_RECORD_NOT_FOUND,
    ERR_MEMORY_VERSION_CONFLICT,
)
from app.core.crud.memory.store import (
    memory_record_crud,
)
from app.core.memory.errors import MemoryConflictError, MemoryNotFoundError
from app.core.memory.identifiers import (
    build_memory_active_mutation_key,
)
from app.core.memory.normalization import (
    _normalize_dedupe_key,
    _normalize_uid,
    _require_non_negative,
    _require_positive,
    _validate_commit,
    build_memory_record_snapshot,
    normalize_memory_record_snapshot,
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
    LongTermMemoryMutationOperation,
    LongTermMemorySource,
)

from .service_common import (
    _accept_existing_job,
    _accepted,
    _finish,
    _lock_active_store,
    _same_operation,
    _submit_job,
    _validate_source_and_attempts,
)
from .service_embedding import append_memory_embedding_delta

__all__ = [
    "LongTermMemoryDelete",
]


class LongTermMemoryDelete:
    async def delete(
        self,
        db: AsyncSession,
        uid: str,
        dedupe_key: str,
        memory_id: int,
        expected_version: int,
        source: LongTermMemorySource | str = LongTermMemorySource.USER_API,
        source_id: str | None = None,
        source_session_id: str | None = None,
        source_profile_id: int | None = None,
        source_message_id: int | None = None,
        max_attempts: int = 3,
        commit: bool = True,
    ) -> MemoryMutationResult:
        try:
            _validate_commit(commit)
            normalized_uid = _normalize_uid(uid)
            normalized_dedupe_key = _normalize_dedupe_key(dedupe_key)
            normalized_memory_id = _require_positive(memory_id, field="memory_id")
            normalized_expected_version = _require_non_negative(expected_version, field="expected_version")
            attempts, normalized_source, normalized_source_id, normalized_session_id, normalized_profile_id, normalized_message_id = _validate_source_and_attempts(
                max_attempts=max_attempts,
                source=source,
                source_id=source_id,
                source_session_id=source_session_id,
                source_profile_id=source_profile_id,
                source_message_id=source_message_id,
            )
            active_key = build_memory_active_mutation_key(normalized_uid, memory_id=normalized_memory_id)
            existing_job = await memory_job_manager.get_job_by_dedupe_key(db, uid=normalized_uid, dedupe_key=normalized_dedupe_key)
            if existing_job is not None:
                existing_version = existing_job.expected_version
                if isinstance(existing_version, bool) or not isinstance(existing_version, int) or existing_version < 0:
                    raise MemoryConflictError(ERR_MEMORY_PUBLICATION_CONFLICT)
                if normalized_expected_version != existing_version:
                    raise MemoryConflictError(ERR_MEMORY_PUBLICATION_CONFLICT)
                record_snapshot = normalize_memory_record_snapshot((existing_job.payload or {}).get("record_snapshot"))
                payload = {
                    "version": existing_version,
                    "source": normalized_source.value,
                    "source_id": normalized_source_id,
                    "source_session_id": normalized_session_id,
                    "source_profile_id": normalized_profile_id,
                    "source_message_id": normalized_message_id,
                    "record_snapshot": record_snapshot,
                }
                submission = await _accept_existing_job(
                    db,
                    existing_job,
                    fallback_active_mutation_key=active_key,
                    operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                    payload=payload,
                    memory_id=normalized_memory_id,
                    expected_version=existing_version,
                    source_session_id=normalized_session_id,
                    source_profile_id=normalized_profile_id,
                    source_message_id=normalized_message_id,
                    max_attempts=attempts,
                    use_existing_identity=False,
                )
                await _finish(db, commit=commit)
                return _accepted(submission)
            record = await memory_record_crud.get_by_id(db, uid=normalized_uid, memory_id=normalized_memory_id)
            if record is None:
                raise MemoryNotFoundError(ERR_MEMORY_RECORD_NOT_FOUND)
            if not record.is_active or record.deleted_at is not None:
                if record.pending_mutation_job_id is None:
                    await _finish(db, commit=commit)
                    return MemoryMutationResult(status=MemoryMutationStatus.UNCHANGED, record=record)
                pending_job = await memory_job_manager.get_job(
                    db,
                    uid=normalized_uid,
                    job_id=record.pending_mutation_job_id,
                )
                if (
                    pending_job is None
                    or pending_job.uid != normalized_uid
                    or not _same_operation(
                        pending_job.operation,
                        LongTermMemoryMutationOperation.DELETE_CLEANUP,
                    )
                ):
                    raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
                await _finish(db, commit=commit)
                return MemoryMutationResult(status=MemoryMutationStatus.UNCHANGED, record=record)
            if record.pending_mutation_job_id is not None:
                raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
            current_version = record.version
            if normalized_expected_version != current_version:
                raise MemoryConflictError(ERR_MEMORY_VERSION_CONFLICT)
            store = await _lock_active_store(db, normalized_uid)
            payload = {
                "version": current_version,
                "source": normalized_source.value,
                "source_id": normalized_source_id,
                "source_session_id": normalized_session_id,
                "source_profile_id": normalized_profile_id,
                "source_message_id": normalized_message_id,
                "record_snapshot": build_memory_record_snapshot(record),
            }
            submission = await _submit_job(
                db,
                uid=normalized_uid,
                operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                dedupe_key=normalized_dedupe_key,
                payload=payload,
                active_mutation_key=active_key,
                memory_id=normalized_memory_id,
                expected_version=current_version,
                source_session_id=normalized_session_id,
                source_profile_id=normalized_profile_id,
                source_message_id=normalized_message_id,
                max_attempts=attempts,
            )
            if submission.created:
                tombstoned = await memory_record_crud.tombstone_for_pending_cleanup(
                    db,
                    uid=normalized_uid,
                    memory_id=normalized_memory_id,
                    job_id=submission.job.id,
                    expected_version=current_version,
                    commit=False,
                )
                if not tombstoned:
                    raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
                await append_memory_embedding_delta(
                    db,
                    store=store,
                    action=LongTermMemoryEmbeddingDeltaAction.DELETE,
                    memory_id=normalized_memory_id,
                    memory_version=current_version,
                    source_mutation_job_id=submission.job.id,
                    snapshot={
                        "version": current_version,
                        "vector_item_id": record.vector_item_id,
                        "is_active": False,
                    },
                    commit=False,
                )
            await _finish(db, commit=commit)
            return _accepted(submission)
        except Exception:
            await db.rollback()
            raise
