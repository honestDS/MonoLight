from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError

from app.core.constants import (
    ERR_MEMORY_JOB_DEDUPE_CONFLICT,
    ERR_MEMORY_JOB_FIELD_INVALID,
    ERR_MEMORY_JOB_FIELD_REQUIRED,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_UNEXPECTED_FAILURE,
    MEMORY_ORGANIZE_MIN_INTERVAL_SECONDS,
)
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.database_integrity import is_unique_constraint_violation
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemorySource,
)

__all__ = [
    "MemoryJobSubmissionError",
    "MemoryJobTargetBusyError",
    "MemoryJobValidationError",
    "MemoryJobSubmissionResult",
    "is_organization_chain_job",
]

logger = get_logger(__name__)

_TARGET_OPERATIONS = frozenset(
    {
        LongTermMemoryMutationOperation.CREATE,
        LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
        LongTermMemoryMutationOperation.UPDATE,
        LongTermMemoryMutationOperation.DELETE_CLEANUP,
    }
)

_NON_TARGET_OPERATIONS = frozenset(
    {
        LongTermMemoryMutationOperation.REINDEX,
        LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
    }
)

_ORGANIZE_OPERATIONS = frozenset({LongTermMemoryMutationOperation.ORGANIZE})

_SUBMITTABLE_OPERATIONS = _TARGET_OPERATIONS | _NON_TARGET_OPERATIONS | _ORGANIZE_OPERATIONS

_ACTIVE_MUTATION_KEY_CONSTRAINT = "uq_long_term_memory_mutation_job_active_key"

_ORGANIZATION_RETRY_KEY_SEPARATOR = ":retry:"

_ORGANIZATION_RETRY_ID_LENGTH = 32

_ORGANIZATION_RETRY_KEY_MAX_LENGTH = 255


class MemoryJobSubmissionError(ValueError):
    pass


class MemoryJobTargetBusyError(MemoryJobSubmissionError):
    pass


class MemoryJobValidationError(MemoryJobSubmissionError):
    pass


class _OrganizationMergeStale(Exception):
    pass


@dataclass(frozen=True, slots=True)
class MemoryJobSubmissionResult:
    job: LongTermMemoryMutationJob
    created: bool


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def is_organization_chain_job(job: LongTermMemoryMutationJob) -> bool:
    try:
        operation = LongTermMemoryMutationOperation(job.operation)
    except (TypeError, ValueError):
        return False
    if operation in {
        LongTermMemoryMutationOperation.ORGANIZE,
        LongTermMemoryMutationOperation.ORGANIZE_MERGE,
    }:
        return True
    return operation == LongTermMemoryMutationOperation.DELETE_CLEANUP and isinstance(job.payload, dict) and job.payload.get("source") == LongTermMemorySource.AUTO_ORGANIZE.value


def _require_non_empty_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field=field))
    if not value.strip():
        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field=field))
    return value


def _validate_source_ids(
    *,
    source_session_id: str | None,
    source_profile_id: int | None,
    source_message_id: int | None,
) -> None:
    if source_session_id is not None and not isinstance(source_session_id, str):
        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="source_session_id"))
    for field, value in (
        ("source_profile_id", source_profile_id),
        ("source_message_id", source_message_id),
    ):
        if value is not None and not _is_integer(value):
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field=field))


def _is_active_mutation_key_integrity_error(exc: IntegrityError) -> bool:
    return is_unique_constraint_violation(
        exc,
        constraint_names=(_ACTIVE_MUTATION_KEY_CONSTRAINT,),
        fallback_marker_groups=(("active_mutation_key",),),
    )


