from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_MEMORY_OLD_COLLECTION_CLEANUP_FAILED
from app.core.crud.memory import memory_embedding_revision_crud, memory_store_crud
from app.core.crud.memory_job import memory_job_crud
from app.core.crud.memory_maintenance import (
    memory_maintenance_job_crud,
    memory_maintenance_store_crud,
)
from app.core.i18n import t
from app.core.memory_jobs.executor import (
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
    MemoryJobExecutionResult,
)
from app.core.memory_jobs.maintenance_state import (
    MIGRATION_OPERATION,
    MIGRATION_PRE_SWITCH_STATUSES,
    REINDEX_OPERATION,
    matches_reindex_source,
    require_job_id,
    retryable,
    validate_migration_payload,
    validate_reindex_payload,
)
from app.models.memory import (
    LongTermMemoryEmbeddingRevisionStatus,
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
)
from app.providers.database.time import get_database_time
from app.providers.vector import async_delete_collection, async_validate_collection


async def cleanup_old_collection(
    context: MemoryJobExecutionContext,
    *,
    operation: LongTermMemoryMutationOperation,
) -> MemoryJobExecutionResult:
    job_id = require_job_id(context.job)
    async with context.session_factory() as db:
        store = await memory_store_crud.lock_for_mutation(db, uid=context.job.uid, commit=False)
        if store is None or store.old_collection_cleanup_job_id != job_id or not store.old_collection_name:
            raise retryable(ERR_MEMORY_OLD_COLLECTION_CLEANUP_FAILED)
        old_collection_name = store.old_collection_name
        if store.old_collection_cleanup_status != LongTermMemoryOldCollectionCleanupStatus.RUNNING:
            updated = await memory_maintenance_store_crud.update_old_collection_cleanup(
                db,
                uid=context.job.uid,
                old_collection_cleanup_job_id=job_id,
                old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.RUNNING,
                old_collection_cleanup_error=None,
                commit=False,
            )
            if updated is None:
                raise retryable(ERR_MEMORY_OLD_COLLECTION_CLEANUP_FAILED)
        await db.commit()
    try:
        validation = await async_validate_collection(old_collection_name)
        if getattr(validation, "exists", False):
            await async_delete_collection(old_collection_name)
    except Exception as exc:
        async with context.session_factory() as db:
            await memory_maintenance_store_crud.update_old_collection_cleanup(
                db,
                uid=context.job.uid,
                old_collection_cleanup_job_id=job_id,
                old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.FAILED,
                old_collection_cleanup_error=t(ERR_MEMORY_OLD_COLLECTION_CLEANUP_FAILED),
                commit=True,
            )
        raise retryable(ERR_MEMORY_OLD_COLLECTION_CLEANUP_FAILED) from exc

    result = {
        "operation": operation.value,
        "collection": old_collection_name,
        "finalized": True,
    }
    async with context.session_factory() as db:
        cleanup_at = await get_database_time(db)
        updated = await memory_maintenance_store_crud.update_old_collection_cleanup(
            db,
            uid=context.job.uid,
            old_collection_cleanup_job_id=job_id,
            old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.SUCCEEDED,
            old_collection_cleanup_error=None,
            old_collection_cleanup_at=cleanup_at,
            commit=False,
        )
        if updated is None or not await memory_job_crud.mark_succeeded(
            db,
            uid=context.job.uid,
            job_id=job_id,
            owner=context.worker_id,
            result=result,
            commit=False,
        ):
            raise retryable(ERR_MEMORY_OLD_COLLECTION_CLEANUP_FAILED)
        await db.commit()
    return MemoryJobExecutionResult(result=result, finalized=True)


