from __future__ import annotations

import math
from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_DENSE_RETRIEVAL_FAILED,
    ERR_MEMORY_CAPACITY_EXCEEDED,
    ERR_MEMORY_CAPACITY_FULL,
    ERR_MEMORY_CAPACITY_PENDING,
    ERR_MEMORY_EMBEDDING_DIMENSION_INVALID,
    ERR_MEMORY_EMBEDDING_VECTOR_INVALID,
    ERR_MEMORY_FIELD_REQUIRED,
    ERR_MEMORY_FIELD_TYPE_INVALID,
    ERR_MEMORY_MIGRATION_DELTA_CONFLICT,
    ERR_MEMORY_MUTATION_PENDING,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_OVER_LIMIT,
    ERR_MEMORY_PUBLICATION_CONFLICT,
    ERR_MEMORY_RECALL_UNAVAILABLE,
    ERR_MEMORY_RECORD_NOT_FOUND,
    ERR_MEMORY_RESTORE_CONDITION_INVALID,
    ERR_MEMORY_SNAPSHOT_UID_FORBIDDEN,
    ERR_MEMORY_VERSION_CONFLICT,
    ERR_VALUE_MUST_BE_BETWEEN,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
    LOG_MEMORY_RECALL_TOUCH_FAILED,
    MEMORY_CONTENT_MAX_CHARS,
    MEMORY_MAX_ACTIVE_RECORDS,
    MEMORY_ORGANIZE_TRIGGER_RECORDS,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import (
    memory_embedding_delta_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.embedding.common import embed_texts_with_config, load_embedding_runtime_config
from app.core.i18n import t
from app.core.log import get_logger
from app.core.memory.capacity import load_memory_capacity_snapshot
from app.core.memory.errors import MemoryConflictError, MemoryNotFoundError, MemoryValidationError
from app.core.memory.identifiers import (
    build_memory_active_mutation_key,
)
from app.core.memory.normalization import (
    _normalize_dedupe_key,
    _normalize_enum,
    _normalize_uid,
    _publication_payload,
    _require_non_negative,
    _require_positive,
    _validate_commit,
    _validate_source_fields,
    build_memory_record_snapshot,
    normalize_memory_content,
    normalize_memory_record_snapshot,
)
from app.core.memory.results import (
    MemoryMutationResult,
    MemoryMutationStatus,
    MemoryRecallItem,
    MemoryRecallResult,
    MemoryRecallStatus,
)
from app.core.memory_jobs.manager import (
    MemoryJobSubmissionError,
    MemoryJobSubmissionResult,
    MemoryJobTargetBusyError,
    MemoryJobValidationError,
    memory_job_manager,
)
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRevision,
    LongTermMemorySource,
    LongTermMemoryStore,
    LongTermMemoryType,
)

_ACTIVE_MIGRATION_STATUSES = frozenset(
    {
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
    }
)
_PUBLICATION_FIELDS = (
    "memory_key",
    "content",
    "content_hash",
    "memory_type",
)

logger = get_logger(__name__)


def _enum_value(value: Any) -> Any:
    return value.value if isinstance(value, StrEnum) else value


def _same_operation(left: Any, right: Any) -> bool:
    try:
        return LongTermMemoryMutationOperation(left) == LongTermMemoryMutationOperation(right)
    except (TypeError, ValueError):
        return False


def _is_legacy_publication_payload(
    operation: LongTermMemoryMutationOperation | str | None,
    existing_payload: Any,
    requested_payload: dict[str, Any],
) -> bool:
    try:
        normalized_operation = LongTermMemoryMutationOperation(operation)
    except (TypeError, ValueError):
        return False
    if normalized_operation not in {
        LongTermMemoryMutationOperation.CREATE,
        LongTermMemoryMutationOperation.UPDATE,
    }:
        return False
    if not isinstance(existing_payload, dict) or "content_token_count" in existing_payload:
        return False
    legacy_payload = dict(requested_payload)
    legacy_payload.pop("content_token_count", None)
    return existing_payload == legacy_payload


def _is_terminal_mutation_status(value: Any) -> bool:
    try:
        return LongTermMemoryMutationStatus(value) in {
            LongTermMemoryMutationStatus.SUCCEEDED,
            LongTermMemoryMutationStatus.FAILED,
            LongTermMemoryMutationStatus.CANCELLED,
        }
    except (TypeError, ValueError):
        return False


def _same_publication(record: LongTermMemoryRecord, payload: dict[str, Any]) -> bool:
    return all(_enum_value(getattr(record, field, None)) == payload.get(field) for field in _PUBLICATION_FIELDS)


