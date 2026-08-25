import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_ACTIVE_TARGET_BUSY,
    ERR_MEMORY_JOB_CANCELLATION_REQUESTED,
    ERR_MEMORY_JOB_CREATE_VERSION_FORBIDDEN,
    ERR_MEMORY_JOB_DEDUPE_CONFLICT,
    ERR_MEMORY_JOB_FIELD_INVALID,
    ERR_MEMORY_JOB_FIELD_REQUIRED,
    ERR_MEMORY_JOB_NON_TARGET_FIELDS_FORBIDDEN,
    ERR_MEMORY_JOB_OPERATION_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_UID_FORBIDDEN,
    ERR_MEMORY_JOB_TARGET_BUSY,
    ERR_MEMORY_JOB_UNEXPECTED_FAILURE,
    ERR_MEMORY_MAINTENANCE_STATE_CONFLICT,
    ERR_MEMORY_MIGRATION_CANNOT_CANCEL_AFTER_SWITCHING,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
    LOG_MEMORY_AUTO_ORGANIZATION_SUBMISSION_FAILED,
    MEMORY_ORGANIZE_MIN_INTERVAL_SECONDS,
)
from app.core.crud.channel import channel_crud
from app.core.crud.memory import memory_record_crud, memory_store_crud
from app.core.crud.memory_job import MemoryJobCancelResult, memory_job_crud
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.memory_jobs.executor import SessionFactory
from app.core.memory_jobs.maintenance_lifecycle import (
    finalize_maintenance_terminal_state,
    mark_cancelled_target_cleanup_failure,
)
from app.models.memory import (
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
    LongTermMemoryRecordIndexStatus,
    LongTermMemorySource,
    LongTermMemoryStore,
)
from app.providers.database.time import get_database_time

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
    original = getattr(exc, "orig", None)
    constraint_name = str(getattr(original, "constraint_name", None) or getattr(exc, "constraint_name", None) or "").lower()
    detail = " ".join(part.lower() for part in (str(original or ""), str(exc)))
    if _ACTIVE_MUTATION_KEY_CONSTRAINT in constraint_name or _ACTIVE_MUTATION_KEY_CONSTRAINT in detail:
        return True
    return "active_mutation_key" in detail and "unique" in detail


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


