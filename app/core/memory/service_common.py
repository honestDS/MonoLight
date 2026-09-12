from __future__ import annotations

from enum import StrEnum
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_DENSE_RETRIEVAL_FAILED,
    ERR_MEMORY_FIELD_REQUIRED,
    ERR_MEMORY_MUTATION_PENDING,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_OVER_LIMIT,
    ERR_MEMORY_PUBLICATION_CONFLICT,
    ERR_VALUE_MUST_BE_BETWEEN,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
    MEMORY_MAX_ACTIVE_RECORDS,
    MEMORY_ORGANIZE_TRIGGER_RECORDS,
)
from app.core.crud.memory.store import (
    memory_store_crud,
)
from app.core.log import get_logger
from app.core.memory.errors import MemoryConflictError, MemoryValidationError
from app.core.memory.identifiers import (
    build_memory_active_mutation_key,
)
from app.core.memory.normalization import (
    _require_non_negative,
    _require_positive,
    _validate_source_fields,
)
from app.core.memory.results import (
    MemoryMutationResult,
    MemoryMutationStatus,
)
from app.core.memory_jobs.manager import (
    MemoryJobSubmissionError,
    MemoryJobSubmissionResult,
    MemoryJobTargetBusyError,
    MemoryJobValidationError,
    memory_job_manager,
)
from app.models.memory import (
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemorySource,
    LongTermMemoryStore,
)

__all__ = []

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
