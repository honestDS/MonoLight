from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED,
    ERR_MEMORY_JOB_LEASE_UNAVAILABLE,
    ERR_MEMORY_JOB_OPERATION_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.i18n import t
from app.core.memory_jobs.executor import (
    MemoryJobCancelledError,
    MemoryJobDeterministicError,
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
    MemoryJobExecutionResult,
    MemoryJobLeaseLostError,
    MemoryJobRetryableError,
)
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
)
from app.providers.database.time import get_database_time
from app.providers.vector import async_delete_collection_items, async_validate_collection

_STAGED_VECTOR_KEY = "_staged_vector"
_VECTOR_CLEANUP_PAYLOAD_FIELDS = frozenset(
    {
        "source_job_id",
        "reason",
        "collection_name",
        "item_id",
    }
)
_VECTOR_CLEANUP_REASONS = frozenset({"staged", "superseded"})
_VECTOR_CLEANUP_PARENT_OPERATIONS = frozenset(
    {
        LongTermMemoryMutationOperation.CREATE,
        LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
        LongTermMemoryMutationOperation.UPDATE,
        LongTermMemoryMutationOperation.ORGANIZE_MERGE,
    }
)


@dataclass(frozen=True, slots=True)
class VectorCleanupSubmission:
    job: LongTermMemoryMutationJob
    created: bool


def _deterministic(message_key: str) -> MemoryJobDeterministicError:
    return MemoryJobDeterministicError(t(message_key))


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _operation(value: Any) -> LongTermMemoryMutationOperation:
    try:
        return LongTermMemoryMutationOperation(value)
    except (TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_OPERATION_INVALID) from exc


def _require_job_id(job: LongTermMemoryMutationJob) -> int:
    if not _is_positive_int(job.id):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return job.id


def _require_uid(job: LongTermMemoryMutationJob) -> str:
    if not _is_non_empty_string(job.uid):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return job.uid


def _require_vector_reference(collection_name: Any, item_id: Any) -> tuple[str, str]:
    if not _is_non_empty_string(collection_name) or not _is_non_empty_string(item_id):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return collection_name, item_id


def _cleanup_payload(
    *,
    source_job_id: int,
    reason: str,
    collection_name: str,
    item_id: str,
) -> dict[str, Any]:
    return {
        "source_job_id": source_job_id,
        "reason": reason,
        "collection_name": collection_name,
        "item_id": item_id,
    }


def _cleanup_dedupe_key(
    *,
    source_job_id: int,
    reason: str,
    collection_name: str,
    item_id: str,
) -> str:
    vector_digest = hashlib.sha256(f"{collection_name}:{item_id}".encode()).hexdigest()
    return f"memory-vector-cleanup:{source_job_id}:{reason}:{vector_digest}"


def _validate_source_job(source_job: LongTermMemoryMutationJob) -> tuple[int, str, int]:
    source_job_id = _require_job_id(source_job)
    uid = _require_uid(source_job)
    operation = _operation(source_job.operation)
    if operation not in _VECTOR_CLEANUP_PARENT_OPERATIONS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not _is_positive_int(source_job.max_attempts):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return source_job_id, uid, max(5, source_job.max_attempts)


def _validate_cleanup_job(
    job: LongTermMemoryMutationJob,
    *,
    expected_reason: str | None = None,
) -> dict[str, Any]:
    operation = _operation(job.operation)
    if operation != LongTermMemoryMutationOperation.VECTOR_CLEANUP:
        raise _deterministic(ERR_MEMORY_JOB_OPERATION_INVALID)
    _require_job_id(job)
    _require_uid(job)
    if not _is_positive_int(job.parent_job_id):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if job.active_mutation_key is not None or job.memory_id is not None or job.expected_version is not None:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if getattr(job, "source_session_id", None) is not None or getattr(job, "source_profile_id", None) is not None or getattr(job, "source_message_id", None) is not None:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not isinstance(job.payload, dict) or set(job.payload) != _VECTOR_CLEANUP_PAYLOAD_FIELDS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)

    payload = dict(job.payload)
    source_job_id = payload["source_job_id"]
    reason = payload["reason"]
    collection_name = payload["collection_name"]
    item_id = payload["item_id"]
    if not _is_positive_int(source_job_id) or source_job_id != job.parent_job_id:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if not isinstance(reason, str) or reason not in _VECTOR_CLEANUP_REASONS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if expected_reason is not None and reason != expected_reason:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    collection_name, item_id = _require_vector_reference(collection_name, item_id)
    return _cleanup_payload(
        source_job_id=source_job_id,
        reason=reason,
        collection_name=collection_name,
        item_id=item_id,
    )