async def delete_cancelled_target_collection(
    context: MemoryJobExecutionContext,
    *,
    operation: LongTermMemoryMutationOperation,
) -> None:
    if operation == REINDEX_OPERATION:
        payload = validate_reindex_payload(context.job)
        target_collection = payload["target"]["collection"]
    elif operation == MIGRATION_OPERATION:
        payload = validate_migration_payload(context.job)
        target_collection = payload["target"]["collection"]
    else:
        return
    try:
        validation = await async_validate_collection(target_collection)
        if getattr(validation, "exists", False):
            await async_delete_collection(target_collection)
    except Exception:
        async with context.session_factory() as db:
            await mark_cancelled_target_cleanup_failure(db, job=context.job)
            await db.commit()
        return


async def mark_cancelled_target_cleanup_failure(
    db: AsyncSession,
    *,
    job: LongTermMemoryMutationJob,
) -> None:
    if isinstance(job.id, bool) or not isinstance(job.id, int) or job.id < 1:
        return
    try:
        operation = LongTermMemoryMutationOperation(job.operation)
    except (TypeError, ValueError):
        return
    if operation == REINDEX_OPERATION:
        try:
            payload = validate_reindex_payload(job)
        except MemoryJobExecutionError:
            return
    elif operation == MIGRATION_OPERATION:
        try:
            payload = validate_migration_payload(job)
        except MemoryJobExecutionError:
            return
    else:
        return
    await memory_maintenance_store_crud.mark_target_collection_cleanup_failed(
        db,
        uid=job.uid,
        job_id=job.id,
        operation=operation,
        expected_active_collection_name=payload["from"]["collection"],
        target_collection_name=payload["target"]["collection"],
        error=t(ERR_MEMORY_OLD_COLLECTION_CLEANUP_FAILED),
        commit=False,
    )


async def record_migration_retry_error(
    context: MemoryJobExecutionContext,
    error_key: str,
) -> None:
    job_id = require_job_id(context.job)
    async with context.session_factory() as db:
        store = await memory_store_crud.get_snapshot_by_uid(db, uid=context.job.uid)
        if store is not None and store.migration_job_id == job_id and store.migration_status in MIGRATION_PRE_SWITCH_STATUSES:
            await memory_maintenance_store_crud.update_embedding_migration_progress(
                db,
                uid=context.job.uid,
                migration_job_id=job_id,
                migration_error=t(error_key),
                commit=True,
            )
        else:
            await db.rollback()


