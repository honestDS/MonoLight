from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
    KNOWLEDGE_MAINTENANCE_BATCH_SIZE,
    KNOWLEDGE_ORGANIZATION_HISTORY_RETENTION_SECONDS,
)
from app.core.crud.knowledge.base import knowledge_base_crud, knowledge_base_document_crud
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import managed_knowledge_item_crud
from app.core.crud.knowledge.organization import knowledge_organization_stage_crud
from app.core.embedding.common import build_embedding_signature
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobDeterministicError,
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutionResult,
    KnowledgeJobRetryableError,
)
from app.core.knowledge_jobs.manager import KnowledgeJobConflictError, KnowledgeJobTargetBusyError
from app.core.knowledge_jobs.migration_common import _ACTIVE_MIGRATION_STATUSES, _request_hash
from app.core.knowledge_jobs.migration_prepare import prepare_knowledge_base_embedding_migration
from app.models.knowledge_base import KnowledgeBase, KnowledgeJob, KnowledgeJobOperation
from app.providers.database.time import get_database_time
from app.providers.vector import (
    async_delete_collection_items,
    async_get_collection_items,
    async_get_collection_items_by_ids,
    async_validate_collection,
)


async def submit_startup_knowledge_maintenance_jobs(
    db: AsyncSession,
    *,
    batch_size: int = KNOWLEDGE_MAINTENANCE_BATCH_SIZE,
) -> int:
    now = await get_database_time(db)
    startup_token = uuid4().hex
    created_count = 0
    offset = 0
    while True:
        knowledge_bases, total = await knowledge_base_crud.list_page(
            db,
            uid=None,
            skip=offset,
            limit=batch_size,
        )
        if not knowledge_bases:
            break
        for knowledge_base in knowledge_bases:
            if knowledge_base.id is None:
                continue
            active_change_key = f"kb-maintenance:{knowledge_base.id}"
            if (
                await knowledge_job_crud.get_by_active_change_key(
                    db,
                    uid=knowledge_base.uid,
                    active_change_key=active_change_key,
                )
                is not None
            ):
                continue
            request = {
                "knowledge_base_id": knowledge_base.id,
                "trigger": "worker_startup",
            }
            try:
                _job, created = await knowledge_job_crud.create(
                    db,
                    uid=knowledge_base.uid,
                    operation=KnowledgeJobOperation.KNOWLEDGE_MAINTENANCE,
                    dedupe_key=f"knowledge-maintenance:{knowledge_base.id}:startup:{startup_token}",
                    request_hash=_request_hash(request),
                    active_change_key=active_change_key,
                    knowledge_base_id=knowledge_base.id,
                    payload=request,
                    available_at=now,
                    max_attempts=3,
                    commit=False,
                )
            except IntegrityError:
                continue
            if created:
                created_count += 1
        offset += len(knowledge_bases)
        if offset >= total:
            break
    await db.flush()
    return created_count


async def _cleanup_expired_organization_history(
    context: KnowledgeJobExecutionContext,
    *,
    uid: str,
    knowledge_base_id: int,
) -> int:
    async with context.session_factory() as db:
        now = await get_database_time(db)
        await db.commit()
    cutoff = now - timedelta(seconds=KNOWLEDGE_ORGANIZATION_HISTORY_RETENTION_SECONDS)
    deleted_total = 0
    while True:
        await context.checkpoint()
        async with context.session_factory() as db:
            deleted = await knowledge_organization_stage_crud.cleanup_expired(
                db,
                before=cutoff,
                batch_size=KNOWLEDGE_MAINTENANCE_BATCH_SIZE,
                uid=uid,
                knowledge_base_id=knowledge_base_id,
            )
        deleted_total += deleted
        if deleted < KNOWLEDGE_MAINTENANCE_BATCH_SIZE:
            return deleted_total


