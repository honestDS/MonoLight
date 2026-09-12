from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
    LOG_KB_TERMINAL_TARGET_CLEANUP_FAILED,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.job import KnowledgeJobCancelResult, knowledge_job_crud
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutionResult,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
)
from app.models.knowledge_base import (
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
)
from app.providers.database.time import get_database_time
from app.providers.vector import async_delete_collection, async_validate_collection

from .migration_build import (
    _build_migration,
    _catch_up_migration,
)
from .migration_common import (
    _PRE_SWITCH_MIGRATION_STATUSES,
    _validate_payload,
    logger,
)
from .migration_prepare import (
    _prepare_migration,
    lock_migrating_knowledge_base,
)
from .migration_switch import (
    _switch_migration,
    _validate_migration,
)

__all__ = [
    "handle_embedding_migration",
    "handle_old_collection_cleanup",
    "finalize_knowledge_migration_terminal_state",
    "cleanup_terminal_target_collection",
    "cancel_knowledge_base_embedding_migration",
]


async def handle_embedding_migration(
    context: KnowledgeJobExecutionContext,
) -> KnowledgeJobExecutionResult:
    job = await context.checkpoint()
    payload = _validate_payload(job)
    while True:
        async with context.session_factory() as db:
            knowledge_base = await knowledge_base_crud.get(
                db,
                job.knowledge_base_id,
            )
            await db.commit()
        if knowledge_base is None or knowledge_base.migration_job_id != job.id:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        status = KnowledgeBaseMigrationStatus(knowledge_base.migration_status)
        if status == KnowledgeBaseMigrationStatus.PREPARING:
            payload = await _prepare_migration(context, payload)
            continue
        if status == KnowledgeBaseMigrationStatus.BUILDING:
            await _build_migration(context, payload)
            continue
        if status == KnowledgeBaseMigrationStatus.CATCHING_UP:
            await _catch_up_migration(context, payload)
            continue
        if status == KnowledgeBaseMigrationStatus.VALIDATING:
            if knowledge_base.migration_delta_high_watermark != knowledge_base.migration_delta_applied_watermark:
                async with context.session_factory() as db:
                    locked = await lock_migrating_knowledge_base(
                        db,
                        uid=job.uid,
                        knowledge_base_id=job.knowledge_base_id,
                    )
                    if locked is None or locked.migration_job_id != job.id or locked.migration_status != KnowledgeBaseMigrationStatus.VALIDATING:
                        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
                    if locked.migration_delta_high_watermark != locked.migration_delta_applied_watermark:
                        locked.migration_status = KnowledgeBaseMigrationStatus.CATCHING_UP
                    await db.commit()
                continue
            validation = await _validate_migration(context, payload)
            return await _switch_migration(context, payload, validation)
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))


async def handle_old_collection_cleanup(
    context: KnowledgeJobExecutionContext,
) -> KnowledgeJobExecutionResult:
    job = await context.checkpoint()
    if job.id is None or not isinstance(job.payload, dict):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    collection_name = job.payload.get("collection")
    migration_job_id = job.payload.get("migration_job_id")
    if not isinstance(collection_name, str) or not collection_name or isinstance(migration_job_id, bool) or not isinstance(migration_job_id, int) or migration_job_id < 1:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

    async with context.session_factory() as db:
        knowledge_base = await lock_migrating_knowledge_base(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
        )
        if knowledge_base is None or knowledge_base.old_collection_cleanup_job_id != job.id or knowledge_base.old_collection_name != collection_name:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        knowledge_base.old_collection_cleanup_status = KnowledgeBaseOldCollectionCleanupStatus.RUNNING
        knowledge_base.old_collection_cleanup_error = None
        await db.commit()

    try:
        validation = await async_validate_collection(collection_name)
        if getattr(validation, "exists", False):
            await async_delete_collection(collection_name)
    except Exception as exc:
        async with context.session_factory() as db:
            knowledge_base = await lock_migrating_knowledge_base(
                db,
                uid=job.uid,
                knowledge_base_id=job.knowledge_base_id,
            )
            if knowledge_base is not None and knowledge_base.old_collection_cleanup_job_id == job.id:
                knowledge_base.old_collection_cleanup_status = KnowledgeBaseOldCollectionCleanupStatus.FAILED
                knowledge_base.old_collection_cleanup_error = t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)
                await db.commit()
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc

    result = {
        "knowledge_base_id": job.knowledge_base_id,
        "collection": collection_name,
        "migration_job_id": migration_job_id,
    }
    async with context.session_factory() as db:
        knowledge_base = await lock_migrating_knowledge_base(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
        )
        if knowledge_base is None or knowledge_base.old_collection_cleanup_job_id != job.id:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        knowledge_base.old_collection_cleanup_status = KnowledgeBaseOldCollectionCleanupStatus.SUCCEEDED
        knowledge_base.old_collection_cleanup_error = None
        knowledge_base.old_collection_cleanup_at = await get_database_time(db)
        if not await knowledge_job_crud.mark_succeeded(
            db,
            uid=job.uid,
            job_id=job.id,
            owner=context.worker_id,
            result=result,
            commit=False,
        ):
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        await db.commit()
    return KnowledgeJobExecutionResult(result=result, finalized=True)