class MemoryJobManager:
    async def _lock_organization_store(
        self,
        db: AsyncSession,
        *,
        uid: str,
    ) -> LongTermMemoryStore:
        from app.core.memory.errors import MemoryConflictError

        snapshot_store = await memory_store_crud.get_snapshot_by_uid(db, uid=uid)
        if snapshot_store is None:
            raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
        snapshot_channel_id = snapshot_store.organization_channel_id
        if _is_integer(snapshot_channel_id) and snapshot_channel_id > 0:
            await channel_crud.lock_for_mutation(
                db,
                channel_id=snapshot_channel_id,
                commit=False,
            )
        store = await memory_store_crud.lock_for_mutation(db, uid=uid, commit=False)
        if store is None:
            raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
        if store.organization_channel_id != snapshot_channel_id:
            raise MemoryConflictError(ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED)
        return store

    async def submit(
        self,
        db: AsyncSession,
        *,
        uid: str,
        operation: LongTermMemoryMutationOperation | str,
        dedupe_key: str,
        payload: dict[str, Any],
        active_mutation_key: str | None = None,
        memory_id: int | None = None,
        parent_job_id: int | None = None,
        expected_version: int | None = None,
        source_session_id: str | None = None,
        source_profile_id: int | None = None,
        source_message_id: int | None = None,
        max_attempts: int = 3,
        available_at: datetime | None = None,
        commit: bool = True,
    ) -> MemoryJobSubmissionResult:
        try:
            uid = _require_non_empty_string(uid, field="uid")
            dedupe_key = _require_non_empty_string(dedupe_key, field="dedupe_key")
            if not isinstance(payload, dict):
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="payload"))
            if "uid" in payload:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_UID_FORBIDDEN))
            if "pinned" in payload:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            try:
                operation = LongTermMemoryMutationOperation(operation)
            except (TypeError, ValueError) as exc:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_OPERATION_INVALID)) from exc
            if operation not in _SUBMITTABLE_OPERATIONS:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_OPERATION_INVALID))
            if not _is_integer(max_attempts):
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="max_attempts"))
            if max_attempts < 1:
                raise MemoryJobValidationError(t(ERR_VALUE_MUST_BE_POSITIVE, field="max_attempts"))
            if memory_id is not None and not _is_integer(memory_id):
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="memory_id"))
            if memory_id is not None and memory_id <= 0:
                raise MemoryJobValidationError(t(ERR_VALUE_MUST_BE_POSITIVE, field="memory_id"))
            if parent_job_id is not None and not _is_integer(parent_job_id):
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="parent_job_id"))
            if parent_job_id is not None and parent_job_id <= 0:
                raise MemoryJobValidationError(t(ERR_VALUE_MUST_BE_POSITIVE, field="parent_job_id"))
            if expected_version is not None and not _is_integer(expected_version):
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="expected_version"))
            if expected_version is not None and expected_version < 0:
                raise MemoryJobValidationError(t(ERR_VALUE_MUST_BE_NON_NEGATIVE, field="expected_version"))
            if available_at is not None and not isinstance(available_at, datetime):
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="available_at"))
            requested_available_at = available_at
            _validate_source_ids(
                source_session_id=source_session_id,
                source_profile_id=source_profile_id,
                source_message_id=source_message_id,
            )

            is_target_operation = operation in _TARGET_OPERATIONS
            if is_target_operation:
                if active_mutation_key is None:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="active_mutation_key"))
                _require_non_empty_string(active_mutation_key, field="active_mutation_key")
                if (
                    operation
                    in {
                        LongTermMemoryMutationOperation.UPDATE,
                        LongTermMemoryMutationOperation.DELETE_CLEANUP,
                    }
                    and memory_id is None
                ):
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="memory_id"))
                if operation == LongTermMemoryMutationOperation.UPDATE and expected_version is None:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="expected_version"))
                if (
                    operation
                    in {
                        LongTermMemoryMutationOperation.CREATE,
                        LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
                    }
                    and expected_version is not None
                ):
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_CREATE_VERSION_FORBIDDEN))
            elif operation in _ORGANIZE_OPERATIONS:
                if active_mutation_key is None:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="active_mutation_key"))
                _require_non_empty_string(active_mutation_key, field="active_mutation_key")
                from app.core.memory.identifiers import build_memory_organization_active_mutation_key

                if active_mutation_key != build_memory_organization_active_mutation_key(uid):
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="active_mutation_key"))
                if memory_id is not None or expected_version is not None:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_NON_TARGET_FIELDS_FORBIDDEN))
                if source_session_id is not None or source_profile_id is not None or source_message_id is not None:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_NON_TARGET_FIELDS_FORBIDDEN))
            elif operation in _NON_TARGET_OPERATIONS:
                if active_mutation_key is not None or memory_id is not None or expected_version is not None:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_NON_TARGET_FIELDS_FORBIDDEN))

            initial_available_at = available_at if available_at is not None else await get_database_time(db)
            values: dict[str, Any] = {
                "operation": operation,
                "dedupe_key": dedupe_key,
                "active_mutation_key": active_mutation_key,
                "memory_id": memory_id,
                "parent_job_id": parent_job_id,
                "expected_version": expected_version,
                "payload": payload,
                "source_session_id": source_session_id,
                "source_profile_id": source_profile_id,
                "source_message_id": source_message_id,
                "max_attempts": max_attempts,
                "available_at": initial_available_at,
            }

            try:
                job, created = await memory_job_crud.create(
                    db,
                    uid=uid,
                    commit=False,
                    **values,
                )
            except IntegrityError as exc:
                if _is_active_mutation_key_integrity_error(exc):
                    raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_ACTIVE_TARGET_BUSY)) from exc
                raise
            if not created:
                if not _job_matches_submission_identity(
                    job,
                    operation=operation,
                    active_mutation_key=active_mutation_key,
                    memory_id=memory_id,
                    parent_job_id=parent_job_id,
                    expected_version=expected_version,
                    payload=payload,
                    source_session_id=source_session_id,
                    source_profile_id=source_profile_id,
                    source_message_id=source_message_id,
                    max_attempts=max_attempts,
                    available_at=requested_available_at,
                ):
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_DEDUPE_CONFLICT))
                if commit:
                    await db.commit()
                    await db.refresh(job)
                return MemoryJobSubmissionResult(job=job, created=False)

            if (
                operation
                in {
                    LongTermMemoryMutationOperation.UPDATE,
                    LongTermMemoryMutationOperation.DELETE_CLEANUP,
                }
                and memory_id is not None
            ):
                reserved = await memory_record_crud.reserve_pending_mutation(
                    db,
                    uid=uid,
                    memory_id=memory_id,
                    job_id=job.id,
                    expected_version=expected_version,
                    commit=False,
                )
                if not reserved:
                    raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY))

            if commit:
                await db.commit()
                await db.refresh(job)
            return MemoryJobSubmissionResult(job=job, created=True)
        except Exception:
            if commit:
                await db.rollback()
            raise

    async def _submit_organization_locked(
        self,
        db: AsyncSession,
        *,
        store: LongTermMemoryStore,
        uid: str,
        trigger: str,
        caller_dedupe_key: str | None,
    ) -> MemoryJobSubmissionResult:
        from app.core.memory.identifiers import build_memory_organization_active_mutation_key
        from app.core.memory.organization import (
            MemoryOrganizationContextExceededError,
            build_organization_dedupe_key,
            build_organization_execution_request,
            build_organization_job_payload,
            build_organization_snapshot,
            load_organization_model_config_for_store,
        )

        records = await memory_record_crud.list_for_organization(db, uid=uid)
        snapshot = build_organization_snapshot(
            records,
            active_embedding_revision=store.active_embedding_revision,
            index_revision=store.index_revision,
            policy_version=store.organization_policy_version,
        )
        final_dedupe_key = build_organization_dedupe_key(
            uid,
            snapshot_digest=snapshot.digest,
            policy_version=store.organization_policy_version,
            caller_dedupe_key=caller_dedupe_key,
        )
        active_mutation_key = build_memory_organization_active_mutation_key(uid)
        existing_job = await memory_job_crud.get_by_dedupe_key(
            db,
            uid=uid,
            dedupe_key=final_dedupe_key,
        )
        if existing_job is not None:
            existing_status = _validate_existing_organization_job(
                existing_job,
                uid=uid,
                dedupe_key=final_dedupe_key,
                snapshot_digest=snapshot.digest,
                policy_version=store.organization_policy_version,
                active_mutation_key=active_mutation_key,
            )
            if trigger != "auto" or existing_status not in {
                LongTermMemoryMutationStatus.FAILED,
                LongTermMemoryMutationStatus.CANCELLED,
            }:
                return MemoryJobSubmissionResult(job=existing_job, created=False)

            latest_job_id = store.organization_last_job_id
            if latest_job_id is not None and latest_job_id != existing_job.id:
                latest_job = await memory_job_crud.get_by_id(
                    db,
                    uid=uid,
                    job_id=latest_job_id,
                )
                if latest_job is not None and _organization_retry_key_claims_stable_key(
                    latest_job.dedupe_key,
                    stable_dedupe_key=final_dedupe_key,
                ):
                    if not _is_organization_retry_dedupe_key(
                        latest_job.dedupe_key,
                        stable_dedupe_key=final_dedupe_key,
                    ):
                        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_DEDUPE_CONFLICT))
                    latest_status = _validate_existing_organization_retry_job(
                        latest_job,
                        uid=uid,
                        stable_dedupe_key=final_dedupe_key,
                        snapshot_digest=snapshot.digest,
                        policy_version=store.organization_policy_version,
                        active_mutation_key=active_mutation_key,
                    )
                    if latest_status not in {
                        LongTermMemoryMutationStatus.FAILED,
                        LongTermMemoryMutationStatus.CANCELLED,
                    }:
                        return MemoryJobSubmissionResult(job=latest_job, created=False)
            final_dedupe_key = _build_organization_retry_dedupe_key(final_dedupe_key)

        organization_model = await load_organization_model_config_for_store(
            db,
            store=store,
            snapshot_count=snapshot.count,
        )
        payload = build_organization_job_payload(snapshot, organization_model, trigger=trigger)
        request = build_organization_execution_request(payload)
        if request.budget.exceeds_hard_window:
            raise MemoryOrganizationContextExceededError(request.budget)
        return await self.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
            dedupe_key=final_dedupe_key,
            payload=payload,
            active_mutation_key=active_mutation_key,
            commit=False,
        )

    async def submit_organization(
        self,
        db: AsyncSession,
        *,
        uid: str,
        dedupe_key: str | None = None,
        commit: bool = True,
    ) -> MemoryJobSubmissionResult:
        from app.core.memory.normalization import _normalize_dedupe_key, _normalize_uid, _validate_commit
        from app.core.memory.organization import validate_organization_submission_store

        normalized_commit = _validate_commit(commit)
        try:
            normalized_uid = _normalize_uid(uid)
            normalized_dedupe_key = _normalize_dedupe_key(dedupe_key) if dedupe_key is not None else None
            store = await self._lock_organization_store(db, uid=normalized_uid)
            validate_organization_submission_store(store)
            submission = await self._submit_organization_locked(
                db,
                store=store,
                uid=normalized_uid,
                trigger="manual",
                caller_dedupe_key=normalized_dedupe_key,
            )
            if normalized_commit:
                await db.commit()
                await db.refresh(submission.job)
            else:
                await db.flush()
            return submission
        except Exception:
            if normalized_commit:
                await db.rollback()
            raise

    async def submit_auto_organization(
        self,
        db: AsyncSession,
        *,
        uid: str,
        commit: bool = True,
    ) -> MemoryJobSubmissionResult | None:
        from app.core.memory.errors import MemoryConflictError
        from app.core.memory.identifiers import build_memory_organization_active_mutation_key
        from app.core.memory.normalization import _normalize_uid, _validate_commit
        from app.core.memory.organization import validate_organization_submission_store

        normalized_commit = _validate_commit(commit)

        async def skip() -> None:
            if normalized_commit:
                await db.commit()
            else:
                await db.flush()

        try:
            normalized_uid = _normalize_uid(uid)
            store = await self._lock_organization_store(db, uid=normalized_uid)
            if store.auto_organize_enabled is not True:
                await skip()
                return None
            try:
                validate_organization_submission_store(store)
            except MemoryConflictError as exc:
                if exc.message == ERR_MEMORY_MAINTENANCE_STATE_CONFLICT:
                    await skip()
                    return None
                raise

            active_count = await memory_record_crud.count_active(db, uid=normalized_uid)
            if active_count < store.organize_trigger_records:
                await skip()
                return None

            now = await get_database_time(db)
            if not _organization_interval_elapsed(store.organization_last_run_at, now):
                await skip()
                return None

            active_job = await memory_job_crud.get_by_active_mutation_key(
                db,
                uid=normalized_uid,
                active_mutation_key=build_memory_organization_active_mutation_key(normalized_uid),
            )
            if active_job is not None:
                await skip()
                return None

            submission = await self._submit_organization_locked(
                db,
                store=store,
                uid=normalized_uid,
                trigger="auto",
                caller_dedupe_key=None,
            )
            if not submission.created:
                try:
                    submission_status = LongTermMemoryMutationStatus(submission.job.status)
                except (TypeError, ValueError) as exc:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
                if submission_status != LongTermMemoryMutationStatus.SUCCEEDED:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            job_id = submission.job.id
            if isinstance(job_id, bool) or not isinstance(job_id, int) or job_id < 1:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            updated_store = await memory_store_crud.update_by_uid(
                db,
                uid=normalized_uid,
                organization_last_job_id=job_id,
                organization_last_run_at=now,
                organization_error=None,
                commit=False,
            )
            if updated_store is None:
                raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
            if normalized_commit:
                await db.commit()
                await db.refresh(submission.job)
            else:
                await db.flush()
            return submission
        except Exception:
            if normalized_commit:
                await db.rollback()
            raise

    async def _create_eviction_cleanup_job(
        self,
        db: AsyncSession,
        *,
        replacement_job: LongTermMemoryMutationJob,
        commit: bool = False,
    ) -> LongTermMemoryMutationJob:
        from app.core.memory.errors import MemoryValidationError
        from app.core.memory.identifiers import build_memory_active_mutation_key
        from app.core.memory.normalization import normalize_memory_record_snapshot

        try:
            operation = LongTermMemoryMutationOperation(replacement_job.operation)
        except (TypeError, ValueError) as exc:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
        if operation != LongTermMemoryMutationOperation.CREATE_WITH_EVICTION or replacement_job.id is None:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_OPERATION_INVALID))
        if not isinstance(replacement_job.payload, dict):
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        payload = replacement_job.payload
        publication = payload.get("publication")
        candidate = payload.get("candidate")
        if set(payload) != {"publication", "candidate", "store"} or not isinstance(publication, dict) or not isinstance(candidate, dict):
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        candidate_memory_id = candidate.get("memory_id")
        candidate_version = candidate.get("version")
        candidate_vector_item_id = candidate.get("vector_item_id")
        try:
            candidate_snapshot = normalize_memory_record_snapshot(candidate["record_snapshot"])
        except (KeyError, TypeError, ValueError, MemoryValidationError) as exc:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
        if not _is_integer(candidate_memory_id) or candidate_memory_id < 1 or not _is_integer(candidate_version) or candidate_version < 1 or not isinstance(candidate_vector_item_id, str) or not candidate_vector_item_id or candidate_snapshot["version"] != candidate_version:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        source_fields = (
            "source",
            "source_id",
            "source_session_id",
            "source_profile_id",
            "source_message_id",
        )
        for field in source_fields:
            if field not in publication:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if publication["source_session_id"] != replacement_job.source_session_id or publication["source_profile_id"] != replacement_job.source_profile_id or publication["source_message_id"] != replacement_job.source_message_id:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        cleanup_payload = {
            "version": candidate_version,
            "source": publication["source"],
            "source_id": publication["source_id"],
            "source_session_id": publication["source_session_id"],
            "source_profile_id": publication["source_profile_id"],
            "source_message_id": publication["source_message_id"],
            "record_snapshot": candidate_snapshot,
        }
        cleanup_dedupe_key = f"memory-eviction-cleanup:{replacement_job.id}:{candidate_memory_id}:{candidate_version}"
        cleanup_active_key = build_memory_active_mutation_key(
            replacement_job.uid,
            memory_id=candidate_memory_id,
        )
        cleanup_available_at = await get_database_time(db)
        try:
            cleanup_job, created = await memory_job_crud.create(
                db,
                uid=replacement_job.uid,
                operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                dedupe_key=cleanup_dedupe_key,
                active_mutation_key=cleanup_active_key,
                memory_id=candidate_memory_id,
                expected_version=candidate_version,
                payload=cleanup_payload,
                source_session_id=publication["source_session_id"],
                source_profile_id=publication["source_profile_id"],
                source_message_id=publication["source_message_id"],
                max_attempts=replacement_job.max_attempts,
                available_at=cleanup_available_at,
                commit=False,
            )
        except IntegrityError as exc:
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY)) from exc

        if not created and (
            cleanup_job.operation != LongTermMemoryMutationOperation.DELETE_CLEANUP
            or cleanup_job.active_mutation_key != cleanup_active_key
            or cleanup_job.memory_id != candidate_memory_id
            or cleanup_job.expected_version != candidate_version
            or cleanup_job.payload != cleanup_payload
            or cleanup_job.source_session_id != publication["source_session_id"]
            or cleanup_job.source_profile_id != publication["source_profile_id"]
            or cleanup_job.source_message_id != publication["source_message_id"]
            or cleanup_job.max_attempts != replacement_job.max_attempts
        ):
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY))
        if cleanup_job.id is None:
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY))

        transferred = await memory_record_crud.transfer_eviction_candidate_to_cleanup(
            db,
            uid=replacement_job.uid,
            memory_id=candidate_memory_id,
            version=candidate_version,
            vector_item_id=candidate_vector_item_id,
            replacement_job_id=replacement_job.id,
            cleanup_job_id=cleanup_job.id,
            commit=False,
        )
        if not transferred:
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY))

        if commit:
            await db.commit()
            await db.refresh(cleanup_job)
        return cleanup_job

    async def create_eviction_cleanup_job(
        self,
        db: AsyncSession,
        *,
        replacement_job: LongTermMemoryMutationJob,
        commit: bool = False,
    ) -> LongTermMemoryMutationJob:
        return await self._create_eviction_cleanup_job(
            db,
            replacement_job=replacement_job,
            commit=commit,
        )

    async def has_unfinished_target_identity(
        self,
        db: AsyncSession,
        *,
        uid: str,
        memory_key: str,
        content_hash: str,
        exclude_job_id: int | None = None,
    ) -> bool:
        unfinished_jobs = await memory_job_crud.list_unfinished_by_uid(db, uid=uid)
        for job in unfinished_jobs:
            if exclude_job_id is not None and job.id == exclude_job_id:
                continue
            identity = _organization_job_target_identity(job)
            if identity is not None and (identity[0] == memory_key or identity[1] == content_hash):
                return True
        return False

    async def create_organization_cleanup_job(
        self,
        db: AsyncSession,
        *,
        merge_job: LongTermMemoryMutationJob,
        memory_id: int,
        version: int,
        vector_item_id: str,
        record_snapshot: dict[str, Any],
        commit: bool = False,
    ) -> LongTermMemoryMutationJob:
        from app.core.memory.errors import MemoryValidationError
        from app.core.memory.identifiers import build_memory_active_mutation_key
        from app.core.memory.normalization import normalize_memory_record_snapshot

        merge_id = merge_job.id
        organization_parent_id = merge_job.parent_job_id
        try:
            merge_operation = LongTermMemoryMutationOperation(merge_job.operation)
        except (TypeError, ValueError) as exc:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
        if (
            not _is_integer(merge_id)
            or merge_id < 1
            or not _is_integer(organization_parent_id)
            or organization_parent_id < 1
            or merge_operation != LongTermMemoryMutationOperation.ORGANIZE_MERGE
            or merge_job.source_session_id is not None
            or merge_job.source_profile_id is not None
            or merge_job.source_message_id is not None
        ):
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if not _is_integer(memory_id) or memory_id < 1 or not _is_integer(version) or version < 1:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if not isinstance(vector_item_id, str) or not vector_item_id.strip():
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        try:
            normalized_snapshot = normalize_memory_record_snapshot(record_snapshot)
        except (MemoryValidationError, KeyError, TypeError, ValueError) as exc:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
        if normalized_snapshot["version"] != version:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        cleanup_payload = {
            "version": version,
            "source": LongTermMemorySource.AUTO_ORGANIZE.value,
            "source_id": None,
            "source_session_id": None,
            "source_profile_id": None,
            "source_message_id": None,
            "record_snapshot": normalized_snapshot,
            "organization_parent_job_id": organization_parent_id,
            "organization_merge_job_id": merge_id,
        }
        cleanup_dedupe_key = f"memory-organize-cleanup:{merge_id}:{memory_id}:{version}"
        cleanup_active_key = build_memory_active_mutation_key(merge_job.uid, memory_id=memory_id)
        cleanup_available_at = await get_database_time(db)
        try:
            cleanup_job, created = await memory_job_crud.create(
                db,
                uid=merge_job.uid,
                parent_job_id=merge_id,
                operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                dedupe_key=cleanup_dedupe_key,
                active_mutation_key=cleanup_active_key,
                memory_id=memory_id,
                expected_version=version,
                payload=cleanup_payload,
                source_session_id=None,
                source_profile_id=None,
                source_message_id=None,
                max_attempts=merge_job.max_attempts,
                available_at=cleanup_available_at,
                commit=False,
            )
        except IntegrityError as exc:
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY)) from exc

        if not created and (
            cleanup_job.uid != merge_job.uid
            or cleanup_job.parent_job_id != merge_id
            or cleanup_job.operation != LongTermMemoryMutationOperation.DELETE_CLEANUP
            or cleanup_job.dedupe_key != cleanup_dedupe_key
            or cleanup_job.active_mutation_key != cleanup_active_key
            or cleanup_job.memory_id != memory_id
            or cleanup_job.expected_version != version
            or cleanup_job.payload != cleanup_payload
            or cleanup_job.source_session_id is not None
            or cleanup_job.source_profile_id is not None
            or cleanup_job.source_message_id is not None
            or cleanup_job.max_attempts != merge_job.max_attempts
        ):
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY))
        if cleanup_job.id is None:
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY))
        if commit:
            await db.commit()
            await db.refresh(cleanup_job)
        return cleanup_job

    async def retry_delete_cleanup_job(
        self,
        db: AsyncSession,
        *,
        failed_job: LongTermMemoryMutationJob,
        commit: bool = False,
    ) -> MemoryJobSubmissionResult:
        from app.core.memory.identifiers import build_memory_active_mutation_key

        try:
            operation = LongTermMemoryMutationOperation(failed_job.operation)
            status = LongTermMemoryMutationStatus(failed_job.status)
        except (TypeError, ValueError) as exc:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
        if operation != LongTermMemoryMutationOperation.DELETE_CLEANUP or status != LongTermMemoryMutationStatus.FAILED:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_OPERATION_INVALID))
        if (
            not isinstance(failed_job.uid, str)
            or not failed_job.uid.strip()
            or not _is_integer(failed_job.id)
            or failed_job.id < 1
            or not _is_integer(failed_job.memory_id)
            or failed_job.memory_id < 1
            or not _is_integer(failed_job.expected_version)
            or failed_job.expected_version < 0
            or not _is_integer(failed_job.max_attempts)
            or failed_job.max_attempts < 1
            or not isinstance(failed_job.payload, dict)
        ):
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        payload = dict(failed_job.payload)
        if payload.get("source") == LongTermMemorySource.AUTO_ORGANIZE.value:
            merge_id = failed_job.parent_job_id
            if not _is_integer(merge_id) or merge_id < 1:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            merge_job = await memory_job_crud.get_by_id(
                db,
                uid=failed_job.uid,
                job_id=merge_id,
            )
            if merge_job is None or merge_job.uid != failed_job.uid:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            try:
                merge_operation = LongTermMemoryMutationOperation(merge_job.operation)
            except (TypeError, ValueError) as exc:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
            if merge_operation != LongTermMemoryMutationOperation.ORGANIZE_MERGE or not isinstance(merge_job.payload, dict):
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            organization_parent_id = merge_job.payload.get("parent_job_id")
            if not _is_integer(organization_parent_id) or organization_parent_id < 1 or merge_job.parent_job_id != organization_parent_id:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

            payload_merge_id = payload.get("organization_merge_job_id", merge_id)
            payload_parent_id = payload.get("organization_parent_job_id", organization_parent_id)
            if not _is_integer(payload_merge_id) or payload_merge_id < 1 or payload_merge_id != merge_id or not _is_integer(payload_parent_id) or payload_parent_id < 1 or payload_parent_id != organization_parent_id:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            payload["organization_parent_job_id"] = organization_parent_id
            payload["organization_merge_job_id"] = merge_id
        elif "organization_parent_job_id" in payload or "organization_merge_job_id" in payload:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        record = await memory_record_crud.get_by_id(
            db,
            uid=failed_job.uid,
            memory_id=failed_job.memory_id,
        )
        vector_item_id = getattr(record, "vector_item_id", None)
        cleanup_dedupe_key = f"memory-delete-cleanup-retry:{failed_job.id}:{uuid4().hex}"
        cleanup_active_key = build_memory_active_mutation_key(
            failed_job.uid,
            memory_id=failed_job.memory_id,
        )
        cleanup_available_at = await get_database_time(db)
        try:
            cleanup_job, created = await memory_job_crud.create(
                db,
                uid=failed_job.uid,
                parent_job_id=failed_job.parent_job_id,
                operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                dedupe_key=cleanup_dedupe_key,
                active_mutation_key=cleanup_active_key,
                status=LongTermMemoryMutationStatus.PENDING,
                memory_id=failed_job.memory_id,
                expected_version=failed_job.expected_version,
                payload=payload,
                source_session_id=failed_job.source_session_id,
                source_profile_id=failed_job.source_profile_id,
                source_message_id=failed_job.source_message_id,
                max_attempts=failed_job.max_attempts,
                available_at=cleanup_available_at,
                commit=False,
            )
        except IntegrityError as exc:
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY)) from exc
        if not created or cleanup_job.id is None:
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY))

        reserved = await memory_record_crud.reserve_existing_tombstone_for_cleanup(
            db,
            uid=failed_job.uid,
            memory_id=failed_job.memory_id,
            version=failed_job.expected_version,
            cleanup_job_id=cleanup_job.id,
            vector_item_id=vector_item_id,
            commit=False,
        )
        if not reserved:
            raise MemoryJobTargetBusyError(t(ERR_MEMORY_JOB_TARGET_BUSY))

        if commit:
            await db.commit()
            await db.refresh(cleanup_job)
        return MemoryJobSubmissionResult(job=cleanup_job, created=True)

    async def create_organization_merge_child(
        self,
        db: AsyncSession,
        *,
        parent_job: LongTermMemoryMutationJob,
        item: Any,
        group_index: int,
        snapshot_digest: str,
        active_embedding_revision: int,
        index_revision: int,
        policy_version: int,
        commit: bool = False,
    ) -> LongTermMemoryMutationJob | None:
        from app.core.memory.errors import MemoryConflictError, MemoryValidationError
        from app.core.memory.identifiers import (
            build_memory_active_mutation_key,
            build_memory_organization_active_mutation_key,
        )
        from app.core.memory.organization import (
            build_organization_merge_child_dedupe_key,
            build_organization_merge_child_payload,
            validate_organization_submission_store,
        )

        parent_id = parent_job.id
        if isinstance(parent_id, bool) or not isinstance(parent_id, int) or parent_id < 1:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="parent_job_id"))
        if not isinstance(parent_job.uid, str) or not parent_job.uid.strip():
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="uid"))
        try:
            parent_operation = LongTermMemoryMutationOperation(parent_job.operation)
            parent_status = LongTermMemoryMutationStatus(parent_job.status)
        except (TypeError, ValueError) as exc:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc
        if (
            parent_operation != LongTermMemoryMutationOperation.ORGANIZE
            or parent_status != LongTermMemoryMutationStatus.RUNNING
            or parent_job.parent_job_id is not None
            or parent_job.cancel_requested_at is not None
            or parent_job.memory_id is not None
            or parent_job.expected_version is not None
            or parent_job.source_session_id is not None
            or parent_job.source_profile_id is not None
            or parent_job.source_message_id is not None
            or parent_job.active_mutation_key != build_memory_organization_active_mutation_key(parent_job.uid)
        ):
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
        if not _is_integer(parent_job.max_attempts) or parent_job.max_attempts < 1:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))

        try:
            payload = build_organization_merge_child_payload(
                item,
                parent_job_id=parent_id,
                group_index=group_index,
                snapshot_digest=snapshot_digest,
                active_embedding_revision=active_embedding_revision,
                index_revision=index_revision,
                policy_version=policy_version,
            )
            primary_memory_id = payload["primary_memory_id"]
            raw_sources = payload["sources"]
            target = payload["target"]
            if not _is_integer(primary_memory_id) or primary_memory_id < 1 or not isinstance(raw_sources, list) or not isinstance(target, dict) or not isinstance(target.get("memory_key"), str) or not target["memory_key"] or not isinstance(target.get("content_hash"), str) or not target["content_hash"]:
                raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            source_by_id: dict[int, dict[str, Any]] = {}
            for source in raw_sources:
                if (
                    not isinstance(source, dict)
                    or set(source) != {"memory_id", "expected_version", "pinned"}
                    or not _is_integer(source.get("memory_id"))
                    or source["memory_id"] < 1
                    or not _is_integer(source.get("expected_version"))
                    or source["expected_version"] < 0
                    or not isinstance(source.get("pinned"), bool)
                    or source["memory_id"] in source_by_id
                ):
                    raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
                source_by_id[source["memory_id"]] = dict(source)
            if primary_memory_id not in source_by_id:
                raise ValueError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID))
            ordered_sources = [source_by_id[memory_id] for memory_id in sorted(source_by_id)]
            payload = {**payload, "sources": ordered_sources}
            expected_version = source_by_id[primary_memory_id]["expected_version"]
            active_mutation_key = build_memory_active_mutation_key(
                parent_job.uid,
                memory_id=primary_memory_id,
            )
            dedupe_key = build_organization_merge_child_dedupe_key(
                parent_job_id=parent_id,
                group_index=group_index,
                payload=payload,
            )
        except (KeyError, MemoryValidationError, StopIteration, TypeError, ValueError) as exc:
            raise MemoryJobValidationError(t(ERR_MEMORY_JOB_PAYLOAD_INVALID)) from exc

        source_pairs = tuple((source["memory_id"], source["expected_version"]) for source in payload["sources"])
        source_ids = {memory_id for memory_id, _ in source_pairs}
        child_job: LongTermMemoryMutationJob | None = None
        try:
            async with db.begin_nested():
                store = await memory_store_crud.lock_for_mutation(
                    db,
                    uid=parent_job.uid,
                    commit=False,
                )
                if store is None:
                    raise _OrganizationMergeStale()
                try:
                    validate_organization_submission_store(store)
                except MemoryConflictError as exc:
                    raise _OrganizationMergeStale() from exc
                if store.active_embedding_revision != active_embedding_revision or store.index_revision != index_revision:
                    raise _OrganizationMergeStale()

                records = await memory_record_crud.get_organization_group(
                    db,
                    uid=parent_job.uid,
                    memory_ids=source_ids,
                )
                if [record.id for record in records] != sorted(source_ids):
                    raise _OrganizationMergeStale()

                existing_child = await memory_job_crud.get_by_dedupe_key(
                    db,
                    uid=parent_job.uid,
                    dedupe_key=dedupe_key,
                )
                pending_ids: set[int | None] = set()
                for record, (_, source_version) in zip(records, source_pairs, strict=True):
                    if (
                        record.uid != parent_job.uid
                        or record.id is None
                        or record.is_active is not True
                        or record.deleted_at is not None
                        or record.index_status != LongTermMemoryRecordIndexStatus.READY
                        or record.indexed_version != record.version
                        or not isinstance(record.vector_item_id, str)
                        or not record.vector_item_id
                        or record.suppress_recall is not False
                        or record.version != source_version
                    ):
                        raise _OrganizationMergeStale()
                    pending_ids.add(record.pending_mutation_job_id)

                if existing_child is not None:
                    if (
                        existing_child.uid != parent_job.uid
                        or existing_child.parent_job_id != parent_id
                        or existing_child.operation != LongTermMemoryMutationOperation.ORGANIZE_MERGE
                        or existing_child.dedupe_key != dedupe_key
                        or existing_child.active_mutation_key != active_mutation_key
                        or existing_child.memory_id != primary_memory_id
                        or existing_child.expected_version != expected_version
                        or existing_child.payload != payload
                        or existing_child.source_session_id is not None
                        or existing_child.source_profile_id is not None
                        or existing_child.source_message_id is not None
                        or existing_child.max_attempts != parent_job.max_attempts
                    ):
                        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_DEDUPE_CONFLICT))
                    if existing_child.id is None:
                        raise _OrganizationMergeStale()
                    if pending_ids not in ({None}, {existing_child.id}):
                        raise _OrganizationMergeStale()
                    if pending_ids == {existing_child.id} or existing_child.status in {
                        LongTermMemoryMutationStatus.SUCCEEDED,
                        LongTermMemoryMutationStatus.FAILED,
                        LongTermMemoryMutationStatus.CANCELLED,
                    }:
                        child_job = existing_child
                    else:
                        child_job = existing_child
                        reserved = await memory_record_crud.reserve_organization_group(
                            db,
                            uid=parent_job.uid,
                            source_versions=source_pairs,
                            job_id=existing_child.id,
                            commit=False,
                        )
                        if not reserved:
                            raise _OrganizationMergeStale()
                else:
                    target_key = payload["target"]["memory_key"]
                    target_hash = payload["target"]["content_hash"]
                    key_record = await memory_record_crud.get_by_key(
                        db,
                        uid=parent_job.uid,
                        memory_key=target_key,
                    )
                    hash_record = await memory_record_crud.get_by_content_hash(
                        db,
                        uid=parent_job.uid,
                        content_hash=target_hash,
                    )
                    if (key_record is not None and key_record.id not in source_ids) or (hash_record is not None and hash_record.id not in source_ids):
                        raise _OrganizationMergeStale()

                    unfinished_jobs = await memory_job_crud.list_unfinished_by_uid(
                        db,
                        uid=parent_job.uid,
                    )
                    for unfinished_job in unfinished_jobs:
                        identity = _organization_job_target_identity(unfinished_job)
                        if identity is None:
                            continue
                        if identity[0] == target_key or identity[1] == target_hash:
                            raise _OrganizationMergeStale()

                    available_at = await get_database_time(db)
                    try:
                        child_job, created = await memory_job_crud.create(
                            db,
                            uid=parent_job.uid,
                            parent_job_id=parent_id,
                            operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
                            dedupe_key=dedupe_key,
                            active_mutation_key=active_mutation_key,
                            memory_id=primary_memory_id,
                            expected_version=expected_version,
                            payload=payload,
                            source_session_id=None,
                            source_profile_id=None,
                            source_message_id=None,
                            max_attempts=parent_job.max_attempts,
                            available_at=available_at,
                            commit=False,
                        )
                    except IntegrityError as exc:
                        if _is_active_mutation_key_integrity_error(exc):
                            raise _OrganizationMergeStale() from exc
                        raise

                    if not created and (
                        child_job.uid != parent_job.uid
                        or child_job.parent_job_id != parent_id
                        or child_job.operation != LongTermMemoryMutationOperation.ORGANIZE_MERGE
                        or child_job.dedupe_key != dedupe_key
                        or child_job.active_mutation_key != active_mutation_key
                        or child_job.memory_id != primary_memory_id
                        or child_job.expected_version != expected_version
                        or child_job.payload != payload
                        or child_job.source_session_id is not None
                        or child_job.source_profile_id is not None
                        or child_job.source_message_id is not None
                        or child_job.max_attempts != parent_job.max_attempts
                    ):
                        raise MemoryJobValidationError(t(ERR_MEMORY_JOB_DEDUPE_CONFLICT))
                    if child_job.id is None:
                        raise _OrganizationMergeStale()
                    reserved = await memory_record_crud.reserve_organization_group(
                        db,
                        uid=parent_job.uid,
                        source_versions=source_pairs,
                        job_id=child_job.id,
                        commit=False,
                    )
                    if not reserved:
                        raise _OrganizationMergeStale()
        except _OrganizationMergeStale:
            if commit:
                await db.commit()
            return None
        except IntegrityError as exc:
            if _is_active_mutation_key_integrity_error(exc):
                if commit:
                    await db.commit()
                return None
            raise

        if child_job is None:
            return None
        if commit:
            await db.commit()
            await db.refresh(child_job)
        return child_job

    async def get_job(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
    ) -> LongTermMemoryMutationJob | None:
        return await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)

    async def get_job_by_dedupe_key(
        self,
        db: AsyncSession,
        *,
        uid: str,
        dedupe_key: str,
    ) -> LongTermMemoryMutationJob | None:
        return await memory_job_crud.get_by_dedupe_key(db, uid=uid, dedupe_key=dedupe_key)

    async def get_job_by_active_mutation_key(
        self,
        db: AsyncSession,
        *,
        uid: str,
        active_mutation_key: str,
    ) -> LongTermMemoryMutationJob | None:
        return await memory_job_crud.get_by_active_mutation_key(db, uid=uid, active_mutation_key=active_mutation_key)

    async def list_jobs(
        self,
        db: AsyncSession,
        *,
        uid: str,
        skip: int = 0,
        limit: int = 100,
    ) -> list[LongTermMemoryMutationJob]:
        return await memory_job_crud.list_by_uid(db, uid=uid, skip=skip, limit=limit)

    async def request_cancel(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
        commit: bool = True,
    ) -> MemoryJobCancelResult:
        job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)
        if job is None or job.operation not in {
            LongTermMemoryMutationOperation.REINDEX,
            LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        }:
            cancellation = await memory_job_crud.request_cancel(db, uid=uid, job_id=job_id, commit=False)
            if cancellation.changed and cancellation.job is not None and cancellation.job.status == LongTermMemoryMutationStatus.CANCELLED:
                from app.core.memory_jobs.vector_cleanup import finalize_staged_vector_terminal_state

                await finalize_staged_vector_terminal_state(
                    db,
                    job=cancellation.job,
                    status=LongTermMemoryMutationStatus.CANCELLED,
                )
                if cancellation.job.operation == LongTermMemoryMutationOperation.ORGANIZE:
                    from app.core.channel_model_protection import finalize_pending_channel_model_deletions_for_organization_job

                    await finalize_pending_channel_model_deletions_for_organization_job(
                        db,
                        job=cancellation.job,
                    )
            if commit:
                await db.commit()
            else:
                await db.flush()
            return cancellation

        store = await memory_store_crud.lock_for_mutation(db, uid=uid, commit=False)
        if store is None:
            return await memory_job_crud.request_cancel(db, uid=uid, job_id=job_id, commit=commit)

        current = await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)
        if current is None:
            if commit:
                await db.commit()
            return MemoryJobCancelResult(job=None, accepted=False, changed=False)
        cleanup_active = (
            current.operation
            in {
                LongTermMemoryMutationOperation.REINDEX,
                LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            }
            and store.old_collection_cleanup_job_id == job_id
            and store.old_collection_cleanup_status
            in {
                LongTermMemoryOldCollectionCleanupStatus.PENDING,
                LongTermMemoryOldCollectionCleanupStatus.RUNNING,
                LongTermMemoryOldCollectionCleanupStatus.FAILED,
            }
        )
        migration_switching = False
        if current.operation == LongTermMemoryMutationOperation.EMBEDDING_MIGRATION:
            if store.migration_job_id == job_id:
                migration_switching = store.migration_status == LongTermMemoryMigrationStatus.SWITCHING

        reindex_switching = False
        if current.operation == LongTermMemoryMutationOperation.REINDEX:
            reindex_payload = current.payload
            if isinstance(reindex_payload, dict):
                reindex_progress = reindex_payload.get("progress")
                if isinstance(reindex_progress, dict):
                    reindex_switching = reindex_progress.get("phase") == "switching"

        switching = migration_switching or reindex_switching
        if cleanup_active or switching:
            if commit:
                await db.commit()
            else:
                await db.flush()
            return MemoryJobCancelResult(
                job=current,
                accepted=False,
                changed=False,
                error=t(ERR_MEMORY_MIGRATION_CANNOT_CANCEL_AFTER_SWITCHING),
            )

        cancellation = await memory_job_crud.request_cancel(db, uid=uid, job_id=job_id, commit=False)
        if cancellation.changed and cancellation.job is not None and cancellation.job.status == LongTermMemoryMutationStatus.CANCELLED:
            if (
                current.status
                in {
                    LongTermMemoryMutationStatus.PENDING,
                    LongTermMemoryMutationStatus.RETRY,
                }
                and current.attempt_count > 0
            ):
                await mark_cancelled_target_cleanup_failure(db, job=current)
            await finalize_maintenance_terminal_state(
                db,
                job=current,
                status=LongTermMemoryMutationStatus.CANCELLED,
                error=t(ERR_MEMORY_JOB_CANCELLATION_REQUESTED),
            )
        if commit:
            await db.commit()
        else:
            await db.flush()
        refreshed = await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)
        return MemoryJobCancelResult(
            job=refreshed,
            accepted=cancellation.accepted,
            changed=cancellation.changed,
            error=cancellation.error,
        )