def _cleanup_job_matches(
    job: LongTermMemoryMutationJob,
    *,
    uid: str,
    parent_job_id: int,
    dedupe_key: str,
    payload: dict[str, Any],
    max_attempts: int,
) -> bool:
    try:
        normalized_payload = _validate_cleanup_job(job)
    except MemoryJobDeterministicError:
        return False
    return job.uid == uid and job.parent_job_id == parent_job_id and job.dedupe_key == dedupe_key and normalized_payload == payload and job.max_attempts == max_attempts


async def persist_staged_vector_reference(
    context: MemoryJobExecutionContext,
    *,
    collection_name: str,
    item_id: str,
) -> None:
    collection_name, item_id = _require_vector_reference(collection_name, item_id)
    job_id = _require_job_id(context.job)
    uid = _require_uid(context.job)
    result = {
        _STAGED_VECTOR_KEY: {
            "collection_name": collection_name,
            "item_id": item_id,
        }
    }
    async with context.session_factory() as db:
        updated = await memory_job_crud.update_running_result(
            db,
            uid=uid,
            job_id=job_id,
            owner=context.worker_id,
            result=result,
        )
    if not updated:
        try:
            await context.checkpoint()
        except (MemoryJobCancelledError, MemoryJobLeaseLostError):
            raise
        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
    context.job.result = result


async def _create_vector_cleanup_job(
    db: AsyncSession,
    *,
    source_job: LongTermMemoryMutationJob,
    reason: str,
    collection_name: str,
    item_id: str,
) -> LongTermMemoryMutationJob:
    source_job_id, uid, max_attempts = _validate_source_job(source_job)
    if reason not in _VECTOR_CLEANUP_REASONS:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    collection_name, item_id = _require_vector_reference(collection_name, item_id)
    payload = _cleanup_payload(
        source_job_id=source_job_id,
        reason=reason,
        collection_name=collection_name,
        item_id=item_id,
    )
    dedupe_key = _cleanup_dedupe_key(
        source_job_id=source_job_id,
        reason=reason,
        collection_name=collection_name,
        item_id=item_id,
    )
    if len(dedupe_key) > 255:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    available_at = await get_database_time(db)
    try:
        cleanup_job, _ = await memory_job_crud.create(
            db,
            uid=uid,
            parent_job_id=source_job_id,
            operation=LongTermMemoryMutationOperation.VECTOR_CLEANUP,
            dedupe_key=dedupe_key,
            active_mutation_key=None,
            memory_id=None,
            expected_version=None,
            payload=payload,
            source_session_id=None,
            source_profile_id=None,
            source_message_id=None,
            max_attempts=max_attempts,
            available_at=available_at,
            commit=False,
        )
    except IntegrityError as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if not _cleanup_job_matches(
        cleanup_job,
        uid=uid,
        parent_job_id=source_job_id,
        dedupe_key=dedupe_key,
        payload=payload,
        max_attempts=max_attempts,
    ):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return cleanup_job


async def create_superseded_vector_cleanup_job(
    db: AsyncSession,
    *,
    source_job: LongTermMemoryMutationJob,
    collection_name: str,
    item_id: str,
) -> LongTermMemoryMutationJob:
    return await _create_vector_cleanup_job(
        db,
        source_job=source_job,
        reason="superseded",
        collection_name=collection_name,
        item_id=item_id,
    )