async def _assert_consistency_fence(
    context: KnowledgeJobExecutionContext,
    *,
    knowledge_base: KnowledgeBase,
) -> None:
    await context.checkpoint()
    baseline_active = resolve_active_knowledge_base_embedding(knowledge_base)
    async with context.session_factory() as db:
        current = await knowledge_base_crud.get(db, knowledge_base.id)
        has_other_active_job = await knowledge_job_crud.has_active_for_knowledge_base_except(
            db,
            uid=knowledge_base.uid,
            knowledge_base_id=knowledge_base.id,
            excluded_job_id=context.job.id,
        )
        await db.commit()
    if current is None or current.uid != knowledge_base.uid or has_other_active_job:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    current_active = resolve_active_knowledge_base_embedding(current)
    if current.migration_status in _ACTIVE_MIGRATION_STATUSES or current.index_revision != knowledge_base.index_revision or current.active_embedding_revision != knowledge_base.active_embedding_revision or current_active != baseline_active:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))


def _managed_vector_is_current(item: Any, item_id: str, metadata: dict[str, Any]) -> bool:
    if item is None or item.deleted_at is not None or item.is_recallable is not True:
        return False
    version = metadata.get("managed_knowledge_version")
    return isinstance(version, int) and not isinstance(version, bool) and item.version == version and item.indexed_version == version and item_id in set(item.vector_item_ids or ())


def _document_vector_is_current(document: Any, item_id: str) -> bool:
    return document is not None and item_id in set(document.chunk_ids or ())


async def _classify_vector_page(
    db: AsyncSession,
    *,
    knowledge_base: KnowledgeBase,
    item_ids: list[str],
    metadatas: list[Any],
) -> tuple[list[str], bool]:
    document_ids: set[int] = set()
    document_uuids: set[str] = set()
    managed_ids: set[int] = set()
    for metadata in metadatas:
        if not isinstance(metadata, dict):
            continue
        document_id = metadata.get("document_id")
        document_uuid = metadata.get("document_uuid")
        managed_id = metadata.get("managed_knowledge_id")
        if isinstance(document_id, int) and not isinstance(document_id, bool) and document_id > 0:
            document_ids.add(document_id)
        if isinstance(document_uuid, str) and document_uuid.strip():
            document_uuids.add(document_uuid.strip())
        if isinstance(managed_id, int) and not isinstance(managed_id, bool) and managed_id > 0:
            managed_ids.add(managed_id)

    documents = await knowledge_base_document_crud.list_by_recall_references(
        db,
        knowledge_base_id=knowledge_base.id,
        document_ids=document_ids,
        document_uuids=document_uuids,
    )
    managed = await managed_knowledge_item_crud.get_by_ids(
        db,
        uid=knowledge_base.uid,
        knowledge_base_id=knowledge_base.id,
        knowledge_ids=managed_ids,
    )
    documents_by_id = {item.id: item for item in documents if item.id is not None}
    documents_by_uuid = {metadata["document_uuid"]: item for item in documents if isinstance((metadata := item.metadata_), dict) and isinstance(metadata.get("document_uuid"), str) and metadata["document_uuid"]}
    managed_by_id = {item.id: item for item in managed if item.id is not None}

    orphan_ids: list[str] = []
    unknown_metadata = False
    for index, item_id in enumerate(item_ids):
        metadata = metadatas[index] if index < len(metadatas) else None
        if not isinstance(metadata, dict):
            unknown_metadata = True
            continue
        if metadata.get("knowledge_base_id") not in {None, knowledge_base.id}:
            orphan_ids.append(item_id)
            continue
        managed_id = metadata.get("managed_knowledge_id")
        if isinstance(managed_id, int) and not isinstance(managed_id, bool) and managed_id > 0:
            if not _managed_vector_is_current(managed_by_id.get(managed_id), item_id, metadata):
                orphan_ids.append(item_id)
            continue
        document_id = metadata.get("document_id")
        if isinstance(document_id, int) and not isinstance(document_id, bool) and document_id > 0:
            if not _document_vector_is_current(documents_by_id.get(document_id), item_id):
                orphan_ids.append(item_id)
            continue
        document_uuid = metadata.get("document_uuid")
        if isinstance(document_uuid, str) and document_uuid.strip():
            if not _document_vector_is_current(documents_by_uuid.get(document_uuid.strip()), item_id):
                orphan_ids.append(item_id)
            continue
        unknown_metadata = True
    return orphan_ids, unknown_metadata


