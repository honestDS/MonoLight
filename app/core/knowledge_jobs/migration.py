from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_ACTIVE_TARGET_BUSY,
    ERR_KNOWLEDGE_JOB_DEDUPE_CONFLICT,
    ERR_KNOWLEDGE_JOB_FIELD_INVALID,
    ERR_KNOWLEDGE_JOB_FIELD_REQUIRED,
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
    MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
    MANAGED_KNOWLEDGE_VECTOR_CHUNK_OVERLAP,
    MANAGED_KNOWLEDGE_VECTOR_CHUNK_SIZE,
)
from app.core.crud.knowledge_base import knowledge_base_crud
from app.core.crud.knowledge_embedding_migration import (
    KnowledgeMigrationSnapshotRecord,
    knowledge_base_migration_crud,
)
from app.core.crud.knowledge_job import KnowledgeJobCancelResult, knowledge_job_crud
from app.core.embedding.common import (
    embed_texts_with_config,
    load_embedding_runtime_config,
)
from app.core.embedding.knowledge_base_runtime import (
    KnowledgeBaseEmbeddingSnapshot,
    resolve_active_knowledge_base_embedding,
)
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobCancelledError,
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutionResult,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
)
from app.core.knowledge_jobs.manager import (
    KnowledgeJobConflictError,
    KnowledgeJobTargetBusyError,
    KnowledgeJobValidationError,
)
from app.core.utils.database_integrity import is_unique_constraint_violation
from app.core.utils.text_splitter import TextSplitter
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeItem,
)
from app.providers.database.time import get_database_time
from app.providers.vector import (
    async_delete_collection,
    async_delete_collection_items,
    async_get_collection_items,
    async_get_or_create_collection,
    async_query_collection,
    async_upsert_collection_items,
    async_validate_collection,
)

MIGRATION_BATCH_SIZE = 20
_ACTIVE_MIGRATION_STATUSES = frozenset(
    {
        KnowledgeBaseMigrationStatus.PREPARING,
        KnowledgeBaseMigrationStatus.BUILDING,
        KnowledgeBaseMigrationStatus.CATCHING_UP,
        KnowledgeBaseMigrationStatus.VALIDATING,
        KnowledgeBaseMigrationStatus.SWITCHING,
    }
)
_PRE_SWITCH_MIGRATION_STATUSES = frozenset(
    {
        KnowledgeBaseMigrationStatus.PREPARING,
        KnowledgeBaseMigrationStatus.BUILDING,
        KnowledgeBaseMigrationStatus.CATCHING_UP,
        KnowledgeBaseMigrationStatus.VALIDATING,
    }
)
_BLOCKING_OLD_COLLECTION_CLEANUP_STATUSES = frozenset(
    {
        KnowledgeBaseOldCollectionCleanupStatus.PENDING,
        KnowledgeBaseOldCollectionCleanupStatus.RUNNING,
        KnowledgeBaseOldCollectionCleanupStatus.FAILED,
    }
)


@dataclass(frozen=True, slots=True)
class _VectorPlan:
    source_type: KnowledgeBaseMigrationSourceType
    source_id: int
    source_version: int | None
    item_ids: tuple[str, ...]
    chunks: tuple[str, ...]
    metadatas: tuple[dict[str, Any], ...]


@dataclass(frozen=True, slots=True)
class _ValidationSnapshot:
    plans: tuple[_VectorPlan, ...]
    count: int
    delta_watermark: int