async def finalize_staged_vector_terminal_state(
    db: AsyncSession,
    *,
    job: LongTermMemoryMutationJob,
    status: LongTermMemoryMutationStatus,
) -> LongTermMemoryMutationJob | None:
    try:
        terminal_status = LongTermMemoryMutationStatus(status)
    except (TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if terminal_status not in {
        LongTermMemoryMutationStatus.FAILED,
        LongTermMemoryMutationStatus.CANCELLED,
    }:
        return None

    try:
        parent_operation = LongTermMemoryMutationOperation(job.operation)
    except (TypeError, ValueError):
        return None
    if parent_operation not in _VECTOR_CLEANUP_PARENT_OPERATIONS:
        return None
    _require_job_id(job)
    _require_uid(job)
    if job.result is None:
        return None
    if not isinstance(job.result, dict):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    if _STAGED_VECTOR_KEY not in job.result:
        return None
    staged_vector = job.result[_STAGED_VECTOR_KEY]
    if not isinstance(staged_vector, dict) or set(staged_vector) != {"collection_name", "item_id"}:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    collection_name, item_id = _require_vector_reference(
        staged_vector["collection_name"],
        staged_vector["item_id"],
    )
    return await _create_vector_cleanup_job(
        db,
        source_job=job,
        reason="staged",
        collection_name=collection_name,
        item_id=item_id,
    )


async def execute_vector_cleanup(
    context: MemoryJobExecutionContext,
) -> MemoryJobExecutionResult:
    claim = await context.checkpoint()
    if claim is None:
        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
    if claim.id != context.job.id or claim.uid != context.job.uid or claim.locked_by != context.worker_id:
        raise MemoryJobLeaseLostError(t(ERR_MEMORY_JOB_LEASE_UNAVAILABLE))
    payload = _validate_cleanup_job(claim)
    try:
        validation = await async_validate_collection(payload["collection_name"])
        if getattr(validation, "exists", False):
            await async_delete_collection_items(
                payload["collection_name"],
                [payload["item_id"]],
                batch_size=1,
            )
    except MemoryJobExecutionError:
        raise
    except Exception as exc:
        raise MemoryJobRetryableError(t(ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED)) from exc
    result = {
        "operation": LongTermMemoryMutationOperation.VECTOR_CLEANUP.value,
        "source_job_id": payload["source_job_id"],
        "reason": payload["reason"],
        "collection_name": payload["collection_name"],
        "item_id": payload["item_id"],
    }
    return MemoryJobExecutionResult(result=result, finalized=False)


async def retry_vector_cleanup_job(
    db: AsyncSession,
    *,
    failed_job: LongTermMemoryMutationJob,
) -> VectorCleanupSubmission:
    try:
        operation = LongTermMemoryMutationOperation(failed_job.operation)
        status = LongTermMemoryMutationStatus(failed_job.status)
    except (TypeError, ValueError) as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if operation != LongTermMemoryMutationOperation.VECTOR_CLEANUP or status != LongTermMemoryMutationStatus.FAILED:
        raise _deterministic(ERR_MEMORY_JOB_OPERATION_INVALID)

    payload = _validate_cleanup_job(failed_job)
    if not _is_positive_int(failed_job.max_attempts):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    retry_dedupe_key = f"{_cleanup_dedupe_key(source_job_id=payload['source_job_id'], reason=payload['reason'], collection_name=payload['collection_name'], item_id=payload['item_id'])}:retry:{uuid4().hex}"
    if len(retry_dedupe_key) > 255:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    available_at = await get_database_time(db)
    try:
        retry_job, created = await memory_job_crud.create(
            db,
            uid=failed_job.uid,
            parent_job_id=failed_job.parent_job_id,
            operation=LongTermMemoryMutationOperation.VECTOR_CLEANUP,
            dedupe_key=retry_dedupe_key,
            active_mutation_key=None,
            memory_id=None,
            expected_version=None,
            status=LongTermMemoryMutationStatus.PENDING,
            payload=payload,
            source_session_id=None,
            source_profile_id=None,
            source_message_id=None,
            max_attempts=failed_job.max_attempts,
            available_at=available_at,
            commit=False,
        )
    except IntegrityError as exc:
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
    if not _cleanup_job_matches(
        retry_job,
        uid=failed_job.uid,
        parent_job_id=failed_job.parent_job_id,
        dedupe_key=retry_dedupe_key,
        payload=payload,
        max_attempts=failed_job.max_attempts,
    ):
        raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID)
    return VectorCleanupSubmission(job=retry_job, created=created)


__all__ = [
    "VectorCleanupSubmission",
    "create_superseded_vector_cleanup_job",
    "execute_vector_cleanup",
    "finalize_staged_vector_terminal_state",
    "persist_staged_vector_reference",
    "retry_vector_cleanup_job",
]