def _existing_create_publication(job: LongTermMemoryMutationJob) -> dict[str, Any] | None:
    if _same_operation(job.operation, LongTermMemoryMutationOperation.CREATE):
        return job.payload if isinstance(job.payload, dict) else None
    if _same_operation(job.operation, LongTermMemoryMutationOperation.CREATE_WITH_EVICTION):
        publication = job.payload.get("publication") if isinstance(job.payload, dict) else None
        return publication if isinstance(publication, dict) else None
    return None


def _existing_active_mutation_key(job: LongTermMemoryMutationJob) -> str | None:
    if job.active_mutation_key is not None:
        return job.active_mutation_key
    publication = _existing_create_publication(job)
    if publication is not None and "memory_key" in publication:
        return build_memory_active_mutation_key(job.uid, memory_key=publication["memory_key"])
    if job.memory_id is not None:
        return build_memory_active_mutation_key(job.uid, memory_id=job.memory_id)
    return None


def _validate_page(skip: Any, limit: Any) -> tuple[int, int]:
    skip_value = _require_non_negative(skip, field="skip", error_key=ERR_VALUE_MUST_BE_NON_NEGATIVE)
    limit_value = _require_positive(limit, field="limit")
    if limit_value > 100:
        raise MemoryValidationError(ERR_VALUE_MUST_BE_BETWEEN, params={"field": "limit", "minimum": 1, "maximum": 100})
    return skip_value, limit_value


def _validate_active_store(store: LongTermMemoryStore) -> None:
    required = (
        store.active_embedding_channel_id,
        store.active_embedding_model_id,
        store.active_embedding_dimensions,
        store.active_embedding_signature,
        store.active_collection_name,
    )
    if isinstance(store.active_embedding_revision, bool) or not isinstance(store.active_embedding_revision, int) or store.active_embedding_revision < 1 or any(value is None or value == "" for value in required):
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
    if isinstance(store.active_embedding_channel_id, bool) or not isinstance(store.active_embedding_channel_id, int) or store.active_embedding_channel_id < 1:
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
    if isinstance(store.active_embedding_dimensions, bool) or not isinstance(store.active_embedding_dimensions, int) or store.active_embedding_dimensions < 1:
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
    max_active_records = store.max_active_records
    if isinstance(max_active_records, bool) or not isinstance(max_active_records, int) or max_active_records < 1:
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
    if max_active_records > MEMORY_MAX_ACTIVE_RECORDS:
        raise MemoryConflictError(ERR_MEMORY_OVER_LIMIT)
    if store.organize_trigger_records != MEMORY_ORGANIZE_TRIGGER_RECORDS:
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)


async def _lock_active_store(db: AsyncSession, uid: str) -> LongTermMemoryStore:
    store = await memory_store_crud.lock_for_mutation(db, uid=uid, commit=False)
    if store is None:
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
    _validate_active_store(store)
    return store


async def _finish(db: AsyncSession, *, commit: bool) -> None:
    if commit:
        await db.commit()
    else:
        await db.flush()


def _accepted(submission: MemoryJobSubmissionResult) -> MemoryMutationResult:
    return MemoryMutationResult(status=MemoryMutationStatus.ACCEPTED, job=submission.job)


def _translate_submission_error(exc: MemoryJobSubmissionError) -> MemoryConflictError | MemoryValidationError:
    if isinstance(exc, MemoryJobTargetBusyError):
        return MemoryConflictError(ERR_MEMORY_MUTATION_PENDING)
    if isinstance(exc, MemoryJobValidationError):
        return MemoryConflictError(ERR_MEMORY_PUBLICATION_CONFLICT)
    return MemoryConflictError(ERR_MEMORY_PUBLICATION_CONFLICT)


async def _submit_job(db: AsyncSession, **kwargs: Any) -> MemoryJobSubmissionResult:
    try:
        return await memory_job_manager.submit(db, commit=False, **kwargs)
    except MemoryJobSubmissionError as exc:
        raise _translate_submission_error(exc) from exc