def _require_string(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise KnowledgeJobValidationError(t(ERR_KNOWLEDGE_JOB_FIELD_REQUIRED, field=field))
    return value.strip()


def _positive_int(value: Any, *, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise KnowledgeJobValidationError(t(ERR_KNOWLEDGE_JOB_FIELD_INVALID, field=field))
    return value


def _request_hash(request: dict[str, Any]) -> str:
    try:
        canonical = json.dumps(
            request,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise KnowledgeJobValidationError(t(ERR_KNOWLEDGE_JOB_FIELD_INVALID, field="request")) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _source_payload(
    knowledge_base: KnowledgeBase,
    active: KnowledgeBaseEmbeddingSnapshot,
) -> dict[str, Any]:
    return {
        "channel_id": active.channel_id,
        "model_id": active.model_id,
        "dimensions": active.dimensions,
        "signature": knowledge_base.active_embedding_signature,
        "revision": knowledge_base.active_embedding_revision,
        "collection": active.collection_name,
        "index_revision": knowledge_base.index_revision,
    }


def _target_payload(
    *,
    channel_id: int,
    model_id: str,
    dimensions: int,
    signature: str,
    revision: int,
    collection: str,
) -> dict[str, Any]:
    return {
        "channel_id": channel_id,
        "model_id": model_id,
        "dimensions": dimensions,
        "signature": signature,
        "revision": revision,
        "collection": collection,
    }


def _validate_payload(job: KnowledgeJob) -> dict[str, Any]:
    payload = job.payload
    if not isinstance(payload, dict):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    source = payload.get("from")
    target = payload.get("target")
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    required_source = (
        "channel_id",
        "model_id",
        "dimensions",
        "revision",
        "collection",
        "index_revision",
    )
    required_target = (
        "channel_id",
        "model_id",
        "dimensions",
        "signature",
        "revision",
        "collection",
    )
    if any(source.get(key) is None for key in required_source):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    if any(target.get(key) is None for key in required_target):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    return dict(payload)


def _matches_source(knowledge_base: KnowledgeBase, source: dict[str, Any]) -> bool:
    active = resolve_active_knowledge_base_embedding(knowledge_base)
    return (
        active.channel_id == source["channel_id"]
        and active.model_id == source["model_id"]
        and active.dimensions == source["dimensions"]
        and active.collection_name == source["collection"]
        and knowledge_base.active_embedding_revision == source["revision"]
        and knowledge_base.index_revision == source["index_revision"]
        and knowledge_base.active_embedding_signature == source.get("signature")
    )


def _matches_target(knowledge_base: KnowledgeBase, target: dict[str, Any]) -> bool:
    return (
        knowledge_base.target_embedding_channel_id == target["channel_id"]
        and knowledge_base.target_embedding_model_id == target["model_id"]
        and knowledge_base.target_embedding_dimensions == target["dimensions"]
        and knowledge_base.target_embedding_signature == target["signature"]
        and knowledge_base.target_embedding_revision == target["revision"]
        and knowledge_base.target_collection_name == target["collection"]
    )


def _collection_metadata(
    *,
    knowledge_base_id: int,
    target: dict[str, Any],
) -> dict[str, Any]:
    return {
        "knowledge_base_id": knowledge_base_id,
        "embedding_signature": target["signature"],
        "embedding_revision": target["revision"],
        "purpose": "migration",
    }


def _document_plan(
    knowledge_base_id: int,
    document: KnowledgeBaseDocument,
    target_revision: int,
) -> _VectorPlan:
    if document.id is None:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    chunks = TextSplitter(
        chunk_size=document.chunk_size,
        chunk_overlap=document.chunk_overlap,
    ).split(document.content)
    item_ids = tuple(f"kbm_doc_{knowledge_base_id}_{document.id}_r{target_revision}_chunk_{index}" for index in range(len(chunks)))
    metadatas = tuple(
        {
            "knowledge_type": "user_document",
            "knowledge_base_id": knowledge_base_id,
            "document_id": document.id,
            "filename": document.filename,
            "chunk_index": index,
            "chunk_count": len(chunks),
            "migration_source_type": KnowledgeBaseMigrationSourceType.USER_DOCUMENT.value,
            "migration_source_id": document.id,
        }
        for index in range(len(chunks))
    )
    return _VectorPlan(
        source_type=KnowledgeBaseMigrationSourceType.USER_DOCUMENT,
        source_id=document.id,
        source_version=None,
        item_ids=item_ids,
        chunks=tuple(chunks),
        metadatas=metadatas,
    )


def _managed_plan(
    knowledge_base_id: int,
    item: ManagedKnowledgeItem,
    target_revision: int,
) -> _VectorPlan:
    if item.id is None:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    chunks = TextSplitter(
        chunk_size=MANAGED_KNOWLEDGE_VECTOR_CHUNK_SIZE,
        chunk_overlap=MANAGED_KNOWLEDGE_VECTOR_CHUNK_OVERLAP,
    ).split(item.content)
    item_ids = tuple(f"kbm_managed_{knowledge_base_id}_{item.id}_v{item.version}_r{target_revision}_chunk_{index}" for index in range(len(chunks)))
    metadatas = tuple(
        {
            "knowledge_type": "managed",
            "knowledge_base_id": knowledge_base_id,
            "managed_knowledge_id": item.id,
            "managed_knowledge_version": item.version,
            "chunk_index": index,
            "chunk_count": len(chunks),
            "migration_source_type": KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE.value,
            "migration_source_id": item.id,
        }
        for index in range(len(chunks))
    )
    return _VectorPlan(
        source_type=KnowledgeBaseMigrationSourceType.MANAGED_KNOWLEDGE,
        source_id=item.id,
        source_version=item.version,
        item_ids=item_ids,
        chunks=tuple(chunks),
        metadatas=metadatas,
    )


def _plan_record(
    knowledge_base_id: int,
    record: KnowledgeMigrationSnapshotRecord,
    target_revision: int,
) -> _VectorPlan:
    if record.source_type == KnowledgeBaseMigrationSourceType.USER_DOCUMENT:
        if not isinstance(record.value, KnowledgeBaseDocument):
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        return _document_plan(knowledge_base_id, record.value, target_revision)
    if not isinstance(record.value, ManagedKnowledgeItem):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    return _managed_plan(knowledge_base_id, record.value, target_revision)


def _plan_value(
    knowledge_base_id: int,
    source_type: KnowledgeBaseMigrationSourceType,
    value: KnowledgeBaseDocument | ManagedKnowledgeItem,
    target_revision: int,
) -> _VectorPlan:
    if source_type == KnowledgeBaseMigrationSourceType.USER_DOCUMENT:
        if not isinstance(value, KnowledgeBaseDocument):
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        return _document_plan(knowledge_base_id, value, target_revision)
    if not isinstance(value, ManagedKnowledgeItem):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    return _managed_plan(knowledge_base_id, value, target_revision)


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
        knowledge_base = await knowledge_base_crud.get(db, knowledge_base_id)
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
    except Exception:
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


__all__ = [
    "cancel_knowledge_base_embedding_migration",
    "cleanup_terminal_target_collection",
    "finalize_knowledge_migration_terminal_state",
    "handle_embedding_migration",
    "handle_old_collection_cleanup",
    "lock_migrating_knowledge_base",
    "prepare_knowledge_base_embedding_migration",
]
