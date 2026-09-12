from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_ACTIVE_CONFIG_CHANGED,
    ERR_MEMORY_JOB_ACTIVE_TARGET_BUSY,
    ERR_MEMORY_JOB_CREATE_VERSION_FORBIDDEN,
    ERR_MEMORY_JOB_DEDUPE_CONFLICT,
    ERR_MEMORY_JOB_FIELD_INVALID,
    ERR_MEMORY_JOB_FIELD_REQUIRED,
    ERR_MEMORY_JOB_NON_TARGET_FIELDS_FORBIDDEN,
    ERR_MEMORY_JOB_OPERATION_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_UID_FORBIDDEN,
    ERR_MEMORY_JOB_TARGET_BUSY,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.crud.channel.channel import channel_crud
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud, memory_store_crud
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationOperation,
    LongTermMemoryStore,
)
from app.providers.database.time import get_database_time

from .manager_common import (
    _NON_TARGET_OPERATIONS,
    _ORGANIZE_OPERATIONS,
    _SUBMITTABLE_OPERATIONS,
    _TARGET_OPERATIONS,
    MemoryJobSubmissionResult,
    MemoryJobTargetBusyError,
    MemoryJobValidationError,
    _is_active_mutation_key_integrity_error,
    _is_integer,
    _job_matches_submission_identity,
    _require_non_empty_string,
    _validate_source_ids,
)

__all__ = [
    "MemoryJobSubmission",
]


class MemoryJobSubmission:
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
