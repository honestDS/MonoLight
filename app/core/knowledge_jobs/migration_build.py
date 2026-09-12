from __future__ import annotations

from typing import Any

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
    MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
)
from app.core.crud.knowledge.base import knowledge_base_crud
from app.core.crud.knowledge.embedding_transition import (
    knowledge_base_migration_crud,
)
from app.core.embedding.common import embed_texts_with_config
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobExecutionContext,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
)
from app.models.knowledge_base import (
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseMigrationStatus,
)
from app.providers.database.time import get_database_time
from app.providers.vector import async_delete_collection_items, async_get_collection_items, async_upsert_collection_items

from .migration_common import (
    MIGRATION_BATCH_SIZE,
    _plan_record,
    _plan_value,
    _VectorPlan,
)
from .migration_prepare import (
    _load_target_runtime_config,
    lock_migrating_knowledge_base,
)

__all__ = []


async def _upsert_plans(
    context: KnowledgeJobExecutionContext,
    *,
    target: dict[str, Any],
    plans: list[_VectorPlan],
) -> None:
    item_ids: list[str] = []
    chunks: list[str] = []
    metadatas: list[dict[str, Any]] = []
    for plan in plans:
        item_ids.extend(plan.item_ids)
        chunks.extend(plan.chunks)
        metadatas.extend(plan.metadatas)
    if not item_ids:
        return
    config = await _load_target_runtime_config(context, target)
    try:
        embeddings = await embed_texts_with_config(
            config,
            chunks,
            batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
            dimensions=target["dimensions"],
        )
    except Exception as exc:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc
    if len(embeddings) != len(chunks) or any(not vector or len(vector) != target["dimensions"] for vector in embeddings):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    try:
        await async_upsert_collection_items(
            target["collection"],
            item_ids,
            chunks,
            embeddings,
            metadatas,
            batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
        )
    except Exception as exc:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc


async def _build_migration(
    context: KnowledgeJobExecutionContext,
    payload: dict[str, Any],
) -> None:
    job = await context.checkpoint()
    if job.id is None:
        raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
    snapshot = payload.get("snapshot")
    if not isinstance(snapshot, dict):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    document_max_id = int(snapshot.get("document_max_id", -1))
    managed_max_id = int(snapshot.get("managed_max_id", -1))
    if document_max_id < 0 or managed_max_id < 0:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

    while True:
        await context.checkpoint()
        async with context.session_factory() as db:
            knowledge_base = await knowledge_base_crud.get(
                db,
                job.knowledge_base_id,
            )
            if knowledge_base is None or knowledge_base.uid != job.uid or knowledge_base.migration_job_id != job.id:
                raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            if knowledge_base.migration_status != KnowledgeBaseMigrationStatus.BUILDING:
                return
            cursor = knowledge_base.migration_cursor or 0
            records = await knowledge_base_migration_crud.list_snapshot_page(
                db,
                uid=job.uid,
                knowledge_base_id=job.knowledge_base_id,
                document_max_id=document_max_id,
                managed_max_id=managed_max_id,
                cursor=cursor,
                limit=MIGRATION_BATCH_SIZE,
            )
            await db.commit()
        if not records:
            async with context.session_factory() as db:
                locked = await lock_migrating_knowledge_base(
                    db,
                    uid=job.uid,
                    knowledge_base_id=job.knowledge_base_id,
                )
                if locked is None or locked.migration_job_id != job.id or locked.migration_status != KnowledgeBaseMigrationStatus.BUILDING:
                    raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
                locked.migration_status = KnowledgeBaseMigrationStatus.CATCHING_UP
                await db.commit()
            return

        plans = [
            _plan_record(
                job.knowledge_base_id,
                record,
                payload["target"]["revision"],
            )
            for record in records
        ]
        await _upsert_plans(context, target=payload["target"], plans=plans)
        await context.checkpoint()
        next_cursor = records[-1].logical_cursor
        async with context.session_factory() as db:
            locked = await lock_migrating_knowledge_base(
                db,
                uid=job.uid,
                knowledge_base_id=job.knowledge_base_id,
            )
            if locked is None or locked.migration_job_id != job.id or locked.migration_status != KnowledgeBaseMigrationStatus.BUILDING or (locked.migration_cursor or 0) >= next_cursor:
                if locked is not None and locked.migration_job_id == job.id and locked.migration_status == KnowledgeBaseMigrationStatus.BUILDING and (locked.migration_cursor or 0) == next_cursor:
                    await db.commit()
                    continue
                raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            locked.migration_cursor = next_cursor
            locked.migration_success_count += len(records)
            await db.commit()


