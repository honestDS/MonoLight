from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_FIELD_INVALID,
    ERR_KNOWLEDGE_JOB_FIELD_REQUIRED,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
    MANAGED_KNOWLEDGE_VECTOR_CHUNK_OVERLAP,
    MANAGED_KNOWLEDGE_VECTOR_CHUNK_SIZE,
)
from app.core.crud.knowledge.embedding_transition import (
    KnowledgeMigrationSnapshotRecord,
)
from app.core.embedding.knowledge_base_runtime import (
    KnowledgeBaseEmbeddingSnapshot,
    resolve_active_knowledge_base_embedding,
)
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobRetryableError,
)
from app.core.knowledge_jobs.manager import (
    KnowledgeJobValidationError,
)
from app.core.log import get_logger
from app.core.utils.text_splitter import TextSplitter
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseDocument,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
    KnowledgeJob,
    ManagedKnowledgeItem,
)

__all__ = [
    "MIGRATION_BATCH_SIZE",
]

MIGRATION_BATCH_SIZE = 20

logger = get_logger(__name__)

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
