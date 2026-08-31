from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.knowledge.embedding_transition import knowledge_base_migration_crud
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseEmbeddingDelta,
    KnowledgeBaseMigrationDeltaAction,
    KnowledgeBaseMigrationSourceType,
    KnowledgeBaseMigrationStatus,
)

_ACTIVE_MIGRATION_STATUSES = frozenset(
    {
        KnowledgeBaseMigrationStatus.PREPARING,
        KnowledgeBaseMigrationStatus.BUILDING,
        KnowledgeBaseMigrationStatus.CATCHING_UP,
        KnowledgeBaseMigrationStatus.VALIDATING,
    }
)


def migration_accepts_deltas(knowledge_base: KnowledgeBase) -> bool:
    return knowledge_base.migration_job_id is not None and knowledge_base.migration_status in _ACTIVE_MIGRATION_STATUSES


async def record_knowledge_base_migration_change(
    db: AsyncSession,
    *,
    knowledge_base: KnowledgeBase,
    source_type: KnowledgeBaseMigrationSourceType,
    source_id: int | None,
    action: KnowledgeBaseMigrationDeltaAction,
    source_version: int | None = None,
) -> KnowledgeBaseEmbeddingDelta | None:
    if knowledge_base.id is None or source_id is None or source_id < 1 or not migration_accepts_deltas(knowledge_base):
        return None
    job_id = knowledge_base.migration_job_id
    if job_id is None:
        return None
    sequence = knowledge_base.migration_delta_high_watermark + 1
    delta = KnowledgeBaseEmbeddingDelta(
        uid=knowledge_base.uid,
        knowledge_base_id=knowledge_base.id,
        migration_job_id=job_id,
        sequence=sequence,
        source_type=source_type,
        source_id=source_id,
        source_version=source_version,
        action=action,
    )
    knowledge_base.migration_delta_high_watermark = sequence
    await knowledge_base_migration_crud.create_delta(db, delta=delta)
    await db.flush()
    return delta


__all__ = [
    "migration_accepts_deltas",
    "record_knowledge_base_migration_change",
]