async def _target_source_item_ids(
    collection_name: str,
    *,
    source_type: KnowledgeBaseMigrationSourceType,
    source_id: int,
) -> list[str]:
    page = await async_get_collection_items(
        collection_name,
        include=["metadatas"],
    )
    item_ids = page.get("ids") or []
    metadatas = page.get("metadatas") or []
    matches: list[str] = []
    for index, item_id in enumerate(item_ids):
        metadata = metadatas[index] if index < len(metadatas) else None
        if not isinstance(metadata, dict):
            continue
        if metadata.get("migration_source_type") == source_type.value and metadata.get("migration_source_id") == source_id:
            matches.append(item_id)
    return matches


async def _apply_delta(
    context: KnowledgeJobExecutionContext,
    *,
    payload: dict[str, Any],
    source_type: KnowledgeBaseMigrationSourceType,
    source_id: int,
) -> None:
    target = payload["target"]
    existing_ids = await _target_source_item_ids(
        target["collection"],
        source_type=source_type,
        source_id=source_id,
    )
    if existing_ids:
        await async_delete_collection_items(
            target["collection"],
            existing_ids,
            batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
        )
    async with context.session_factory() as db:
        value = await knowledge_base_migration_crud.get_source(
            db,
            uid=context.job.uid,
            knowledge_base_id=context.job.knowledge_base_id,
            source_type=source_type,
            source_id=source_id,
        )
        await db.commit()
    if value is None:
        return
    plan = _plan_value(
        context.job.knowledge_base_id,
        source_type,
        value,
        target["revision"],
    )
    await _upsert_plans(context, target=target, plans=[plan])


async def _catch_up_migration(
    context: KnowledgeJobExecutionContext,
    payload: dict[str, Any],
) -> None:
    job = await context.checkpoint()
    if job.id is None:
        raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
    observed_high: int | None = None
    stable_observations = 0
    while stable_observations < 2:
        await context.checkpoint()
        async with context.session_factory() as db:
            knowledge_base = await knowledge_base_crud.get(
                db,
                job.knowledge_base_id,
            )
            if knowledge_base is None or knowledge_base.uid != job.uid or knowledge_base.migration_job_id != job.id or knowledge_base.migration_status != KnowledgeBaseMigrationStatus.CATCHING_UP:
                raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            applied = knowledge_base.migration_delta_applied_watermark
            high = knowledge_base.migration_delta_high_watermark
            if applied > high:
                raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
            if applied < high:
                deltas = await knowledge_base_migration_crud.list_deltas(
                    db,
                    uid=job.uid,
                    migration_job_id=job.id,
                    sequence_start=applied + 1,
                    sequence_end=high,
                    limit=MIGRATION_BATCH_SIZE,
                )
            else:
                deltas = []
            await db.commit()

        if deltas:
            expected = applied + 1
            for delta in deltas:
                if delta.sequence != expected:
                    raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
                try:
                    source_type = KnowledgeBaseMigrationSourceType(delta.source_type)
                    action = KnowledgeBaseMigrationDeltaAction(delta.action)
                except (TypeError, ValueError) as exc:
                    raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc
                if action not in {
                    KnowledgeBaseMigrationDeltaAction.UPSERT,
                    KnowledgeBaseMigrationDeltaAction.DELETE,
                }:
                    raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
                await _apply_delta(
                    context,
                    payload=payload,
                    source_type=source_type,
                    source_id=delta.source_id,
                )
                await context.checkpoint()
                async with context.session_factory() as db:
                    locked = await lock_migrating_knowledge_base(
                        db,
                        uid=job.uid,
                        knowledge_base_id=job.knowledge_base_id,
                    )
                    if locked is None or locked.migration_job_id != job.id or locked.migration_status != KnowledgeBaseMigrationStatus.CATCHING_UP or locked.migration_delta_applied_watermark != delta.sequence - 1:
                        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
                    if not await knowledge_base_migration_crud.mark_delta_applied(
                        db,
                        uid=job.uid,
                        migration_job_id=job.id,
                        sequence=delta.sequence,
                        applied_at=await get_database_time(db),
                    ):
                        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
                    locked.migration_delta_applied_watermark = delta.sequence
                    await db.commit()
                expected += 1
            observed_high = None
            stable_observations = 0
            continue

        if observed_high == high:
            stable_observations += 1
        else:
            observed_high = high
            stable_observations = 1

    async with context.session_factory() as db:
        locked = await lock_migrating_knowledge_base(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
        )
        if locked is None or locked.migration_job_id != job.id or locked.migration_status != KnowledgeBaseMigrationStatus.CATCHING_UP:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if locked.migration_delta_high_watermark != locked.migration_delta_applied_watermark:
            await db.commit()
            return
        locked.migration_status = KnowledgeBaseMigrationStatus.VALIDATING
        await db.commit()
