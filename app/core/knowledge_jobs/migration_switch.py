from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.embedding_transition import (
    knowledge_base_migration_crud,
)
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.embedding.common import embed_texts_with_config
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobCancelledError,
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutionResult,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
)
from app.models.knowledge_base import (
    KnowledgeBaseIndexStatus,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
    KnowledgeJobOperation,
)
from app.providers.database.time import get_database_time
from app.providers.vector import async_get_collection_items, async_query_collection, async_validate_collection

from .migration_common import (
    _collection_metadata,
    _matches_source,
    _matches_target,
    _plan_record,
    _request_hash,
    _ValidationSnapshot,
    _VectorPlan,
)
from .migration_prepare import (
    _load_target_runtime_config,
    lock_migrating_knowledge_base,
)

__all__ = []


async def _current_plans(
    context: KnowledgeJobExecutionContext,
    payload: dict[str, Any],
) -> tuple[list[_VectorPlan], int]:
    source = payload["from"]
    target = payload["target"]
    async with context.session_factory() as db:
        knowledge_base = await knowledge_base_crud.get(
            db,
            context.job.knowledge_base_id,
        )
        if (
            knowledge_base is None
            or knowledge_base.uid != context.job.uid
            or knowledge_base.migration_job_id != context.job.id
            or knowledge_base.migration_status != KnowledgeBaseMigrationStatus.VALIDATING
            or not _matches_source(knowledge_base, source)
            or not _matches_target(knowledge_base, target)
            or knowledge_base.migration_delta_high_watermark != knowledge_base.migration_delta_applied_watermark
        ):
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        delta_watermark = knowledge_base.migration_delta_high_watermark
        records = await knowledge_base_migration_crud.list_current_sources(
            db,
            uid=context.job.uid,
            knowledge_base_id=context.job.knowledge_base_id,
        )
        await db.commit()
    plans = [
        _plan_record(
            context.job.knowledge_base_id,
            record,
            target["revision"],
        )
        for record in records
    ]
    return plans, delta_watermark


def _flatten_plan_items(
    plans: list[_VectorPlan] | tuple[_VectorPlan, ...],
) -> dict[str, tuple[str, dict[str, Any]]]:
    items: dict[str, tuple[str, dict[str, Any]]] = {}
    for plan in plans:
        for item_id, chunk, metadata in zip(
            plan.item_ids,
            plan.chunks,
            plan.metadatas,
            strict=True,
        ):
            items[item_id] = (chunk, metadata)
    return items


async def _validate_migration(
    context: KnowledgeJobExecutionContext,
    payload: dict[str, Any],
) -> _ValidationSnapshot:
    plans, delta_watermark = await _current_plans(context, payload)
    expected = _flatten_plan_items(plans)
    target = payload["target"]
    metadata = _collection_metadata(
        knowledge_base_id=context.job.knowledge_base_id,
        target=target,
    )
    validation = await async_validate_collection(
        target["collection"],
        expected_count=len(expected),
        expected_metadata=metadata,
        expected_dimension=target["dimensions"] if expected else None,
        sample_size=min(max(len(expected), 1), 5),
    )
    if not getattr(validation, "valid", False):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

    page = await async_get_collection_items(
        target["collection"],
        include=["documents", "metadatas"],
    )
    actual_ids = list(page.get("ids") or [])
    documents = list(page.get("documents") or [])
    metadatas = list(page.get("metadatas") or [])
    if set(actual_ids) != set(expected):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    for index, item_id in enumerate(actual_ids):
        expected_document, expected_metadata = expected[item_id]
        actual_document = documents[index] if index < len(documents) else None
        actual_metadata = metadatas[index] if index < len(metadatas) else None
        if actual_document != expected_document or actual_metadata != expected_metadata:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

    if expected:
        first_item_id = next(iter(expected))
        sample_text = expected[first_item_id][0]
        config = await _load_target_runtime_config(context, target)
        embeddings = await embed_texts_with_config(
            config,
            [sample_text],
            batch_size=1,
            dimensions=target["dimensions"],
        )
        if len(embeddings) != 1 or len(embeddings[0]) != target["dimensions"]:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        query = await async_query_collection(
            target["collection"],
            embeddings[0],
            n_results=1,
            include=["documents", "metadatas", "distances"],
        )
        query_ids = query.get("ids") or []
        top_ids = query_ids[0] if query_ids and isinstance(query_ids[0], list) else []
        if not top_ids or top_ids[0] not in expected:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    return _ValidationSnapshot(
        plans=tuple(plans),
        count=len(expected),
        delta_watermark=delta_watermark,
    )