async def _accept_existing_job(
    db: AsyncSession,
    existing: LongTermMemoryMutationJob,
    *,
    fallback_active_mutation_key: str | None,
    operation: LongTermMemoryMutationOperation | str | None = None,
    payload: dict[str, Any] | None = None,
    memory_id: int | None = None,
    expected_version: int | None = None,
    source_session_id: str | None = None,
    source_profile_id: int | None = None,
    source_message_id: int | None = None,
    max_attempts: int | None = None,
    use_existing_identity: bool = True,
) -> MemoryJobSubmissionResult:
    if use_existing_identity:
        operation = existing.operation
        payload = dict(existing.payload or {})
        memory_id = existing.memory_id
        expected_version = existing.expected_version
        source_session_id = existing.source_session_id
        source_profile_id = existing.source_profile_id
        source_message_id = existing.source_message_id
        max_attempts = existing.max_attempts
    elif operation is None or payload is None or max_attempts is None:
        raise MemoryValidationError(ERR_MEMORY_FIELD_REQUIRED, params={"field": "mutation_identity"})
    requested_payload = payload
    if not use_existing_identity and _same_operation(operation, LongTermMemoryMutationOperation.CREATE) and _same_operation(existing.operation, LongTermMemoryMutationOperation.CREATE_WITH_EVICTION) and _existing_create_publication(existing) == requested_payload:
        operation = existing.operation
        payload = dict(existing.payload or {})
        memory_id = existing.memory_id
        expected_version = existing.expected_version
    if _same_operation(operation, LongTermMemoryMutationOperation.CREATE):
        memory_id = existing.memory_id
    active_key = existing.active_mutation_key or fallback_active_mutation_key
    submission_payload = payload
    if _is_legacy_publication_payload(operation, existing.payload, payload):
        submission_payload = dict(existing.payload or {})
    try:
        return await memory_job_manager.submit(
            db,
            uid=existing.uid,
            operation=operation,
            dedupe_key=existing.dedupe_key,
            payload=submission_payload,
            active_mutation_key=active_key,
            memory_id=memory_id,
            expected_version=expected_version,
            source_session_id=source_session_id,
            source_profile_id=source_profile_id,
            source_message_id=source_message_id,
            max_attempts=max_attempts,
            commit=False,
        )
    except MemoryJobValidationError as exc:
        if _is_terminal_mutation_status(existing.status) and (
            _same_operation(operation, existing.operation)
            and submission_payload == (existing.payload or {})
            and active_key == _existing_active_mutation_key(existing)
            and memory_id == existing.memory_id
            and expected_version == existing.expected_version
            and source_session_id == existing.source_session_id
            and source_profile_id == existing.source_profile_id
            and source_message_id == existing.source_message_id
            and max_attempts == existing.max_attempts
        ):
            return MemoryJobSubmissionResult(job=existing, created=False)
        raise _translate_submission_error(exc) from exc
    except MemoryJobSubmissionError as exc:
        raise _translate_submission_error(exc) from exc


def _validate_source_and_attempts(
    *,
    max_attempts: Any,
    source: Any,
    source_id: str | None,
    source_session_id: str | None,
    source_profile_id: int | None,
    source_message_id: int | None,
) -> tuple[int, LongTermMemorySource, str | None, str | None, int | None, int | None]:
    attempts = _require_positive(max_attempts, field="max_attempts", error_key=ERR_VALUE_MUST_BE_POSITIVE)
    normalized_source, normalized_source_id, normalized_session_id, normalized_profile_id, normalized_message_id = _validate_source_fields(
        source=source,
        source_id=source_id,
        source_session_id=source_session_id,
        source_profile_id=source_profile_id,
        source_message_id=source_message_id,
    )
    return attempts, normalized_source, normalized_source_id, normalized_session_id, normalized_profile_id, normalized_message_id


async def _hybrid_query_collection(
    collection_name: str,
    query_embedding: list[float],
    query: str,
    limit: int,
    error_key: str = ERR_DENSE_RETRIEVAL_FAILED,
) -> list[Any]:
    from app.core.retrieval.hybrid import hybrid_query_collection

    return await hybrid_query_collection(collection_name, query_embedding, query, limit, error_key=error_key)


