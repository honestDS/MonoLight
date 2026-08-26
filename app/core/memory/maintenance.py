from __future__ import annotations

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_MEMORY_MAINTENANCE_STATE_CONFLICT, ERR_MEMORY_NOT_CONFIGURED
from app.core.crud.memory import memory_store_crud
from app.core.crud.memory_job import memory_job_crud
from app.core.crud.memory_maintenance import memory_maintenance_store_crud
from app.core.memory.errors import MemoryConflictError
from app.core.memory.identifiers import build_memory_collection_name
from app.core.memory.normalization import _normalize_dedupe_key, _normalize_uid, _require_positive, _validate_commit
from app.core.memory_jobs.manager import MemoryJobSubmissionResult, is_organization_chain_job, memory_job_manager
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
    LongTermMemoryStore,
)

_ACTIVE_MIGRATION_STATUSES = frozenset(
    {
        LongTermMemoryMigrationStatus.PREPARING,
        LongTermMemoryMigrationStatus.BUILDING,
        LongTermMemoryMigrationStatus.CATCHING_UP,
        LongTermMemoryMigrationStatus.VALIDATING,
        LongTermMemoryMigrationStatus.SWITCHING,
    }
)
_CLEANUP_RESTARTABLE_STATUSES = frozenset(
    {
        LongTermMemoryOldCollectionCleanupStatus.PENDING,
        LongTermMemoryOldCollectionCleanupStatus.RUNNING,
        LongTermMemoryOldCollectionCleanupStatus.FAILED,
        LongTermMemoryOldCollectionCleanupStatus.SUCCEEDED,
    }
)


def _validate_active_embedding(store: LongTermMemoryStore) -> None:
    required = (
        store.active_embedding_channel_id,
        store.active_embedding_model_id,
        store.active_embedding_dimensions,
        store.active_embedding_signature,
        store.active_collection_name,
    )
    if (
        isinstance(store.active_embedding_revision, bool)
        or not isinstance(store.active_embedding_revision, int)
        or store.active_embedding_revision < 1
        or any(value is None or value == "" for value in required)
        or isinstance(store.active_embedding_channel_id, bool)
        or not isinstance(store.active_embedding_channel_id, int)
        or store.active_embedding_channel_id < 1
        or isinstance(store.active_embedding_dimensions, bool)
        or not isinstance(store.active_embedding_dimensions, int)
        or store.active_embedding_dimensions < 1
    ):
        raise MemoryConflictError(ERR_MEMORY_NOT_CONFIGURED)


def _has_active_embedding_migration(store: LongTermMemoryStore) -> bool:
    return store.migration_job_id is not None and store.migration_status in _ACTIVE_MIGRATION_STATUSES


def _reindex_payload(store: LongTermMemoryStore, *, target_collection_name: str, target_index_revision: int) -> dict[str, Any]:
    return {
        "from": {
            "channel_id": store.active_embedding_channel_id,
            "model_id": store.active_embedding_model_id,
            "dimensions": store.active_embedding_dimensions,
            "signature": store.active_embedding_signature,
            "embedding_revision": store.active_embedding_revision,
            "collection": store.active_collection_name,
            "index_revision": store.index_revision,
        },
        "target": {
            "collection": target_collection_name,
            "index_revision": target_index_revision,
        },
        "progress": {
            "phase": "preparing",
            "snapshot_initialized": False,
            "snapshot_boundary": 0,
            "cursor": 0,
            "total_count": 0,
            "success_count": 0,
            "failure_count": 0,
        },
    }


async def submit_memory_reindex(
    db: AsyncSession,
    *,
    uid: str,
    dedupe_key: str,
    source_session_id: str | None = None,
    source_profile_id: int | None = None,
    source_message_id: int | None = None,
    max_attempts: int = 3,
    commit: bool = True,
) -> MemoryJobSubmissionResult:
    try:
        normalized_uid = _normalize_uid(uid)
        normalized_dedupe_key = _normalize_dedupe_key(dedupe_key)
        commit = _validate_commit(commit)
        existing_job = await memory_job_crud.get_by_dedupe_key(
            db,
            uid=normalized_uid,
            dedupe_key=normalized_dedupe_key,
        )
        if existing_job is not None:
            submission = await memory_job_manager.submit(
                db,
                uid=normalized_uid,
                operation=LongTermMemoryMutationOperation.REINDEX,
                dedupe_key=normalized_dedupe_key,
                payload=existing_job.payload,
                source_session_id=source_session_id,
                source_profile_id=source_profile_id,
                source_message_id=source_message_id,
                max_attempts=max_attempts,
                commit=False,
            )
            if commit:
                await db.commit()
                await db.refresh(submission.job)
            else:
                await db.flush()
            return submission
        store = await memory_store_crud.lock_for_mutation(db, uid=normalized_uid, commit=False)
        if store is None:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        _validate_active_embedding(store)
        unfinished_jobs = await memory_job_crud.list_unfinished_by_uid(db, uid=normalized_uid)
        if any(is_organization_chain_job(job) for job in unfinished_jobs):
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if _has_active_embedding_migration(store):
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if store.old_collection_cleanup_status in {
            LongTermMemoryOldCollectionCleanupStatus.PENDING,
            LongTermMemoryOldCollectionCleanupStatus.RUNNING,
            LongTermMemoryOldCollectionCleanupStatus.FAILED,
        }:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)

        target_index_revision = store.index_revision + 1
        target_collection_name = build_memory_collection_name(
            normalized_uid,
            store.active_embedding_signature,
            target_index_revision,
            "reindex",
        )
        payload = _reindex_payload(
            store,
            target_collection_name=target_collection_name,
            target_index_revision=target_index_revision,
        )
        submission = await memory_job_manager.submit(
            db,
            uid=normalized_uid,
            operation=LongTermMemoryMutationOperation.REINDEX,
            dedupe_key=normalized_dedupe_key,
            payload=payload,
            source_session_id=source_session_id,
            source_profile_id=source_profile_id,
            source_message_id=source_message_id,
            max_attempts=max_attempts,
            commit=False,
        )
        if submission.created:
            if store.index_status == LongTermMemoryIndexStatus.REINDEXING:
                raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
            started = await memory_maintenance_store_crud.start_reindex(
                db,
                uid=normalized_uid,
                expected_active_revision=store.active_embedding_revision,
                expected_active_collection_name=store.active_collection_name,
                expected_index_revision=store.index_revision,
                commit=False,
            )
            if started is None:
                raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if commit:
            await db.commit()
            await db.refresh(submission.job)
        else:
            await db.flush()
        return submission
    except Exception:
        await db.rollback()
        raise