async def finalize_knowledge_migration_terminal_state(
    db: AsyncSession,
    *,
    job: KnowledgeJob,
    error: str | None,
) -> str | None:
    if (
        job.operation != KnowledgeJobOperation.EMBEDDING_MIGRATION
        or job.id is None
        or job.status
        not in {
            KnowledgeJobStatus.FAILED,
            KnowledgeJobStatus.CANCELLED,
        }
    ):
        return None
    knowledge_base = await lock_migrating_knowledge_base(
        db,
        uid=job.uid,
        knowledge_base_id=job.knowledge_base_id,
    )
    if knowledge_base is None or knowledge_base.migration_job_id != job.id or knowledge_base.migration_status not in _PRE_SWITCH_MIGRATION_STATUSES:
        return None
    target_collection = knowledge_base.target_collection_name
    terminal_status = KnowledgeBaseMigrationStatus.CANCELLED if job.status == KnowledgeJobStatus.CANCELLED else KnowledgeBaseMigrationStatus.FAILED
    knowledge_base.migration_status = terminal_status
    knowledge_base.migration_error = error
    knowledge_base.migration_finished_at = await get_database_time(db)
    if terminal_status == KnowledgeBaseMigrationStatus.FAILED:
        knowledge_base.migration_failure_count = max(
            1,
            knowledge_base.migration_failure_count,
        )
    knowledge_base.target_embedding_channel_id = None
    knowledge_base.target_embedding_model_id = None
    knowledge_base.target_embedding_dimensions = None
    knowledge_base.target_embedding_signature = None
    knowledge_base.target_embedding_revision = None
    knowledge_base.target_collection_name = None
    await db.flush()
    return target_collection


async def cleanup_terminal_target_collection(collection_name: str | None) -> None:
    if not collection_name:
        return
    try:
        validation = await async_validate_collection(collection_name)
        if getattr(validation, "exists", False):
            await async_delete_collection(collection_name)
    except Exception as exc:
        logger.bind(collection_name=collection_name, error_type=type(exc).__name__).warning(t(LOG_KB_TERMINAL_TARGET_CLEANUP_FAILED, collection_name=collection_name))
        return


async def cancel_knowledge_base_embedding_migration(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
) -> KnowledgeJobCancelResult:
    knowledge_base = await knowledge_base_crud.get(db, knowledge_base_id)
    if knowledge_base is None or knowledge_base.uid != uid or knowledge_base.migration_job_id is None:
        return KnowledgeJobCancelResult(job=None, accepted=False, changed=False)
    job = await knowledge_job_crud.get_by_id(
        db,
        uid=uid,
        job_id=knowledge_base.migration_job_id,
    )
    if job is None or job.operation != KnowledgeJobOperation.EMBEDDING_MIGRATION:
        return KnowledgeJobCancelResult(job=job, accepted=False, changed=False)
    cancellation = await knowledge_job_crud.request_cancel(
        db,
        uid=uid,
        job_id=job.id,
        commit=False,
    )
    target_collection = None
    if cancellation.changed and cancellation.job is not None and cancellation.job.status == KnowledgeJobStatus.CANCELLED:
        target_collection = await finalize_knowledge_migration_terminal_state(
            db,
            job=cancellation.job,
            error=cancellation.job.error,
        )
    await db.commit()
    await cleanup_terminal_target_collection(target_collection)
    return cancellation