async def append_memory_embedding_delta(
    db: AsyncSession,
    store: LongTermMemoryStore,
    action: LongTermMemoryEmbeddingDeltaAction | str,
    memory_id: int,
    memory_version: int,
    source_mutation_job_id: int | None,
    snapshot: dict[str, Any],
    commit: bool = False,
) -> LongTermMemoryEmbeddingDelta | None:
    _validate_commit(commit)
    _require_positive(memory_id, field="memory_id")
    _require_non_negative(memory_version, field="memory_version")
    if source_mutation_job_id is not None:
        _require_positive(source_mutation_job_id, field="source_mutation_job_id")
    normalized_action = _normalize_enum(action, LongTermMemoryEmbeddingDeltaAction, field="action")
    if not isinstance(snapshot, dict):
        raise MemoryValidationError(ERR_MEMORY_FIELD_TYPE_INVALID, params={"field": "snapshot"})
    if "uid" in snapshot:
        raise MemoryValidationError(ERR_MEMORY_SNAPSHOT_UID_FORBIDDEN)
    if store.migration_job_id is None:
        return None
    try:
        migration_status = LongTermMemoryMigrationStatus(store.migration_status)
    except (TypeError, ValueError):
        return None
    if migration_status not in _ACTIVE_MIGRATION_STATUSES:
        return None
    if isinstance(store.migration_delta_high_watermark, bool) or not isinstance(store.migration_delta_high_watermark, int) or store.migration_delta_high_watermark < 0:
        raise MemoryConflictError(ERR_MEMORY_MIGRATION_DELTA_CONFLICT)
    sequence = await memory_store_crud.reserve_migration_delta_sequence(
        db,
        uid=store.uid,
        migration_job_id=store.migration_job_id,
        expected_high_watermark=store.migration_delta_high_watermark,
        commit=False,
    )
    if sequence is None:
        raise MemoryConflictError(ERR_MEMORY_MIGRATION_DELTA_CONFLICT)
    store.migration_delta_high_watermark = sequence
    return await memory_embedding_delta_crud.create(
        db,
        uid=store.uid,
        migration_job_id=store.migration_job_id,
        sequence=sequence,
        memory_id=memory_id,
        memory_version=memory_version,
        action=normalized_action,
        source_mutation_job_id=source_mutation_job_id,
        snapshot=dict(snapshot),
        commit=commit,
    )