async def _cleanup_orphan_vectors(
    context: KnowledgeJobExecutionContext,
    *,
    knowledge_base: KnowledgeBase,
    collection_name: str,
) -> tuple[int, bool]:
    offset = 0
    deleted_total = 0
    unknown_metadata = False
    while True:
        await _assert_consistency_fence(context, knowledge_base=knowledge_base)
        try:
            page = await async_get_collection_items(
                collection_name,
                offset=offset,
                limit=KNOWLEDGE_MAINTENANCE_BATCH_SIZE,
                include=["metadatas"],
            )
        except Exception as exc:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc
        item_ids = list(page.get("ids") or [])
        metadatas = list(page.get("metadatas") or [])
        if not item_ids:
            break
        async with context.session_factory() as db:
            orphan_ids, page_unknown = await _classify_vector_page(
                db,
                knowledge_base=knowledge_base,
                item_ids=item_ids,
                metadatas=metadatas,
            )
            await db.commit()
        unknown_metadata = unknown_metadata or page_unknown
        if orphan_ids:
            await _assert_consistency_fence(context, knowledge_base=knowledge_base)
            try:
                await async_delete_collection_items(
                    collection_name,
                    orphan_ids,
                    batch_size=KNOWLEDGE_MAINTENANCE_BATCH_SIZE,
                )
            except Exception as exc:
                raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc
            deleted_total += len(orphan_ids)
        offset += len(item_ids) - len(orphan_ids)
        if len(item_ids) < KNOWLEDGE_MAINTENANCE_BATCH_SIZE and not orphan_ids:
            break
    return deleted_total, unknown_metadata


async def _relation_vector_references(
    context: KnowledgeJobExecutionContext,
    *,
    knowledge_base: KnowledgeBase,
) -> tuple[int, bool]:
    expected_count = 0
    missing = False
    collection_name = resolve_active_knowledge_base_embedding(knowledge_base).collection_name

    offset = 0
    while True:
        await _assert_consistency_fence(context, knowledge_base=knowledge_base)
        async with context.session_factory() as db:
            documents, total = await knowledge_base_document_crud.list_page(
                db,
                knowledge_base_id=knowledge_base.id,
                skip=offset,
                limit=KNOWLEDGE_MAINTENANCE_BATCH_SIZE,
            )
            await db.commit()
        if not documents:
            break
        for document in documents:
            references = list(document.chunk_ids or ())
            expected_count += len(references)
            for start in range(0, len(references), KNOWLEDGE_MAINTENANCE_BATCH_SIZE):
                batch = references[start : start + KNOWLEDGE_MAINTENANCE_BATCH_SIZE]
                await context.checkpoint()
                try:
                    found = await async_get_collection_items_by_ids(collection_name, batch, include=["metadatas"])
                except Exception as exc:
                    raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc
                if set(found.get("ids") or []) != set(batch):
                    missing = True
        offset += len(documents)
        if offset >= total:
            break

    offset = 0
    while True:
        await _assert_consistency_fence(context, knowledge_base=knowledge_base)
        async with context.session_factory() as db:
            items, total = await managed_knowledge_item_crud.list_page(
                db,
                uid=knowledge_base.uid,
                knowledge_base_id=knowledge_base.id,
                skip=offset,
                limit=KNOWLEDGE_MAINTENANCE_BATCH_SIZE,
            )
            await db.commit()
        if not items:
            break
        for item in items:
            if item.is_recallable is not True or item.indexed_version != item.version:
                continue
            references = list(item.vector_item_ids or ())
            expected_count += len(references)
            for start in range(0, len(references), KNOWLEDGE_MAINTENANCE_BATCH_SIZE):
                batch = references[start : start + KNOWLEDGE_MAINTENANCE_BATCH_SIZE]
                await context.checkpoint()
                try:
                    found = await async_get_collection_items_by_ids(collection_name, batch, include=["metadatas"])
                except Exception as exc:
                    raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc
                if set(found.get("ids") or []) != set(batch):
                    missing = True
        offset += len(items)
        if offset >= total:
            break
    return expected_count, missing