async def _switch_migration(
    context: KnowledgeJobExecutionContext,
    payload: dict[str, Any],
    validation: _ValidationSnapshot,
) -> KnowledgeJobExecutionResult:
    job = await context.checkpoint()
    if job.id is None:
        raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
    source = payload["from"]
    target = payload["target"]
    document_updates = [(plan.source_id, list(plan.item_ids)) for plan in validation.plans if plan.source_type == KnowledgeBaseMigrationSourceType.USER_DOCUMENT]
    managed_updates = [(plan.source_id, plan.source_version, list(plan.item_ids)) for plan in validation.plans if plan.source_type == KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE and plan.source_version is not None]
    async with context.session_factory() as db:
        claim = await knowledge_job_crud.get_active_claim(
            db,
            uid=job.uid,
            job_id=job.id,
            owner=context.worker_id,
        )
        if claim is None:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        if claim.cancel_requested_at is not None:
            raise KnowledgeJobCancelledError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        knowledge_base = await lock_migrating_knowledge_base(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
        )
        if knowledge_base is None or knowledge_base.migration_job_id != job.id or knowledge_base.migration_status != KnowledgeBaseMigrationStatus.VALIDATING or not _matches_source(knowledge_base, source) or not _matches_target(knowledge_base, target):
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if knowledge_base.migration_delta_high_watermark != validation.delta_watermark or knowledge_base.migration_delta_applied_watermark != validation.delta_watermark:
            knowledge_base.migration_status = KnowledgeBaseMigrationStatus.CATCHING_UP
            await db.commit()
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

        knowledge_base.migration_status = KnowledgeBaseMigrationStatus.SWITCHING
        if not await knowledge_base_migration_crud.update_document_vectors_batch(
            db,
            knowledge_base_id=job.knowledge_base_id,
            updates=document_updates,
        ):
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if not await knowledge_base_migration_crud.update_managed_vectors_batch(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
            updates=managed_updates,
        ):
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

        cleanup_request = {
            "knowledge_base_id": job.knowledge_base_id,
            "migration_job_id": job.id,
            "collection": source["collection"],
        }
        cleanup_available_at = await get_database_time(db)
        cleanup_job, _ = await knowledge_job_crud.create(
            db,
            uid=job.uid,
            parent_job_id=job.id,
            operation=KnowledgeJobOperation.OLD_COLLECTION_CLEANUP,
            dedupe_key=f"kb-old-collection-cleanup:{job.id}",
            request_hash=_request_hash(cleanup_request),
            active_change_key=None,
            knowledge_base_id=job.knowledge_base_id,
            payload=cleanup_request,
            available_at=cleanup_available_at,
            max_attempts=3,
            commit=False,
        )
        if cleanup_job.id is None:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

        now = await get_database_time(db)
        knowledge_base.active_embedding_channel_id = target["channel_id"]
        knowledge_base.active_embedding_model_id = target["model_id"]
        knowledge_base.active_embedding_dimensions = target["dimensions"]
        knowledge_base.active_embedding_signature = target["signature"]
        knowledge_base.active_embedding_revision = target["revision"]
        knowledge_base.active_collection_name = target["collection"]
        knowledge_base.index_revision += 1
        knowledge_base.index_status = KnowledgeBaseIndexStatus.READY
        knowledge_base.target_embedding_channel_id = None
        knowledge_base.target_embedding_model_id = None
        knowledge_base.target_embedding_dimensions = None
        knowledge_base.target_embedding_signature = None
        knowledge_base.target_embedding_revision = None
        knowledge_base.target_collection_name = None
        knowledge_base.migration_status = KnowledgeBaseMigrationStatus.SUCCEEDED
        knowledge_base.migration_error = None
        knowledge_base.migration_finished_at = now
        knowledge_base.old_collection_name = source["collection"]
        knowledge_base.old_collection_cleanup_status = KnowledgeBaseOldCollectionCleanupStatus.PENDING
        knowledge_base.old_collection_cleanup_job_id = cleanup_job.id
        knowledge_base.old_collection_cleanup_error = None
        knowledge_base.old_collection_cleanup_at = None

        result = {
            "knowledge_base_id": job.knowledge_base_id,
            "collection": target["collection"],
            "revision": target["revision"],
            "count": validation.count,
        }
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