class LongTermMemoryService:
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

    async def recall(
        self,
        db: AsyncSession,
        uid: str,
        query: str,
        top_k: int = 5,
        candidate_k: int = 10,
        result_max_chars: int = 4000,
    ) -> MemoryRecallResult:
        normalized_uid = _normalize_uid(uid)
        normalized_query = normalize_memory_content(query)
        normalized_top_k = _require_positive(top_k, field="top_k")
        if normalized_top_k > 50:
            raise MemoryValidationError(ERR_VALUE_MUST_BE_BETWEEN, params={"field": "top_k", "minimum": 1, "maximum": 50})
        normalized_candidate_k = _require_positive(candidate_k, field="candidate_k")
        if normalized_candidate_k > 100:
            raise MemoryValidationError(ERR_VALUE_MUST_BE_BETWEEN, params={"field": "candidate_k", "minimum": 1, "maximum": 100})
        if normalized_candidate_k < normalized_top_k:
            raise MemoryValidationError(
                ERR_VALUE_MUST_BE_BETWEEN,
                params={"field": "candidate_k", "minimum": normalized_top_k, "maximum": 100},
            )
        normalized_result_max_chars = _require_positive(result_max_chars, field="result_max_chars")
        if not 256 <= normalized_result_max_chars <= MEMORY_CONTENT_MAX_CHARS:
            raise MemoryValidationError(
                ERR_VALUE_MUST_BE_BETWEEN,
                params={"field": "result_max_chars", "minimum": 256, "maximum": MEMORY_CONTENT_MAX_CHARS},
            )

        store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
        if store is None:
            return MemoryRecallResult(status=MemoryRecallStatus.NOT_CONFIGURED, error_key=ERR_MEMORY_NOT_CONFIGURED)
        try:
            _validate_active_store(store)
        except MemoryConflictError:
            return MemoryRecallResult(status=MemoryRecallStatus.NOT_CONFIGURED, error_key=ERR_MEMORY_NOT_CONFIGURED)
        if await memory_record_crud.count_active(db, uid=normalized_uid) == 0:
            return MemoryRecallResult(status=MemoryRecallStatus.EMPTY)

        active_snapshot = (
            store.active_collection_name,
            store.active_embedding_signature,
            store.active_embedding_revision,
        )
        try:
            runtime_config = await load_embedding_runtime_config(
                db,
                store.active_embedding_channel_id,
                store.active_embedding_model_id,
            )
            embeddings = await embed_texts_with_config(
                runtime_config,
                [normalized_query],
                dimensions=store.active_embedding_dimensions,
                db=db,
                release_connection=True,
            )
            if not isinstance(embeddings, list) or len(embeddings) != 1 or not isinstance(embeddings[0], list) or not embeddings[0] or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in embeddings[0]):
                await db.rollback()
                return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
            query_vector = embeddings[0]
            if len(query_vector) != store.active_embedding_dimensions:
                await db.rollback()
                return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_EMBEDDING_DIMENSION_INVALID)
            current_store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
            if (
                current_store is None
                or (
                    current_store.active_collection_name,
                    current_store.active_embedding_signature,
                    current_store.active_embedding_revision,
                )
                != active_snapshot
            ):
                await db.rollback()
                return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_RECALL_UNAVAILABLE)
            await db.commit()
            hits = await _hybrid_query_collection(
                current_store.active_collection_name,
                query_vector,
                normalized_query,
                limit=normalized_candidate_k,
            )
            latest_store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
            if (
                latest_store is None
                or (
                    latest_store.active_collection_name,
                    latest_store.active_embedding_signature,
                    latest_store.active_embedding_revision,
                )
                != active_snapshot
            ):
                await db.rollback()
                return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_RECALL_UNAVAILABLE)
        except Exception as exc:
            await db.rollback()
            logger.bind(uid=normalized_uid, error_type=type(exc).__name__).warning(t(ERR_MEMORY_RECALL_UNAVAILABLE))
            return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_RECALL_UNAVAILABLE)

        candidate_ids: list[int] = []
        valid_hits: list[tuple[Any, int, int]] = []
        for hit in hits:
            metadata = getattr(hit, "metadata", None)
            if not isinstance(metadata, dict) or metadata.get("uid") != normalized_uid:
                continue
            if isinstance(metadata.get("embedding_revision"), bool) or not isinstance(metadata.get("embedding_revision"), int) or metadata.get("embedding_revision") != active_snapshot[2]:
                continue
            hit_memory_id = metadata.get("memory_id")
            hit_version = metadata.get("version")
            if isinstance(hit_memory_id, bool) or not isinstance(hit_memory_id, int) or hit_memory_id < 1 or isinstance(hit_version, bool) or not isinstance(hit_version, int) or hit_version < 1:
                continue
            candidate_ids.append(hit_memory_id)
            valid_hits.append((hit, hit_memory_id, hit_version))

        records = await memory_record_crud.list_recallable_by_ids(db, uid=normalized_uid, memory_ids=set(candidate_ids))
        records_by_id = {record.id: record for record in records}
        items: list[MemoryRecallItem] = []
        remaining = normalized_result_max_chars
        for hit, hit_memory_id, hit_version in valid_hits:
            if len(items) >= normalized_top_k:
                break
            record = records_by_id.get(hit_memory_id)
            if record is None or record.version != hit_version or record.vector_item_id != hit.id or not isinstance(record.content, str):
                continue
            if remaining <= 0:
                break
            content = record.content
            truncated = len(content) > remaining
            output_content = content[:remaining] if truncated else content
            items.append(
                MemoryRecallItem(
                    memory_id=record.id,
                    memory_key=record.memory_key or "",
                    content=output_content,
                    memory_type=_enum_value(record.memory_type),
                    version=record.version,
                    updated_at=record.updated_at,
                    source=_enum_value(record.source),
                    dense_distance=getattr(hit, "dense_distance", None),
                    dense_rank=getattr(hit, "dense_rank", None),
                    sparse_score=getattr(hit, "sparse_score", None),
                    sparse_rank=getattr(hit, "sparse_rank", None),
                    fusion_score=getattr(hit, "fusion_score", None),
                    truncated=truncated,
                )
            )
            remaining -= len(output_content)
            if truncated:
                break
        if not items:
            return MemoryRecallResult(status=MemoryRecallStatus.EMPTY)
        recall_result = MemoryRecallResult(status=MemoryRecallStatus.OK, items=tuple(items))
        recalled_memory_ids = {item.memory_id for item in items}
        try:
            await memory_record_crud.touch_last_recalled_at(
                db,
                uid=normalized_uid,
                memory_ids=recalled_memory_ids,
                commit=True,
            )
        except Exception as exc:
            rollback_error_type: str | None = None
            try:
                await db.rollback()
            except Exception as rollback_exc:
                rollback_error_type = type(rollback_exc).__name__
            logger.bind(
                uid=normalized_uid,
                memory_ids=sorted(recalled_memory_ids),
                error_type=type(exc).__name__,
                rollback_error_type=rollback_error_type,
            ).warning(t(LOG_MEMORY_RECALL_TOUCH_FAILED))
        return recall_result


memory_service = LongTermMemoryService()


__all__ = [
    "LongTermMemoryService",
    "append_memory_embedding_delta",
    "memory_service",
]
