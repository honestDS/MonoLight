from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.managed_knowledge import managed_knowledge_item_crud
from app.core.retrieval.schemas import RetrievalHit


def _metadata_positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value > 0:
        return value
    return None


async def filter_recallable_managed_hits(
    db: AsyncSession,
    *,
    uid: str,
    knowledge_base_id: int,
    hits: list[RetrievalHit],
) -> list[RetrievalHit]:
    """过滤已失效的托管知识向量，关系库状态始终作为可召回性的最终依据。"""
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

    items = []
    if managed_ids:
        items = await managed_knowledge_item_crud.get_by_ids(
            db,
            uid=uid,
            knowledge_base_id=knowledge_base_id,
            knowledge_ids=managed_ids,
        )
    items_by_id = {item.id: item for item in items if item.id is not None}

    filtered: list[RetrievalHit] = []
    for hit in hits:
        metadata = hit.metadata or {}
        if metadata.get("knowledge_type") != "managed":
            filtered.append(hit)
            continue
        knowledge_id = _metadata_positive_int(metadata.get("managed_knowledge_id"))
        version = _metadata_positive_int(metadata.get("managed_knowledge_version"))
        item = items_by_id.get(knowledge_id)
        if (
            item is None
            or version is None
            or item.deleted_at is not None
            or not item.is_recallable
            or item.version != version
            or item.indexed_version != version
            or hit.id not in (item.vector_item_ids or [])
        ):
            continue
        filtered.append(hit)
    return filtered


__all__ = ["filter_recallable_managed_hits"]
