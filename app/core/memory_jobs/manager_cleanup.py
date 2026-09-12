from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_OPERATION_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_TARGET_BUSY,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud
from app.core.i18n import t
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemorySource,
)
from app.providers.database.time import get_database_time

from .manager_common import (
    MemoryJobSubmissionResult,
    MemoryJobTargetBusyError,
    MemoryJobValidationError,
    _is_integer,
)

__all__ = [
    "MemoryJobCleanup",
]


class MemoryJobCleanup:
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
