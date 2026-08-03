from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_ACTIVE_TARGET_BUSY,
    ERR_MEMORY_JOB_CREATE_VERSION_FORBIDDEN,
    ERR_MEMORY_JOB_DEDUPE_CONFLICT,
    ERR_MEMORY_JOB_FIELD_INVALID,
    ERR_MEMORY_JOB_FIELD_REQUIRED,
    ERR_MEMORY_JOB_NON_TARGET_FIELDS_FORBIDDEN,
    ERR_MEMORY_JOB_OPERATION_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_UID_FORBIDDEN,
    ERR_MEMORY_JOB_TARGET_BUSY,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.crud.memory import memory_record_crud
from app.core.crud.memory_job import MemoryJobCancelResult, memory_job_crud
from app.core.i18n import t
from app.models.memory import LongTermMemoryMutationJob, LongTermMemoryMutationOperation
from app.providers.database.time import get_database_time

_TARGET_OPERATIONS = frozenset(
    {
        LongTermMemoryMutationOperation.CREATE,
        LongTermMemoryMutationOperation.UPDATE,
        LongTermMemoryMutationOperation.RESTORE,
        LongTermMemoryMutationOperation.DELETE_CLEANUP,
    }
)
_NON_TARGET_OPERATIONS = frozenset(
    {
        LongTermMemoryMutationOperation.REINDEX,
        LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        LongTermMemoryMutationOperation.EXTRACT,
    }
)
_ACTIVE_MUTATION_KEY_CONSTRAINT = "uq_long_term_memory_mutation_job_active_key"


class MemoryJobSubmissionError(ValueError):
    pass


class MemoryJobTargetBusyError(MemoryJobSubmissionError):
    pass


class MemoryJobValidationError(MemoryJobSubmissionError):
    pass


@dataclass(frozen=True, slots=True)
class MemoryJobSubmissionResult:
    job: LongTermMemoryMutationJob
    created: bool


def _is_integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


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


def _job_matches_submission_identity(
    job: LongTermMemoryMutationJob,
    *,
    operation: LongTermMemoryMutationOperation,
    active_mutation_key: str | None,
    memory_id: int | None,
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
    if job.active_mutation_key != active_mutation_key:
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


class MemoryJobManager:
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
            try:
                operation = LongTermMemoryMutationOperation(operation)
            except (TypeError, ValueError) as exc:
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_OPERATION_INVALID)) from exc
            if not _is_integer(max_attempts):
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="max_attempts"))
            if max_attempts < 1:
                raise MemoryJobValidationError(t(ERR_VALUE_MUST_BE_POSITIVE, field="max_attempts"))
            if memory_id is not None and not _is_integer(memory_id):
                raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_INVALID, field="memory_id"))
            if memory_id is not None and memory_id <= 0:
                raise MemoryJobValidationError(t(ERR_VALUE_MUST_BE_POSITIVE, field="memory_id"))
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
                        LongTermMemoryMutationOperation.RESTORE,
                        LongTermMemoryMutationOperation.DELETE_CLEANUP,
                    }
                    and memory_id is None
                ):
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="memory_id"))
                if (
                    operation
                    in {
                        LongTermMemoryMutationOperation.UPDATE,
                        LongTermMemoryMutationOperation.RESTORE,
                    }
                    and expected_version is None
                ):
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_FIELD_REQUIRED, field="expected_version"))
                if operation == LongTermMemoryMutationOperation.CREATE and expected_version is not None:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_CREATE_VERSION_FORBIDDEN))
            elif operation in _NON_TARGET_OPERATIONS:
                if active_mutation_key is not None or memory_id is not None or expected_version is not None:
                    raise MemoryJobValidationError(t(ERR_MEMORY_JOB_NON_TARGET_FIELDS_FORBIDDEN))

            initial_available_at = available_at if available_at is not None else await get_database_time(db)
            values: dict[str, Any] = {
                "operation": operation,
                "dedupe_key": dedupe_key,
                "active_mutation_key": active_mutation_key,
                "memory_id": memory_id,
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

            if is_target_operation and memory_id is not None:
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

    async def get_job(
        self,
        db: AsyncSession,
        *,
        uid: str,
        job_id: int,
    ) -> LongTermMemoryMutationJob | None:
        return await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)

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
        return await memory_job_crud.request_cancel(db, uid=uid, job_id=job_id, commit=commit)


memory_job_manager = MemoryJobManager()


__all__ = [
    "MemoryJobCancelResult",
    "MemoryJobManager",
    "MemoryJobSubmissionError",
    "MemoryJobSubmissionResult",
    "MemoryJobTargetBusyError",
    "MemoryJobValidationError",
    "memory_job_manager",
]
