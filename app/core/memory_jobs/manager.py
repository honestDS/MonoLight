from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
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
    ERR_MEMORY_MIGRATION_CANNOT_CANCEL_AFTER_SWITCHING,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.crud.memory import memory_record_crud, memory_store_crud
from app.core.crud.memory_job import MemoryJobCancelResult, memory_job_crud
from app.core.i18n import t
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
)
from app.providers.database.time import get_database_time

_TARGET_OPERATIONS = frozenset(
    {
        LongTermMemoryMutationOperation.CREATE,
        LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
        LongTermMemoryMutationOperation.UPDATE,
        LongTermMemoryMutationOperation.RESTORE,
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

            if (
                operation
                in {
                    LongTermMemoryMutationOperation.UPDATE,
                    LongTermMemoryMutationOperation.RESTORE,
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

    async def submit_organization(
        self,
        db: AsyncSession,
        *,
        uid: str,
        dedupe_key: str | None = None,
        commit: bool = True,
    ) -> MemoryJobSubmissionResult:
        from app.core.memory.errors import MemoryConflictError
        from app.core.memory.identifiers import build_memory_organization_active_mutation_key
        from app.core.memory.normalization import _normalize_dedupe_key, _normalize_uid, _validate_commit
        from app.core.memory.organization import (
            build_organization_dedupe_key,
            build_organization_job_payload,
            build_organization_snapshot,
            load_organization_model_config_for_store,
            validate_organization_submission_store,
        )

        normalized_commit = _validate_commit(commit)
        try:
            normalized_uid = _normalize_uid(uid)
            normalized_dedupe_key = _normalize_dedupe_key(dedupe_key) if dedupe_key is not None else None
            store = await memory_store_crud.lock_for_mutation(db, uid=normalized_uid, commit=False)
            if store is None:
                raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)
            validate_organization_submission_store(store)

            records = await memory_record_crud.list_recallable_for_organization(db, uid=normalized_uid)
            snapshot = build_organization_snapshot(
                records,
                active_embedding_revision=store.active_embedding_revision,
                index_revision=store.index_revision,
                policy_version=store.organization_policy_version,
            )
            organization_model = await load_organization_model_config_for_store(
                db,
                store=store,
                snapshot_count=snapshot.count,
            )
            payload = build_organization_job_payload(snapshot, organization_model)
            final_dedupe_key = build_organization_dedupe_key(
                normalized_uid,
                snapshot_digest=snapshot.digest,
                policy_version=store.organization_policy_version,
                caller_dedupe_key=normalized_dedupe_key,
            )
            submission = await self.submit(
                db,
                uid=normalized_uid,
                operation=LongTermMemoryMutationOperation.ORGANIZE,
                dedupe_key=final_dedupe_key,
                payload=payload,
                active_mutation_key=build_memory_organization_active_mutation_key(normalized_uid),
                commit=False,
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
            return await memory_job_crud.request_cancel(db, uid=uid, job_id=job_id, commit=commit)

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
