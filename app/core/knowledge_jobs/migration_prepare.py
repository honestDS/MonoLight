from __future__ import annotations

import hashlib
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY,
    ERR_KNOWLEDGE_JOB_DEDUPE_CONFLICT,
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.embedding_transition import (
    knowledge_base_migration_crud,
)
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.embedding.common import load_embedding_runtime_config
from app.core.embedding.knowledge_base_runtime import (
    resolve_active_knowledge_base_embedding,
)
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobExecutionContext,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
)
from app.core.knowledge_jobs.manager import (
    KnowledgeJobConflictError,
    KnowledgeJobTargetBusyError,
)
from app.core.utils.database_integrity import is_unique_constraint_violation
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseMigrationStatus,
    KnowledgeJob,
    KnowledgeJobOperation,
)
from app.providers.database.time import get_database_time
from app.providers.vector import async_get_or_create_collection

from .migration_common import (
    _ACTIVE_MIGRATION_STATUSES,
    _BLOCKING_OLD_COLLECTION_CLEANUP_STATUSES,
    _collection_metadata,
    _matches_source,
    _matches_target,
    _positive_int,
    _request_hash,
    _require_string,
    _source_payload,
    _target_payload,
    _validate_payload,
)

__all__ = [
    "lock_migrating_knowledge_base",
    "prepare_knowledge_base_embedding_migration",
]


async def lock_migrating_knowledge_base(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
) -> KnowledgeBase | None:
    return await knowledge_base_crud.lock_owned_by_id(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
    )


