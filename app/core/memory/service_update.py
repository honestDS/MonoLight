from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_MUTATION_PENDING,
    ERR_MEMORY_OVER_LIMIT,
    ERR_MEMORY_RECORD_NOT_FOUND,
    ERR_MEMORY_VERSION_CONFLICT,
)
from app.core.crud.memory.store import (
    memory_record_crud,
)
from app.core.memory.capacity import load_memory_capacity_snapshot
from app.core.memory.errors import MemoryConflictError, MemoryNotFoundError, MemoryValidationError
from app.core.memory.identifiers import (
    build_memory_active_mutation_key,
)
from app.core.memory.normalization import (
    _normalize_dedupe_key,
    _normalize_uid,
    _publication_payload,
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
    LongTermMemoryMutationOperation,
    LongTermMemorySource,
    LongTermMemoryType,
)

from .service_common import (
    _accept_existing_job,
    _accepted,
    _finish,
    _lock_active_store,
    _same_publication,
    _submit_job,
    _validate_source_and_attempts,
)
from .service_embedding import append_memory_embedding_delta

__all__ = [
    "LongTermMemoryUpdate",
]


class LongTermMemoryUpdate:
    async def update(
        self,
        db: AsyncSession,
        uid: str,
        dedupe_key: str,
        memory_id: int,
        expected_version: int,
        content: str,
        memory_key: str,
        memory_type: LongTermMemoryType | str,
        change_evidence: str | None = None,
        source: LongTermMemorySource | str = LongTermMemorySource.USER_API,
        source_id: str | None = None,
        source_session_id: str | None = None,
        source_profile_id: int | None = None,
        source_message_id: int | None = None,
        suppress_current: bool = False,
        max_attempts: int = 3,
        commit: bool = True,
    ) -> MemoryMutationResult:
        try:
            _validate_commit(commit)
            normalized_uid = _normalize_uid(uid)
            normalized_memory_id = _require_positive(memory_id, field="memory_id")
            normalized_expected_version = _require_non_negative(expected_version, field="expected_version")
            if not isinstance(suppress_current, bool):
                raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, params={"field": "suppress_current"})
            normalized_dedupe_key = _normalize_dedupe_key(dedupe_key)
            attempts, normalized_source, normalized_source_id, normalized_session_id, normalized_profile_id, normalized_message_id = _validate_source_and_attempts(
                max_attempts=max_attempts,
                source=source,
                source_id=source_id,
                source_session_id=source_session_id,
                source_profile_id=source_profile_id,
                source_message_id=source_message_id,
            )
            payload = _publication_payload(
                content=content,
                memory_key=memory_key,
                memory_type=memory_type,
                change_evidence=change_evidence,
                source=normalized_source,
                source_id=normalized_source_id,
                source_session_id=normalized_session_id,
                source_profile_id=normalized_profile_id,
                source_message_id=normalized_message_id,
            )
            payload["suppress_current"] = suppress_current
            active_key = build_memory_active_mutation_key(normalized_uid, memory_id=normalized_memory_id)
            existing_job = await memory_job_manager.get_job_by_dedupe_key(db, uid=normalized_uid, dedupe_key=normalized_dedupe_key)
            if existing_job is not None:
                submission = await _accept_existing_job(
                    db,
                    existing_job,
                    fallback_active_mutation_key=active_key,
                    operation=LongTermMemoryMutationOperation.UPDATE,
                    payload=payload,
                    memory_id=normalized_memory_id,
                    expected_version=normalized_expected_version,
                    source_session_id=normalized_session_id,
                    source_profile_id=normalized_profile_id,
                    source_message_id=normalized_message_id,
                    max_attempts=attempts,
                    use_existing_identity=False,
                )
                await _finish(db, commit=commit)
                return _accepted(submission)

            store = await _lock_active_store(db, normalized_uid)
            record = await memory_record_crud.get_by_id(db, uid=normalized_uid, memory_id=normalized_memory_id)
            if record is None:
                raise MemoryNotFoundError(ERR_MEMORY_RECORD_NOT_FOUND)
            if record.pending_mutation_job_id is not None:
                raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
            if not record.is_active or record.deleted_at is not None:
                raise MemoryConflictError(ERR_MEMORY_RECORD_NOT_FOUND)
            if record.version != normalized_expected_version:
                raise MemoryConflictError(ERR_MEMORY_VERSION_CONFLICT)
            key_record = await memory_record_crud.get_by_key(db, uid=normalized_uid, memory_key=payload["memory_key"])
            hash_record = await memory_record_crud.get_by_content_hash(db, uid=normalized_uid, content_hash=payload["content_hash"])
            if (key_record is not None and key_record.id != normalized_memory_id) or (hash_record is not None and hash_record.id != normalized_memory_id):
                await _finish(db, commit=commit)
                return MemoryMutationResult(status=MemoryMutationStatus.EXISTING, record=key_record or hash_record)
            if _same_publication(record, payload):
                await _finish(db, commit=commit)
                return MemoryMutationResult(status=MemoryMutationStatus.UNCHANGED, record=record)
            capacity = await load_memory_capacity_snapshot(db, normalized_uid, store.max_active_records)
            if capacity.is_over_limit and payload["content_token_count"] >= record.content_token_count:
                raise MemoryConflictError(ERR_MEMORY_OVER_LIMIT)
            submission = await _submit_job(
                db,
                uid=normalized_uid,
                operation=LongTermMemoryMutationOperation.UPDATE,
                dedupe_key=normalized_dedupe_key,
                payload=payload,
                active_mutation_key=active_key,
                memory_id=normalized_memory_id,
                expected_version=normalized_expected_version,
                source_session_id=normalized_session_id,
                source_profile_id=normalized_profile_id,
                source_message_id=normalized_message_id,
                max_attempts=attempts,
            )
            if submission.created and suppress_current:
                suppressed = await memory_record_crud.suppress_for_pending_mutation(
                    db,
                    uid=normalized_uid,
                    memory_id=normalized_memory_id,
                    job_id=submission.job.id,
                    expected_version=normalized_expected_version,
                    commit=False,
                )
                if not suppressed:
                    raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
                await append_memory_embedding_delta(
                    db,
                    store=store,
                    action=LongTermMemoryEmbeddingDeltaAction.SUPPRESS,
                    memory_id=normalized_memory_id,
                    memory_version=normalized_expected_version,
                    source_mutation_job_id=submission.job.id,
                    snapshot={
                        "version": normalized_expected_version,
                        "vector_item_id": record.vector_item_id,
                        "suppress_recall": True,
                    },
                    commit=False,
                )
            await _finish(db, commit=commit)
            return _accepted(submission)
        except Exception:
            await db.rollback()
            raise