def _organization_job_target_identity(
    job: LongTermMemoryMutationJob,
) -> tuple[str | None, str | None] | None:
    try:
        operation = LongTermMemoryMutationOperation(job.operation)
    except (TypeError, ValueError):
        return None
    if operation in {
        LongTermMemoryMutationOperation.CREATE,
        LongTermMemoryMutationOperation.UPDATE,
        LongTermMemoryMutationOperation.RESTORE,
    }:
        target = job.payload if isinstance(job.payload, dict) else None
    elif operation == LongTermMemoryMutationOperation.CREATE_WITH_EVICTION:
        target = job.payload.get("publication") if isinstance(job.payload, dict) else None
    elif operation == LongTermMemoryMutationOperation.ORGANIZE_MERGE:
        target = job.payload.get("target") if isinstance(job.payload, dict) else None
    else:
        return None
    if not isinstance(target, dict):
        return None
    memory_key = target.get("memory_key")
    content_hash = target.get("content_hash")
    if not isinstance(memory_key, str) or not memory_key or not isinstance(content_hash, str) or not content_hash:
        return None
    return memory_key, content_hash


def _job_matches_submission_identity(
    job: LongTermMemoryMutationJob,
    *,
    operation: LongTermMemoryMutationOperation,
    active_mutation_key: str | None,
    memory_id: int | None,
    parent_job_id: int | None,
    expected_version: int | None,
    payload: dict[str, Any],
    source_session_id: str | None,
    source_profile_id: int | None,
    source_message_id: int | None,
    max_attempts: int,
    available_at: datetime | None,
) -> bool:
    try:
        existing_operation = LongTermMemoryMutationOperation(job.operation)
    except (TypeError, ValueError):
        return False
    if existing_operation != operation:
        return False
    if job.parent_job_id != parent_job_id:
        return False
    if job.active_mutation_key != active_mutation_key and not (
        operation == LongTermMemoryMutationOperation.ORGANIZE
        and job.active_mutation_key is None
        and job.status
        in {
            LongTermMemoryMutationStatus.SUCCEEDED,
            LongTermMemoryMutationStatus.FAILED,
            LongTermMemoryMutationStatus.CANCELLED,
        }
    ):
        return False
    if job.memory_id != memory_id or job.expected_version != expected_version:
        return False
    if job.payload != payload:
        return False
    if job.source_session_id != source_session_id:
        return False
    if job.source_profile_id != source_profile_id or job.source_message_id != source_message_id:
        return False
    if job.max_attempts != max_attempts:
        return False
    return available_at is None or job.available_at == available_at


def _validate_existing_organization_job(
    job: LongTermMemoryMutationJob,
    *,
    uid: str,
    dedupe_key: str,
    snapshot_digest: str,
    policy_version: int,
    active_mutation_key: str,
    expected_trigger: str | None = None,
) -> LongTermMemoryMutationStatus:
    try:
        if job.uid != uid or job.dedupe_key != dedupe_key:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if job.parent_job_id is not None:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if LongTermMemoryMutationOperation(job.operation) != LongTermMemoryMutationOperation.ORGANIZE:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if job.memory_id is not None or job.expected_version is not None:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if job.source_session_id is not None or job.source_profile_id is not None or job.source_message_id is not None:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        status = LongTermMemoryMutationStatus(job.status)
        if status in {
            LongTermMemoryMutationStatus.PENDING,
            LongTermMemoryMutationStatus.RUNNING,
            LongTermMemoryMutationStatus.RETRY,
        }:
            expected_active_mutation_key = active_mutation_key
        elif status in {
            LongTermMemoryMutationStatus.SUCCEEDED,
            LongTermMemoryMutationStatus.FAILED,
            LongTermMemoryMutationStatus.CANCELLED,
        }:
            expected_active_mutation_key = None
        else:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if job.active_mutation_key != expected_active_mutation_key:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        from app.core.memory.organization import restore_organization_execution_payload

        restored = restore_organization_execution_payload(job.payload)
        if restored.snapshot.digest != snapshot_digest or restored.snapshot.policy_version != policy_version:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if expected_trigger is not None and restored.trigger != expected_trigger:
            raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        return status
    except Exception as exc:
        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_DEDUPE_CONFLICT)) from exc


