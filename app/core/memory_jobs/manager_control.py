from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_JOB_CANCELLATION_REQUESTED,
    ERR_MEMORY_MIGRATION_CANNOT_CANCEL_AFTER_SWITCHING,
)
from app.core.crud.memory.job import MemoryJobCancelResult, memory_job_crud
from app.core.crud.memory.store import memory_store_crud
from app.core.i18n import t
from app.core.memory_jobs.maintenance_lifecycle import (
    finalize_maintenance_terminal_state,
    mark_cancelled_target_cleanup_failure,
)
from app.models.memory import (
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryOldCollectionCleanupStatus,
)

__all__ = [
    "MemoryJobControl",
]


class MemoryJobControl:
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