async def best_effort_submit_auto_organization_after_publication(
    session_factory: SessionFactory,
    uid: str,
    source_job_id: int,
) -> None:
    try:
        async with session_factory() as db:
            await memory_job_manager.submit_auto_organization(db, uid=uid)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        error_key, error_text = _safe_auto_organization_error(exc)
        logger.bind(
            uid=uid,
            source_job_id=source_job_id,
            exception_type=type(exc).__name__,
            error_key=error_key,
            error_text=error_text,
        ).warning(t(LOG_MEMORY_AUTO_ORGANIZATION_SUBMISSION_FAILED))
        try:
            async with session_factory() as db:
                store = await memory_store_crud.lock_for_mutation(
                    db,
                    uid=uid,
                    commit=False,
                )
                if store is None:
                    return
                updated_store = await memory_store_crud.update_by_uid(
                    db,
                    uid=uid,
                    organization_error=error_text,
                    commit=False,
                )
                if updated_store is None:
                    return
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception as update_exc:
            logger.bind(
                uid=uid,
                source_job_id=source_job_id,
                exception_type=type(update_exc).__name__,
                error_key=error_key,
                error_text=error_text,
            ).error(t("LOG_MEMORY_JOB_DATABASE_OPERATION_FAILED"))


memory_job_manager = MemoryJobManager()


__all__ = [
    "MemoryJobCancelResult",
    "MemoryJobManager",
    "MemoryJobSubmissionError",
    "MemoryJobSubmissionResult",
    "MemoryJobTargetBusyError",
    "MemoryJobValidationError",
    "best_effort_submit_auto_organization_after_publication",
    "is_organization_chain_job",
    "memory_job_manager",
]
