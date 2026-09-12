from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_CAPACITY_EXCEEDED,
    ERR_MEMORY_CAPACITY_FULL,
    ERR_MEMORY_CAPACITY_PENDING,
    ERR_MEMORY_MUTATION_PENDING,
    ERR_MEMORY_OVER_LIMIT,
    MEMORY_MAX_ACTIVE_RECORDS,
)
from app.core.crud.memory.store import (
    memory_record_crud,
)
from app.core.memory.capacity import load_memory_capacity_snapshot
from app.core.memory.errors import MemoryConflictError
from app.core.memory.identifiers import (
    build_memory_active_mutation_key,
)
from app.core.memory.normalization import (
    _normalize_dedupe_key,
    _normalize_uid,
    _publication_payload,
    _validate_commit,
    build_memory_record_snapshot,
)
from app.core.memory.results import (
    MemoryMutationResult,
    MemoryMutationStatus,
)
from app.core.memory_jobs.manager import (
    MemoryJobSubmissionResult,
    memory_job_manager,
)
from app.models.memory import (
    LongTermMemoryMutationOperation,
    LongTermMemorySource,
    LongTermMemoryType,
)

from .service_common import (
    _accept_existing_job,
    _accepted,
    _enum_value,
    _finish,
    _lock_active_store,
    _same_publication,
    _submit_job,
    _validate_source_and_attempts,
)

__all__ = [
    "LongTermMemoryCreate",
]


