from __future__ import annotations

import asyncio
import hashlib
import math
from numbers import Real
from typing import Any

from app.core.constants import (
    ERR_MEMORY_EMBEDDING_VECTOR_INVALID,
    ERR_MEMORY_JOB_VECTOR_DIMENSION_INVALID,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.i18n import t
from app.core.memory_jobs.executor import (
    MemoryJobExecutionContext,
)
from app.providers.vector import (
    async_delete_collection_items,
    async_validate_collection,
)

from .handler_contracts import (
    _deterministic,
    _MemoryOrganizationMergeSnapshot,
    _MemoryPublicationSnapshot,
    _MemoryReplacementSnapshot,
    logger,
)

__all__ = []


def _collection_metadata(
    snapshot: _MemoryPublicationSnapshot | _MemoryReplacementSnapshot | _MemoryOrganizationMergeSnapshot,
) -> dict[str, Any]:
    return {
        "memory_type": "long_term_memory",
        "uid_sha256": hashlib.sha256(snapshot.uid.encode("utf-8")).hexdigest(),
        "embedding_signature": snapshot.active_embedding_signature,
        "embedding_revision": snapshot.active_embedding_revision,
    }


def _validate_embedding_result(embeddings: Any, dimensions: int) -> list[float]:
    if not isinstance(embeddings, list) or len(embeddings) != 1:
        raise _deterministic(ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
    vector = embeddings[0]
    if not isinstance(vector, list) or not vector:
        raise _deterministic(ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
    if any(isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(float(value)) for value in vector):
        raise _deterministic(ERR_MEMORY_EMBEDDING_VECTOR_INVALID)
    if len(vector) != dimensions:
        raise _deterministic(ERR_MEMORY_JOB_VECTOR_DIMENSION_INVALID)
    return [float(value) for value in vector]


def _build_vector_metadata(snapshot: _MemoryPublicationSnapshot | _MemoryReplacementSnapshot, version: int) -> dict[str, Any]:
    payload = snapshot.payload if isinstance(snapshot, _MemoryPublicationSnapshot) else snapshot.publication
    metadata: dict[str, Any] = {
        "memory_id": snapshot.memory_id,
        "uid": snapshot.uid,
        "memory_key": payload["memory_key"],
        "memory_type": payload["memory_type"],
        "version": version,
        "source": payload["source"],
        "embedding_revision": snapshot.active_embedding_revision,
        "updated_at": snapshot.updated_at,
    }
    return metadata


async def _best_effort_delete_item(
    uid: str,
    job_id: int,
    collection_name: str,
    item_id: str,
    message_key: str,
    *,
    context: MemoryJobExecutionContext | None = None,
) -> None:
    try:
        if context is not None:
            async with context.session_factory() as db:
                claim = await memory_job_crud.get_active_claim(
                    db,
                    uid=uid,
                    job_id=job_id,
                    owner=context.worker_id,
                )
            if claim is None:
                return
        validation = await async_validate_collection(collection_name)
        if not getattr(validation, "exists", False):
            return
        await async_delete_collection_items(collection_name, [item_id], batch_size=1)
    except Exception as exc:
        logger.bind(
            uid=uid,
            job_id=job_id,
            item_id=item_id,
            exception_type=type(exc).__name__,
        ).warning(t(message_key))


async def _shield_best_effort_delete_item(
    uid: str,
    job_id: int,
    collection_name: str,
    item_id: str,
    message_key: str,
    *,
    context: MemoryJobExecutionContext | None = None,
) -> None:
    cleanup_task = asyncio.create_task(
        _best_effort_delete_item(
            uid,
            job_id,
            collection_name,
            item_id,
            message_key,
            context=context,
        )
    )
    try:
        await asyncio.shield(cleanup_task)
    except asyncio.CancelledError:
        try:
            await asyncio.shield(cleanup_task)
        except asyncio.CancelledError:
            await asyncio.gather(cleanup_task, return_exceptions=True)
            raise
        except Exception:
            pass
        raise
    except Exception:
        await asyncio.gather(cleanup_task, return_exceptions=True)
