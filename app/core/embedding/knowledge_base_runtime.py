from __future__ import annotations

from dataclasses import dataclass

from app.models.knowledge_base import KnowledgeBase


@dataclass(frozen=True, slots=True)
class KnowledgeBaseEmbeddingSnapshot:
    channel_id: int
    model_id: str
    dimensions: int | None
    collection_name: str


def resolve_active_knowledge_base_embedding(kb: KnowledgeBase) -> KnowledgeBaseEmbeddingSnapshot:
    """解析知识库当前生效的嵌入与集合快照；legacy 字段仅用于旧数据兼容回退。"""
    active_complete = bool(kb.active_embedding_channel_id and kb.active_embedding_model_id and kb.active_collection_name)
    if active_complete:
        return KnowledgeBaseEmbeddingSnapshot(
            channel_id=kb.active_embedding_channel_id,
            model_id=kb.active_embedding_model_id,
            dimensions=kb.active_embedding_dimensions,
            collection_name=kb.active_collection_name,
        )
    return KnowledgeBaseEmbeddingSnapshot(
        channel_id=kb.embedding_channel_id,
        model_id=kb.embedding_model_id,
        dimensions=kb.embedding_dimensions,
        collection_name=kb.collection_name,
    )


__all__ = ["KnowledgeBaseEmbeddingSnapshot", "resolve_active_knowledge_base_embedding"]
