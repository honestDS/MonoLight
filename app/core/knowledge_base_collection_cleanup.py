from __future__ import annotations

import asyncio
from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.knowledge.base import knowledge_base_collection_owner_crud
from app.core.log import get_logger
from app.providers.database import AsyncSessionLocal
from app.providers.vector import async_delete_collection_if_exists

__all__ = [
    "CollectionCleanupBatchResult",
    "process_pending_collection_cleanups",
    "run_knowledge_base_collection_cleanup_loop",
]

logger = get_logger(__name__)

KNOWLEDGE_BASE_COLLECTION_CLEANUP_INTERVAL_SECONDS = 30
KNOWLEDGE_BASE_COLLECTION_CLEANUP_BATCH_LIMIT = 100


@dataclass(frozen=True, slots=True)
class CollectionCleanupBatchResult:
    pending_count: int
    succeeded_count: int
    failed_count: int


async def process_pending_collection_cleanups(
    db: AsyncSession,
    *,
    limit: int = KNOWLEDGE_BASE_COLLECTION_CLEANUP_BATCH_LIMIT,
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


async def run_knowledge_base_collection_cleanup_loop(
    stop_event: asyncio.Event,
    *,
    interval_seconds: float = KNOWLEDGE_BASE_COLLECTION_CLEANUP_INTERVAL_SECONDS,
    batch_limit: int = KNOWLEDGE_BASE_COLLECTION_CLEANUP_BATCH_LIMIT,
) -> None:
    while not stop_event.is_set():
        try:
            async with AsyncSessionLocal() as db:
                result = await process_pending_collection_cleanups(db, limit=batch_limit)
            if result.pending_count:
                logger.bind(
                    pending_count=result.pending_count,
                    succeeded_count=result.succeeded_count,
                    failed_count=result.failed_count,
                ).info("Knowledge base collection cleanup round completed")
        except Exception:
            logger.exception("Knowledge base collection cleanup round failed")

        if stop_event.is_set():
            return

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
        except TimeoutError:
            continue