def _organization_interval_elapsed(last_run_at: datetime | None, now: datetime) -> bool:
    if last_run_at is None:
        return True

    def as_utc_naive(value: datetime) -> datetime:
        if value.tzinfo is not None:
            return value.astimezone(UTC).replace(tzinfo=None)
        return value

    return as_utc_naive(now) - as_utc_naive(last_run_at) >= timedelta(seconds=MEMORY_ORGANIZE_MIN_INTERVAL_SECONDS)


def _build_organization_retry_dedupe_key(dedupe_key: str) -> str:
    retry_suffix = f"{_ORGANIZATION_RETRY_KEY_SEPARATOR}{uuid4().hex}"
    prefix_length = _ORGANIZATION_RETRY_KEY_MAX_LENGTH - len(retry_suffix)
    return f"{dedupe_key[:prefix_length]}{retry_suffix}"


def _organization_retry_prefix(stable_dedupe_key: str) -> str:
    retry_suffix_length = len(_ORGANIZATION_RETRY_KEY_SEPARATOR) + _ORGANIZATION_RETRY_ID_LENGTH
    return stable_dedupe_key[: _ORGANIZATION_RETRY_KEY_MAX_LENGTH - retry_suffix_length]


def _organization_retry_key_claims_stable_key(
    dedupe_key: str,
    *,
    stable_dedupe_key: str,
) -> bool:
    if not isinstance(dedupe_key, str) or not isinstance(stable_dedupe_key, str):
        return False
    return dedupe_key.startswith(f"{_organization_retry_prefix(stable_dedupe_key)}{_ORGANIZATION_RETRY_KEY_SEPARATOR}")


def _is_organization_retry_dedupe_key(
    dedupe_key: str,
    *,
    stable_dedupe_key: str,
) -> bool:
    if not isinstance(dedupe_key, str) or not isinstance(stable_dedupe_key, str):
        return False
    expected_prefix = _organization_retry_prefix(stable_dedupe_key)
    retry_suffix = dedupe_key[len(expected_prefix) :]
    retry_id = retry_suffix[len(_ORGANIZATION_RETRY_KEY_SEPARATOR) :]
    return (
        len(dedupe_key) <= _ORGANIZATION_RETRY_KEY_MAX_LENGTH
        and dedupe_key.startswith(expected_prefix)
        and len(retry_suffix) == len(_ORGANIZATION_RETRY_KEY_SEPARATOR) + _ORGANIZATION_RETRY_ID_LENGTH
        and retry_suffix[: len(_ORGANIZATION_RETRY_KEY_SEPARATOR)] == _ORGANIZATION_RETRY_KEY_SEPARATOR
        and len(retry_id) == _ORGANIZATION_RETRY_ID_LENGTH
        and all(character in "0123456789abcdef" for character in retry_id)
    )


def _validate_existing_organization_retry_job(
    job: LongTermMemoryMutationJob,
    *,
    uid: str,
    stable_dedupe_key: str,
    snapshot_digest: str,
    policy_version: int,
    active_mutation_key: str,
) -> LongTermMemoryMutationStatus:
    if not _is_organization_retry_dedupe_key(job.dedupe_key, stable_dedupe_key=stable_dedupe_key):
        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_DEDUPE_CONFLICT))
    return _validate_existing_organization_job(
        job,
        uid=uid,
        dedupe_key=job.dedupe_key,
        snapshot_digest=snapshot_digest,
        policy_version=policy_version,
        active_mutation_key=active_mutation_key,
        expected_trigger="auto",
    )


def _safe_auto_organization_error(exc: Exception) -> tuple[str, str]:
    if isinstance(exc, BaseBusinessException):
        try:
            return exc.message, exc.render_message()
        except Exception:
            pass
    if isinstance(exc, MemoryJobSubmissionError):
        return f"MEMORY_JOB_SUBMISSION_{type(exc).__name__}", str(exc)
    return ERR_MEMORY_JOB_UNEXPECTED_FAILURE, t(ERR_MEMORY_JOB_UNEXPECTED_FAILURE)