async def _submit_rebuild(
    context: KnowledgeJobExecutionContext,
    *,
    knowledge_base: KnowledgeBase,
) -> KnowledgeJob:
    await _assert_consistency_fence(context, knowledge_base=knowledge_base)
    active = resolve_active_knowledge_base_embedding(knowledge_base)
    if active.dimensions is None or active.dimensions < 1:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    signature = knowledge_base.active_embedding_signature or build_embedding_signature(
        active.channel_id,
        active.model_id,
        active.dimensions,
    )
    async with context.session_factory() as db:
        if await knowledge_job_crud.has_active_for_knowledge_base_except(
            db,
            uid=knowledge_base.uid,
            knowledge_base_id=knowledge_base.id,
            excluded_job_id=context.job.id,
        ):
            await db.commit()
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        current = await knowledge_base_crud.get(db, knowledge_base.id)
        if current is None or current.uid != knowledge_base.uid or current.index_revision != knowledge_base.index_revision:
            await db.commit()
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        try:
            rebuild = await prepare_knowledge_base_embedding_migration(
                db,
                uid=knowledge_base.uid,
                knowledge_base_id=knowledge_base.id,
                target_channel_id=active.channel_id,
                target_model_id=active.model_id,
                target_dimensions=active.dimensions,
                target_signature=signature,
                dedupe_key=f"kb-consistency-rebuild:{knowledge_base.id}:index:{knowledge_base.index_revision}",
                commit=False,
            )
        except (KnowledgeJobConflictError, KnowledgeJobTargetBusyError) as exc:
            await db.rollback()
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT)) from exc
        await db.commit()
        return rebuild


def _collection_validation_failed(validation: Any, *, expected_count: int | None = None) -> bool:
    if not getattr(validation, "exists", False):
        return True
    errors = tuple(getattr(validation, "errors", ()) or ())
    if expected_count == 0:
        errors = tuple(error for error in errors if error != "sample_dimension_missing")
    elif expected_count is None and getattr(validation, "count", None) == 0:
        errors = tuple(error for error in errors if error != "sample_dimension_missing")
    return bool(errors)


async def handle_knowledge_maintenance(
    context: KnowledgeJobExecutionContext,
) -> KnowledgeJobExecutionResult:
    job = await context.checkpoint()
    if job.id is None:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

    history_deleted = await _cleanup_expired_organization_history(
        context,
        uid=job.uid,
        knowledge_base_id=job.knowledge_base_id,
    )
    async with context.session_factory() as db:
        knowledge_base = await knowledge_base_crud.get(db, job.knowledge_base_id)
        if knowledge_base is None or knowledge_base.uid != job.uid or knowledge_base.id is None:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if await knowledge_job_crud.has_active_for_knowledge_base_except(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
            excluded_job_id=job.id,
        ):
            await db.commit()
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        if knowledge_base.migration_status in _ACTIVE_MIGRATION_STATUSES:
            await db.commit()
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        active = resolve_active_knowledge_base_embedding(knowledge_base)
        await db.commit()

    await context.checkpoint()
    validation = await async_validate_collection(
        active.collection_name,
        expected_dimension=active.dimensions,
    )
    inconsistent = _collection_validation_failed(validation)
    orphan_deleted = 0
    unknown_metadata = False
    if validation.exists:
        orphan_deleted, unknown_metadata = await _cleanup_orphan_vectors(
            context,
            knowledge_base=knowledge_base,
            collection_name=active.collection_name,
        )
        expected_count, missing_references = await _relation_vector_references(
            context,
            knowledge_base=knowledge_base,
        )
        await _assert_consistency_fence(context, knowledge_base=knowledge_base)
        final_validation = await async_validate_collection(
            active.collection_name,
            expected_count=expected_count,
            expected_dimension=active.dimensions,
        )
        inconsistent = inconsistent or unknown_metadata or missing_references or _collection_validation_failed(final_validation, expected_count=expected_count)

    rebuild = await _submit_rebuild(context, knowledge_base=knowledge_base) if inconsistent else None
    return KnowledgeJobExecutionResult(
        result={
            "history_deleted": history_deleted,
            "orphan_vectors_deleted": orphan_deleted,
            "consistent": not inconsistent,
            "rebuild_job_id": rebuild.id if rebuild is not None else None,
        }
    )


__all__ = ["handle_knowledge_maintenance", "submit_startup_knowledge_maintenance_jobs"]
