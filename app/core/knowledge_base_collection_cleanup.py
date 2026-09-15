from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import KNOWLEDGE_COLLECTION_CLEANUP_BATCH_LIMIT
from app.core.crud.knowledge.base import knowledge_base_collection_owner_crud
from app.providers.vector import async_delete_collection_if_exists

__all__ = [
    "CollectionCleanupBatchResult",
    "process_pending_collection_cleanups",
]


@dataclass(frozen=True, slots=True)
class CollectionCleanupBatchResult:
    pending_count: int
    succeeded_count: int
    failed_count: int


async def process_pending_collection_cleanups(
    db: AsyncSession,
    *,
    limit: int = KNOWLEDGE_COLLECTION_CLEANUP_BATCH_LIMIT,
) -> CollectionCleanupBatchResult:
    if limit <= 0:
        return CollectionCleanupBatchResult(0, 0, 0)

    pending_records = await knowledge_base_collection_owner_crud.list_pending(db, limit=limit)
    pending_snapshots = [(record.collection_name, record.cleanup_revision) for record in pending_records]
    await db.commit()

    succeeded_count = 0
    failed_count = 0
    for collection_name, cleanup_revision in pending_snapshots:
        try:
            await async_delete_collection_if_exists(collection_name)
        except Exception as exc:
            message = str(exc)
            error = f"{type(exc).__name__}: {message}" if message else type(exc).__name__
            changed = await knowledge_base_collection_owner_crud.mark_failed(
                db,
                collection_name=collection_name,
                expected_revision=cleanup_revision,
                error=error,
            )
            if changed:
                failed_count += 1
        else:
            changed = await knowledge_base_collection_owner_crud.mark_succeeded(
                db,
                collection_name=collection_name,
                expected_revision=cleanup_revision,
            )
            if changed:
                succeeded_count += 1

    return CollectionCleanupBatchResult(
        pending_count=len(pending_snapshots),
        succeeded_count=succeeded_count,
        failed_count=failed_count,
    )
