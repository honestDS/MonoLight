from __future__ import annotations

import math
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_EMBEDDING_DIMENSION_INVALID,
    ERR_MEMORY_EMBEDDING_VECTOR_INVALID,
    ERR_MEMORY_NOT_CONFIGURED,
    ERR_MEMORY_RECALL_UNAVAILABLE,
    ERR_VALUE_MUST_BE_BETWEEN,
    LOG_MEMORY_RECALL_TOUCH_FAILED,
    MEMORY_CONTENT_MAX_CHARS,
)
from app.core.crud.memory.store import (
    memory_record_crud,
    memory_store_crud,
)
from app.core.embedding.common import embed_texts_with_config, load_embedding_runtime_config
from app.core.i18n import t
from app.core.memory.errors import MemoryConflictError, MemoryValidationError
from app.core.memory.normalization import (
    _normalize_uid,
    _require_positive,
    normalize_memory_content,
)
from app.core.memory.results import (
    MemoryRecallItem,
    MemoryRecallResult,
    MemoryRecallStatus,
)

from .service_common import (
    _enum_value,
    _hybrid_query_collection,
    _validate_active_store,
    logger,
)

__all__ = [
    "LongTermMemoryRecall",
]


class LongTermMemoryRecall:
    async def recall(
        self,
        db: AsyncSession,
        uid: str,
        query: str,
        top_k: int = 5,
        candidate_k: int = 10,
        result_max_chars: int = 4000,
    ) -> MemoryRecallResult:
        normalized_uid = _normalize_uid(uid)
        normalized_query = normalize_memory_content(query)
        normalized_top_k = _require_positive(top_k, field="top_k")
        if normalized_top_k > 50:
            raise MemoryValidationError(ERR_VALUE_MUST_BE_BETWEEN, params={"field": "top_k", "minimum": 1, "maximum": 50})
        normalized_candidate_k = _require_positive(candidate_k, field="candidate_k")
        if normalized_candidate_k > 100:
            raise MemoryValidationError(ERR_VALUE_MUST_BE_BETWEEN, params={"field": "candidate_k", "minimum": 1, "maximum": 100})
        if normalized_candidate_k < normalized_top_k:
            raise MemoryValidationError(
                ERR_VALUE_MUST_BE_BETWEEN,
                params={"field": "candidate_k", "minimum": normalized_top_k, "maximum": 100},
            )
        normalized_result_max_chars = _require_positive(result_max_chars, field="result_max_chars")
        if not 256 <= normalized_result_max_chars <= MEMORY_CONTENT_MAX_CHARS:
            raise MemoryValidationError(
                ERR_VALUE_MUST_BE_BETWEEN,
                params={"field": "result_max_chars", "minimum": 256, "maximum": MEMORY_CONTENT_MAX_CHARS},
            )

        store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
        if store is None:
            return MemoryRecallResult(status=MemoryRecallStatus.NOT_CONFIGURED, error_key=ERR_MEMORY_NOT_CONFIGURED)
        try:
            _validate_active_store(store)
        except MemoryConflictError:
            return MemoryRecallResult(status=MemoryRecallStatus.NOT_CONFIGURED, error_key=ERR_MEMORY_NOT_CONFIGURED)
        if await memory_record_crud.count_active(db, uid=normalized_uid) == 0:
            return MemoryRecallResult(status=MemoryRecallStatus.EMPTY)

        active_snapshot = (
            store.active_collection_name,
            store.active_embedding_signature,
            store.active_embedding_revision,
        )
        try:
            runtime_config = await load_embedding_runtime_config(
                db,
                store.active_embedding_channel_id,
                store.active_embedding_model_id,
            )
            embeddings = await embed_texts_with_config(
                runtime_config,
                [normalized_query],
                dimensions=store.active_embedding_dimensions,
                db=db,
                release_connection=True,
            )
            if not isinstance(embeddings, list) or len(embeddings) != 1 or not isinstance(embeddings[0], list) or not embeddings[0] or any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in embeddings[0]):
                await db.rollback()
                return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
            query_vector = embeddings[0]
            if len(query_vector) != store.active_embedding_dimensions:
                await db.rollback()
                return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_EMBEDDING_DIMENSION_INVALID)
            current_store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
            if (
                current_store is None
                or (
                    current_store.active_collection_name,
                    current_store.active_embedding_signature,
                    current_store.active_embedding_revision,
                )
                != active_snapshot
            ):
                await db.rollback()
                return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_RECALL_UNAVAILABLE)
            await db.commit()
            hits = await _hybrid_query_collection(
                current_store.active_collection_name,
                query_vector,
                normalized_query,
                limit=normalized_candidate_k,
            )
            latest_store = await memory_store_crud.get_snapshot_by_uid(db, uid=normalized_uid)
            if (
                latest_store is None
                or (
                    latest_store.active_collection_name,
                    latest_store.active_embedding_signature,
                    latest_store.active_embedding_revision,
                )
                != active_snapshot
            ):
                await db.rollback()
                return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_RECALL_UNAVAILABLE)
        except Exception as exc:
            await db.rollback()
            logger.bind(uid=normalized_uid, error_type=type(exc).__name__).warning(t(ERR_MEMORY_RECALL_UNAVAILABLE))
            return MemoryRecallResult(status=MemoryRecallStatus.DEGRADED, error_key=ERR_MEMORY_RECALL_UNAVAILABLE)

        candidate_ids: list[int] = []
        valid_hits: list[tuple[Any, int, int]] = []
        for hit in hits:
            metadata = getattr(hit, "metadata", None)
            if not isinstance(metadata, dict) or metadata.get("uid") != normalized_uid:
                continue
            if isinstance(metadata.get("embedding_revision"), bool) or not isinstance(metadata.get("embedding_revision"), int) or metadata.get("embedding_revision") != active_snapshot[2]:
                continue
            hit_memory_id = metadata.get("memory_id")
            hit_version = metadata.get("version")
            if isinstance(hit_memory_id, bool) or not isinstance(hit_memory_id, int) or hit_memory_id < 1 or isinstance(hit_version, bool) or not isinstance(hit_version, int) or hit_version < 1:
                continue
            candidate_ids.append(hit_memory_id)
            valid_hits.append((hit, hit_memory_id, hit_version))

        records = await memory_record_crud.list_recallable_by_ids(db, uid=normalized_uid, memory_ids=set(candidate_ids))
        records_by_id = {record.id: record for record in records}
        items: list[MemoryRecallItem] = []
        remaining = normalized_result_max_chars
        for hit, hit_memory_id, hit_version in valid_hits:
            if len(items) >= normalized_top_k:
                break
            record = records_by_id.get(hit_memory_id)
            if record is None or record.version != hit_version or record.vector_item_id != hit.id or not isinstance(record.content, str):
                continue
            if remaining <= 0:
                break
            content = record.content
            truncated = len(content) > remaining
            output_content = content[:remaining] if truncated else content
            items.append(
                MemoryRecallItem(
                    memory_id=record.id,
                    memory_key=record.memory_key or "",
                    content=output_content,
                    memory_type=_enum_value(record.memory_type),
                    version=record.version,
                    updated_at=record.updated_at,
                    source=_enum_value(record.source),
                    dense_distance=getattr(hit, "dense_distance", None),
                    dense_rank=getattr(hit, "dense_rank", None),
                    sparse_score=getattr(hit, "sparse_score", None),
                    sparse_rank=getattr(hit, "sparse_rank", None),
                    fusion_score=getattr(hit, "fusion_score", None),
                    truncated=truncated,
                )
            )
            remaining -= len(output_content)
            if truncated:
                break
        if not items:
            return MemoryRecallResult(status=MemoryRecallStatus.EMPTY)
        recall_result = MemoryRecallResult(status=MemoryRecallStatus.OK, items=tuple(items))
        recalled_memory_ids = {item.memory_id for item in items}
        try:
            await memory_record_crud.touch_last_recalled_at(
                db,
                uid=normalized_uid,
                memory_ids=recalled_memory_ids,
                commit=True,
            )
        except Exception as exc:
            rollback_error_type: str | None = None
            try:
                await db.rollback()
            except Exception as rollback_exc:
                rollback_error_type = type(rollback_exc).__name__
            logger.bind(
                uid=normalized_uid,
                memory_ids=sorted(recalled_memory_ids),
                error_type=type(exc).__name__,
                rollback_error_type=rollback_error_type,
            ).warning(t(LOG_MEMORY_RECALL_TOUCH_FAILED))
        return recall_result