class LongTermMemoryCreate:
    async def create(
        self,
        db: AsyncSession,
        uid: str,
        dedupe_key: str,
        content: str,
        memory_key: str,
        memory_type: LongTermMemoryType | str,
        change_evidence: str | None = None,
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
            active_key = build_memory_active_mutation_key(normalized_uid, memory_key=payload["memory_key"])
            existing_job = await memory_job_manager.get_job_by_dedupe_key(db, uid=normalized_uid, dedupe_key=normalized_dedupe_key)
            if existing_job is not None:
                submission = await _accept_existing_job(
                    db,
                    existing_job,
                    fallback_active_mutation_key=active_key,
                    operation=LongTermMemoryMutationOperation.CREATE,
                    payload=payload,
                    source_session_id=normalized_session_id,
                    source_profile_id=normalized_profile_id,
                    source_message_id=normalized_message_id,
                    max_attempts=attempts,
                    use_existing_identity=False,
                )
                await _finish(db, commit=commit)
                return _accepted(submission)

            store = await _lock_active_store(db, normalized_uid)
            existing_job = await memory_job_manager.get_job_by_dedupe_key(
                db,
                uid=normalized_uid,
                dedupe_key=normalized_dedupe_key,
            )
            if existing_job is not None:
                submission = await _accept_existing_job(
                    db,
                    existing_job,
                    fallback_active_mutation_key=active_key,
                    operation=LongTermMemoryMutationOperation.CREATE,
                    payload=payload,
                    source_session_id=normalized_session_id,
                    source_profile_id=normalized_profile_id,
                    source_message_id=normalized_message_id,
                    max_attempts=attempts,
                    use_existing_identity=False,
                )
                await _finish(db, commit=commit)
                return _accepted(submission)
            if (
                await memory_job_manager.get_job_by_active_mutation_key(
                    db,
                    uid=normalized_uid,
                    active_mutation_key=active_key,
                )
                is not None
            ):
                raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
            key_record = await memory_record_crud.get_by_key(db, uid=normalized_uid, memory_key=payload["memory_key"])
            hash_record = await memory_record_crud.get_by_content_hash(db, uid=normalized_uid, content_hash=payload["content_hash"])
            if key_record is not None and hash_record is not None and key_record.id == hash_record.id and _same_publication(key_record, payload):
                await _finish(db, commit=commit)
                return MemoryMutationResult(status=MemoryMutationStatus.UNCHANGED, record=key_record)
            if key_record is not None or hash_record is not None:
                await _finish(db, commit=commit)
                return MemoryMutationResult(status=MemoryMutationStatus.EXISTING, record=key_record or hash_record)
            capacity = await load_memory_capacity_snapshot(db, normalized_uid, store.max_active_records)
            if capacity.is_over_limit:
                raise MemoryConflictError(ERR_MEMORY_OVER_LIMIT)
            if capacity.active_count == MEMORY_MAX_ACTIVE_RECORDS:
                if capacity.pending_create_count > 0:
                    raise MemoryConflictError(ERR_MEMORY_CAPACITY_PENDING, maximum=store.max_active_records)
                candidate = await memory_record_crud.get_eviction_candidate(db, uid=normalized_uid)
                if candidate is None or candidate.id is None or not isinstance(candidate.vector_item_id, str) or not candidate.vector_item_id:
                    raise MemoryConflictError(ERR_MEMORY_CAPACITY_FULL)
                replacement_payload = {
                    "publication": dict(payload),
                    "candidate": {
                        "memory_id": candidate.id,
                        "version": candidate.version,
                        "vector_item_id": candidate.vector_item_id,
                        "record_snapshot": build_memory_record_snapshot(candidate),
                    },
                    "store": {
                        "active_embedding_channel_id": store.active_embedding_channel_id,
                        "active_embedding_model_id": store.active_embedding_model_id,
                        "active_embedding_dimensions": store.active_embedding_dimensions,
                        "active_embedding_signature": store.active_embedding_signature,
                        "active_embedding_revision": store.active_embedding_revision,
                        "active_collection_name": store.active_collection_name,
                        "max_active_records": store.max_active_records,
                        "organize_trigger_records": store.organize_trigger_records,
                        "active_count": capacity.active_count,
                        "index_revision": store.index_revision,
                        "index_status": _enum_value(store.index_status),
                        "capacity_status": _enum_value(store.capacity_status),
                    },
                }
                submission = await _submit_job(
                    db,
                    uid=normalized_uid,
                    operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
                    dedupe_key=normalized_dedupe_key,
                    payload=replacement_payload,
                    active_mutation_key=active_key,
                    source_session_id=normalized_session_id,
                    source_profile_id=normalized_profile_id,
                    source_message_id=normalized_message_id,
                    max_attempts=attempts,
                )
                if not submission.created:
                    await _finish(db, commit=commit)
                    return _accepted(submission)
                if submission.job.id is None:
                    raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
                if not await memory_record_crud.reserve_eviction_candidate(
                    db,
                    uid=normalized_uid,
                    memory_id=candidate.id,
                    version=candidate.version,
                    vector_item_id=candidate.vector_item_id,
                    job_id=submission.job.id,
                    commit=False,
                ):
                    raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
                refreshed_job = await memory_job_manager.get_job(
                    db,
                    uid=normalized_uid,
                    job_id=submission.job.id,
                )
                if refreshed_job is None:
                    raise MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
                submission = MemoryJobSubmissionResult(job=refreshed_job, created=True)
                await _finish(db, commit=commit)
                return _accepted(submission)
            if capacity.active_count == store.max_active_records:
                raise MemoryConflictError(ERR_MEMORY_CAPACITY_EXCEEDED, maximum=store.max_active_records)
            if capacity.occupied_count >= store.max_active_records:
                raise MemoryConflictError(ERR_MEMORY_CAPACITY_PENDING, maximum=store.max_active_records)
            submission = await _submit_job(
                db,
                uid=normalized_uid,
                operation=LongTermMemoryMutationOperation.CREATE,
                dedupe_key=normalized_dedupe_key,
                payload=payload,
                active_mutation_key=active_key,
                source_session_id=normalized_session_id,
                source_profile_id=normalized_profile_id,
                source_message_id=normalized_message_id,
                max_attempts=attempts,
            )
            await _finish(db, commit=commit)
            return _accepted(submission)
        except Exception:
            await db.rollback()
            raise
