from __future__ import annotations

from dataclasses import replace

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.managed_knowledge import (
    ManagedKnowledgeRecallState,
    managed_knowledge_item_crud,
)
from app.core.retrieval.schemas import RetrievalHit
from app.models.knowledge_base import ManagedKnowledgeItem


def _metadata_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


def _is_current_recallable_managed_hit(
    *,
    item: ManagedKnowledgeItem | ManagedKnowledgeRecallState | None,
    hit: RetrievalHit,
    version: int | None,
) -> bool:
    return bool(item is not None and version is not None and item.deleted_at is None and item.is_recallable and item.version == version and item.indexed_version == version and hit.id in (item.vector_item_ids or []))


async def filter_recallable_managed_hits(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    hits: list[RetrievalHit],
) -> list[RetrievalHit]:
    """校验托管知识向量并按逻辑知识去重；保留最佳分块供后续 rerank。"""
    managed_ids: set[int] = set()
    has_managed_hits = False
    for hit in hits:
        metadata = hit.metadata or {}
        if metadata.get("knowledge_type") != "managed":
            continue
        has_managed_hits = True
        knowledge_id = _metadata_positive_int(metadata.get("managed_knowledge_id"))
        if knowledge_id is not None:
            managed_ids.add(knowledge_id)

    if not has_managed_hits:
        return hits

    items: list[ManagedKnowledgeRecallState] = []
    if managed_ids:
        items = await managed_knowledge_item_crud.get_recall_states_by_ids(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            knowledge_ids=managed_ids,
        )
    items_by_id = {item.id: item for item in items}

    filtered: list[RetrievalHit] = []
    emitted_managed_ids: set[int] = set()
    for hit in hits:
        metadata = hit.metadata or {}
        if metadata.get("knowledge_type") != "managed":
            filtered.append(hit)
            continue
        knowledge_id = _metadata_positive_int(metadata.get("managed_knowledge_id"))
        version = _metadata_positive_int(metadata.get("managed_knowledge_version"))
        item = items_by_id.get(knowledge_id)
        if not _is_current_recallable_managed_hit(item=item, hit=hit, version=version):
            continue
        if knowledge_id in emitted_managed_ids:
            continue
        emitted_managed_ids.add(knowledge_id)
        filtered.append(hit)
    return filtered


async def materialize_recallable_managed_hits(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    hits: list[RetrievalHit],
) -> list[RetrievalHit]:
    """最终返回前再次校验托管知识状态，并用关系库当前完整正文替换向量分块。"""
    managed_ids = {knowledge_id for hit in hits if (hit.metadata or {}).get("knowledge_type") == "managed" if (knowledge_id := _metadata_positive_int((hit.metadata or {}).get("managed_knowledge_id"))) is not None}
    if not managed_ids:
        return hits

    items = await managed_knowledge_item_crud.get_by_ids(
        db,
        uid=uid,
        knowledge_base_id=knowledge_base_id,
        knowledge_ids=managed_ids,
    )
    items_by_id = {item.id: item for item in items if item.id is not None}

    materialized: list[RetrievalHit] = []
    emitted_managed_ids: set[int] = set()
    for hit in hits:
        metadata = hit.metadata or {}
        if metadata.get("knowledge_type") != "managed":
            materialized.append(hit)
            continue
        knowledge_id = _metadata_positive_int(metadata.get("managed_knowledge_id"))
        version = _metadata_positive_int(metadata.get("managed_knowledge_version"))
        item = items_by_id.get(knowledge_id)
        if knowledge_id in emitted_managed_ids or not _is_current_recallable_managed_hit(
            item=item,
            hit=hit,
            version=version,
        ):
            continue
        emitted_managed_ids.add(knowledge_id)
        managed_metadata = dict(metadata)
        managed_metadata.update(
            {
                "knowledge_type": "managed",
                "managed_knowledge_id": item.id,
                "managed_knowledge_version": item.version,
                "managed_knowledge_llm_maintainable": item.llm_maintainable,
            }
        )
        materialized.append(
            replace(
                hit,
                content=item.content,
                metadata=managed_metadata,
            )
        )
    return materialized


__all__ = ["filter_recallable_managed_hits", "materialize_recallable_managed_hits"]