async def prepare_knowledge_base_embedding_migration(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    target_channel_id: int,
    target_model_id: str,
    target_dimensions: int,
    target_signature: str,
    dedupe_key: str,
    target_collection_name: str | None = None,
    max_attempts: int = 3,
    commit: bool = True,
) -> KnowledgeJob:
    uid = _require_string(uid, field="uid")
    knowledge_base_id = _positive_int(
        knowledge_base_id,
        field="knowledge_base_id",
    )
    target_channel_id = _positive_int(target_channel_id, field="target_channel_id")
    target_model_id = _require_string(target_model_id, field="target_model_id")
    target_dimensions = _positive_int(target_dimensions, field="target_dimensions")
    target_signature = _require_string(target_signature, field="target_signature")
    dedupe_key = _require_string(dedupe_key, field="dedupe_key")
    max_attempts = _positive_int(max_attempts, field="max_attempts")
    try:
        knowledge_base = await lock_migrating_knowledge_base(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        if knowledge_base is None or knowledge_base.uid != uid or knowledge_base.id is None:
            raise KnowledgeJobConflictError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if knowledge_base.migration_status in _ACTIVE_MIGRATION_STATUSES:
            existing = (
                await knowledge_job_crud.get_by_id(
                    db,
                    uid=uid,
                    job_id=knowledge_base.migration_job_id,
                )
                if knowledge_base.migration_job_id is not None
                else None
            )
            if existing is not None and existing.dedupe_key == dedupe_key:
                return existing
            raise KnowledgeJobTargetBusyError(t(ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY))
        if knowledge_base.old_collection_cleanup_status in _BLOCKING_OLD_COLLECTION_CLEANUP_STATUSES:
            raise KnowledgeJobTargetBusyError(t(ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY))

        active = resolve_active_knowledge_base_embedding(knowledge_base)
        next_revision = knowledge_base.active_embedding_revision + 1
        collection_name = target_collection_name.strip() if isinstance(target_collection_name, str) and target_collection_name.strip() else f"kb_{knowledge_base_id}_migration_r{next_revision}_{hashlib.sha256(dedupe_key.encode('utf-8')).hexdigest()[:16]}"
        source = _source_payload(knowledge_base, active)
        target = _target_payload(
            channel_id=target_channel_id,
            model_id=target_model_id,
            dimensions=target_dimensions,
            signature=target_signature,
            revision=next_revision,
            collection=collection_name,
        )
        request = {
            "knowledge_base_id": knowledge_base_id,
            "from": source,
            "target": target,
        }
        request_hash = _request_hash(request)
        available_at = await get_database_time(db)
        try:
            job, created = await knowledge_job_crud.create(
                db,
                uid=uid,
                operation=KnowledgeJobOperation.EMBEDDING_MIGRATION,
                dedupe_key=dedupe_key,
                request_hash=request_hash,
                active_change_key=f"kb-migration:{knowledge_base_id}",
                status="pending",
                knowledge_base_id=knowledge_base_id,
                payload=request,
                available_at=available_at,
                max_attempts=max_attempts,
                commit=False,
            )
        except IntegrityError as exc:
            if is_unique_constraint_violation(
                exc,
                constraint_names=("uq_knowledge_job_uid_active_change",),
                fallback_marker_groups=(("active_change_key",),),
            ):
                raise KnowledgeJobTargetBusyError(t(ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY)) from exc
            raise
        if not created:
            if job.request_hash != request_hash:
                raise KnowledgeJobConflictError(t(ERR_KNOWLEDGE_JOB_DEDUPE_CONFLICT))
            if knowledge_base.migration_job_id == job.id:
                return job
            raise KnowledgeJobConflictError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

        knowledge_base = await lock_migrating_knowledge_base(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
        )
        if knowledge_base is None or knowledge_base.id is None:
            raise KnowledgeJobConflictError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if knowledge_base.migration_status in _ACTIVE_MIGRATION_STATUSES:
            raise KnowledgeJobTargetBusyError(t(ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY))
        if knowledge_base.old_collection_cleanup_status in _BLOCKING_OLD_COLLECTION_CLEANUP_STATUSES:
            raise KnowledgeJobTargetBusyError(t(ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY))
        if not _matches_source(knowledge_base, source):
            raise KnowledgeJobConflictError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

        now = await get_database_time(db)
        knowledge_base.target_embedding_channel_id = target_channel_id
        knowledge_base.target_embedding_model_id = target_model_id
        knowledge_base.target_embedding_dimensions = target_dimensions
        knowledge_base.target_embedding_signature = target_signature
        knowledge_base.target_embedding_revision = next_revision
        knowledge_base.target_collection_name = collection_name
        knowledge_base.migration_job_id = job.id
        knowledge_base.migration_status = KnowledgeBaseMigrationStatus.PREPARING
        knowledge_base.migration_snapshot_boundary = None
        knowledge_base.migration_cursor = 0
        knowledge_base.migration_total_count = 0
        knowledge_base.migration_success_count = 0
        knowledge_base.migration_failure_count = 0
        knowledge_base.migration_delta_high_watermark = 0
        knowledge_base.migration_delta_applied_watermark = 0
        knowledge_base.migration_error = None
        knowledge_base.migration_started_at = now
        knowledge_base.migration_finished_at = None
        await db.flush()
        if commit:
            await db.commit()
            await db.refresh(job)
        return job
    except Exception:
        if commit and db.in_transaction():
            await db.rollback()
        raise


async def _load_target_runtime_config(
    context: KnowledgeJobExecutionContext,
    target: dict[str, Any],
):
    async with context.session_factory() as db:
        config = await load_embedding_runtime_config(
            db,
            target["channel_id"],
            target["model_id"],
        )
        await db.commit()
        return config


async def _prepare_migration(
    context: KnowledgeJobExecutionContext,
    payload: dict[str, Any],
) -> dict[str, Any]:
    job = await context.checkpoint()
    if job.id is None:
        raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
    payload = _validate_payload(job)
    target = payload["target"]
    async with context.session_factory() as db:
        knowledge_base = await lock_migrating_knowledge_base(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
        )
        if knowledge_base is None or knowledge_base.migration_job_id != job.id or knowledge_base.migration_status != KnowledgeBaseMigrationStatus.PREPARING or not _matches_source(knowledge_base, payload["from"]) or not _matches_target(knowledge_base, target):
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if "snapshot" not in payload:
            boundary = await knowledge_base_migration_crud.get_snapshot_boundary(
                db,
                uid=job.uid,
                knowledge_base_id=job.knowledge_base_id,
            )
            payload["snapshot"] = {
                "document_max_id": boundary.document_max_id,
                "managed_max_id": boundary.managed_max_id,
            }
            updated_job = await knowledge_job_crud.update_running_payload(
                db,
                uid=job.uid,
                job_id=job.id,
                owner=context.worker_id,
                payload=payload,
                commit=False,
            )
            if updated_job is None:
                raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
            knowledge_base.migration_snapshot_boundary = boundary.logical_boundary
            knowledge_base.migration_cursor = 0
            knowledge_base.migration_total_count = boundary.total_count
            knowledge_base.migration_success_count = 0
            knowledge_base.migration_failure_count = 0
            knowledge_base.migration_error = None
        await db.commit()

    metadata = _collection_metadata(
        knowledge_base_id=job.knowledge_base_id,
        target=target,
    )
    try:
        await async_get_or_create_collection(target["collection"], metadata=metadata)
    except Exception as exc:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc

    await context.checkpoint()
    async with context.session_factory() as db:
        knowledge_base = await lock_migrating_knowledge_base(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
        )
        if knowledge_base is None or knowledge_base.migration_job_id != job.id or knowledge_base.migration_status != KnowledgeBaseMigrationStatus.PREPARING:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        knowledge_base.migration_status = KnowledgeBaseMigrationStatus.BUILDING
        await db.commit()
    return payload