async def submit_memory_cleanup_retry(
    db: AsyncSession,
    *,
    uid: str,
    dedupe_key: str,
    job_id: int,
    source_session_id: str | None = None,
    source_profile_id: int | None = None,
    source_message_id: int | None = None,
    max_attempts: int = 3,
    commit: bool = True,
) -> MemoryJobSubmissionResult:
    try:
        normalized_uid = _normalize_uid(uid)
        normalized_dedupe_key = _normalize_dedupe_key(dedupe_key)
        normalized_job_id = _require_positive(job_id, field="job_id")
        commit = _validate_commit(commit)
        existing_job = await memory_job_crud.get_by_dedupe_key(
            db,
            uid=normalized_uid,
            dedupe_key=normalized_dedupe_key,
        )
        if existing_job is not None:
            store = await memory_store_crud.lock_for_mutation(db, uid=normalized_uid, commit=False)
            if store is None:
                raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
            if existing_job.parent_job_id != normalized_job_id:
                raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
            if store.old_collection_cleanup_job_id != existing_job.id:
                raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
            if store.old_collection_cleanup_status not in _CLEANUP_RESTARTABLE_STATUSES:
                raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
            submission = await memory_job_manager.submit(
                db,
                uid=normalized_uid,
                operation=existing_job.operation,
                dedupe_key=normalized_dedupe_key,
                parent_job_id=normalized_job_id,
                payload=existing_job.payload,
                source_session_id=source_session_id,
                source_profile_id=source_profile_id,
                source_message_id=source_message_id,
                max_attempts=max_attempts,
                commit=False,
            )
            if commit:
                await db.commit()
                await db.refresh(submission.job)
            else:
                await db.flush()
            return submission

        store = await memory_store_crud.lock_for_mutation(db, uid=normalized_uid, commit=False)
        if store is None:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if store.old_collection_cleanup_job_id != normalized_job_id:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        old_cleanup_job_id = normalized_job_id
        if store.old_collection_cleanup_status != LongTermMemoryOldCollectionCleanupStatus.FAILED:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if not store.old_collection_name:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)

        old_job = await memory_job_crud.get_by_id(
            db,
            uid=normalized_uid,
            job_id=old_cleanup_job_id,
        )
        if (
            old_job is None
            or old_job.operation
            not in {
                LongTermMemoryMutationOperation.REINDEX,
                LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            }
            or old_job.status
            not in {
                LongTermMemoryMutationStatus.FAILED,
                LongTermMemoryMutationStatus.CANCELLED,
            }
            or not isinstance(old_job.payload, dict)
        ):
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)

        submission = await memory_job_manager.submit(
            db,
            uid=normalized_uid,
            operation=old_job.operation,
            dedupe_key=normalized_dedupe_key,
            parent_job_id=normalized_job_id,
            payload=old_job.payload,
            source_session_id=source_session_id,
            source_profile_id=source_profile_id,
            source_message_id=source_message_id,
            max_attempts=max_attempts,
            commit=False,
        )
        new_job = submission.job
        new_job_id = new_job.id if new_job is not None else None
        if not submission.created:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if isinstance(new_job_id, bool):
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if not isinstance(new_job_id, int):
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if new_job_id < 1:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        restarted = await memory_maintenance_store_crud.restart_old_collection_cleanup(
            db,
            uid=normalized_uid,
            expected_cleanup_job_id=old_cleanup_job_id,
            retry_cleanup_job_id=new_job_id,
            commit=False,
        )
        if restarted is None:
            raise MemoryConflictError(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        if commit:
            await db.commit()
            await db.refresh(submission.job)
        else:
            await db.flush()
        return submission
    except Exception:
        await db.rollback()
        raise


__all__ = ["submit_memory_cleanup_retry", "submit_memory_reindex"]