async def finalize_maintenance_terminal_state(
    db: AsyncSession,
    *,
    job: LongTermMemoryMutationJob,
    status: LongTermMemoryMutationStatus,
    error: str | None,
) -> None:
    if status not in {
        LongTermMemoryMutationStatus.FAILED,
        LongTermMemoryMutationStatus.CANCELLED,
    }:
        return
    try:
        operation = LongTermMemoryMutationOperation(job.operation)
    except (TypeError, ValueError):
        return
    if operation not in {REINDEX_OPERATION, MIGRATION_OPERATION}:
        return
    if isinstance(job.id, bool) or not isinstance(job.id, int) or job.id < 1:
        return
    job_id = job.id

    try:
        if operation == REINDEX_OPERATION:
            payload = validate_reindex_payload(job)
        else:
            payload = validate_migration_payload(job)
    except MemoryJobExecutionError:
        return

    if operation == REINDEX_OPERATION and status == LongTermMemoryMutationStatus.FAILED:
        progress = payload["progress"]
        updated_progress: dict[str, object] = {}
        for key, value in progress.items():
            updated_progress[key] = value
        updated_progress["failure_count"] = max(1, int(progress["failure_count"]))
        updated_payload: dict[str, object] = {}
        for key, value in payload.items():
            updated_payload[key] = value
        updated_payload["progress"] = updated_progress
        await memory_maintenance_job_crud.update_terminal_payload(
            db,
            uid=job.uid,
            job_id=job_id,
            payload=updated_payload,
            commit=False,
        )

    store = await memory_store_crud.lock_for_mutation(db, uid=job.uid, commit=False)
    if store is None:
        return
    if (
        store.old_collection_cleanup_job_id == job_id
        and store.old_collection_name
        and store.old_collection_cleanup_status
        in {
            LongTermMemoryOldCollectionCleanupStatus.PENDING,
            LongTermMemoryOldCollectionCleanupStatus.RUNNING,
        }
    ):
        cleanup_error = error
        if cleanup_error is None:
            cleanup_error = t(ERR_MEMORY_OLD_COLLECTION_CLEANUP_FAILED)
        updated_cleanup = await memory_maintenance_store_crud.update_old_collection_cleanup(
            db,
            uid=job.uid,
            old_collection_cleanup_job_id=job_id,
            old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.FAILED,
            old_collection_cleanup_error=cleanup_error,
            old_collection_cleanup_at=None,
            commit=False,
        )
        if updated_cleanup is None:
            return
        return
    now = await get_database_time(db)
    if operation == REINDEX_OPERATION:
        source_matches = matches_reindex_source(store, payload["from"])
        switched_matches = (
            store.old_collection_cleanup_job_id == job_id
            and store.old_collection_name == payload["from"]["collection"]
            and store.active_collection_name == payload["target"]["collection"]
            and store.index_revision == payload["target"]["index_revision"]
            and store.active_embedding_revision == payload["from"]["embedding_revision"]
        )
        if not source_matches and not switched_matches:
            return
        if switched_matches:
            return
        if store.index_status == LongTermMemoryIndexStatus.REINDEXING:
            if status == LongTermMemoryMutationStatus.CANCELLED:
                next_index_status = LongTermMemoryIndexStatus.READY
            else:
                next_index_status = LongTermMemoryIndexStatus.FAILED
            await memory_store_crud.update_by_uid(
                db,
                uid=job.uid,
                index_status=next_index_status,
                commit=False,
            )
        return

    if store.migration_job_id != job_id:
        return
    if store.migration_status == LongTermMemoryMigrationStatus.FAILED:
        if status == LongTermMemoryMutationStatus.FAILED and store.migration_failure_count < 1:
            await memory_maintenance_store_crud.update_embedding_migration_progress(
                db,
                uid=job.uid,
                migration_job_id=job_id,
                migration_failure_count=1,
                commit=False,
            )
        return
    if store.migration_status not in MIGRATION_PRE_SWITCH_STATUSES:
        return
    target_revision = payload["target"]["revision"]
    revision = await memory_embedding_revision_crud.get_by_revision(
        db,
        uid=job.uid,
        revision=target_revision,
    )
    if revision is None or revision.job_id != job_id:
        return
    if status == LongTermMemoryMutationStatus.CANCELLED:
        next_migration_status = LongTermMemoryMigrationStatus.CANCELLED
        next_revision_status = LongTermMemoryEmbeddingRevisionStatus.CANCELLED
    else:
        next_migration_status = LongTermMemoryMigrationStatus.FAILED
        next_revision_status = LongTermMemoryEmbeddingRevisionStatus.FAILED
    migration_failure_count = store.migration_failure_count
    if status == LongTermMemoryMutationStatus.FAILED:
        migration_failure_count = max(1, migration_failure_count)
    updated = await memory_maintenance_store_crud.update_embedding_migration_progress(
        db,
        uid=job.uid,
        migration_job_id=job_id,
        migration_status=next_migration_status,
        migration_error=error,
        migration_finished_at=now,
        migration_failure_count=migration_failure_count,
        commit=False,
    )
    if updated is None:
        return
    revision_result = await memory_embedding_revision_crud.update_by_revision(
        db,
        uid=job.uid,
        revision=target_revision,
        status=next_revision_status,
        error=error,
        finished_at=now,
        commit=False,
    )
    if revision_result is None:
        return


__all__ = [
    "cleanup_old_collection",
    "delete_cancelled_target_collection",
    "finalize_maintenance_terminal_state",
    "mark_cancelled_target_cleanup_failure",
    "record_migration_retry_error",
]
