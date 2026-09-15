from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.job import KnowledgeJobCancelResult, knowledge_job_crud
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobDeterministicError,
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
from app.providers.vector import async_delete_collection, async_delete_collection_if_exists, async_validate_collection

from .migration_build import (
    _build_migration,
    _catch_up_migration,
)
from .migration_common import (
    _PRE_SWITCH_MIGRATION_STATUSES,
    _request_hash,
    _validate_payload,
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
    "handle_migration_target_cleanup",
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
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
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
) -> KnowledgeJob | None:
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
    cleanup_job = None
    if target_collection:
        cleanup_request = {
            "knowledge_base_id": job.knowledge_base_id,
            "migration_job_id": job.id,
            "collection": target_collection,
        }
        cleanup_job, _ = await knowledge_job_crud.create(
            db,
            uid=job.uid,
            parent_job_id=job.id,
            operation=KnowledgeJobOperation.MIGRATION_TARGET_CLEANUP,
            dedupe_key=f"kb-migration-target-cleanup:{job.id}",
            request_hash=_request_hash(cleanup_request),
            active_change_key=None,
            knowledge_base_id=job.knowledge_base_id,
            payload=cleanup_request,
            available_at=await get_database_time(db),
            max_attempts=job.max_attempts,
            commit=False,
        )
    await db.flush()
    return cleanup_job


async def handle_migration_target_cleanup(
    context: KnowledgeJobExecutionContext,
) -> KnowledgeJobExecutionResult:
    job = await context.checkpoint()
    if job.id is None or job.parent_job_id is None or not isinstance(job.payload, dict):
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    collection_name = job.payload.get("collection")
    migration_job_id = job.payload.get("migration_job_id")
    knowledge_base_id = job.payload.get("knowledge_base_id")
    if (
        not isinstance(collection_name, str)
        or not collection_name
        or isinstance(migration_job_id, bool)
        or not isinstance(migration_job_id, int)
        or migration_job_id < 1
        or migration_job_id != job.parent_job_id
        or isinstance(knowledge_base_id, bool)
        or not isinstance(knowledge_base_id, int)
        or knowledge_base_id != job.knowledge_base_id
    ):
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

    async with context.session_factory() as db:
        parent = await knowledge_job_crud.get_by_id(
            db,
            uid=job.uid,
            job_id=migration_job_id,
        )
        knowledge_base = await knowledge_base_crud.get(db, job.knowledge_base_id)
        await db.commit()
    parent_target = parent.payload.get("target") if parent is not None and isinstance(parent.payload, dict) else None
    if (
        parent is None
        or parent.operation != KnowledgeJobOperation.EMBEDDING_MIGRATION
        or parent.status not in {KnowledgeJobStatus.FAILED, KnowledgeJobStatus.CANCELLED}
        or parent.knowledge_base_id != job.knowledge_base_id
        or not isinstance(parent_target, dict)
        or parent_target.get("collection") != collection_name
        or knowledge_base is None
        or knowledge_base.uid != job.uid
        or resolve_active_knowledge_base_embedding(knowledge_base).collection_name == collection_name
    ):
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

    await context.checkpoint()
    try:
        await async_delete_collection_if_exists(collection_name)
    except Exception as exc:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc
    return KnowledgeJobExecutionResult(
        result={
            "knowledge_base_id": job.knowledge_base_id,
            "migration_job_id": migration_job_id,
            "collection": collection_name,
        }
    )


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
    if cancellation.changed and cancellation.job is not None and cancellation.job.status == KnowledgeJobStatus.CANCELLED:
        await finalize_knowledge_migration_terminal_state(
            db,
            job=cancellation.job,
            error=cancellation.job.error,
        )
    await db.commit()
    return cancellation
