from __future__ import annotations

from app.core.constants import (
    ERR_MEMORY_JOB_EMBEDDING_FAILED,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_JOB_PREPARATION_FAILED,
    ERR_MEMORY_JOB_PUBLICATION_FAILED,
    ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
    ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
)
from app.core.embedding.common import (
    embed_texts_with_config,
)
from app.core.i18n import t
from app.core.memory import (
    MemoryConflictError,
    MemoryNotFoundError,
    MemoryValidationError,
    build_memory_staged_vector_item_id,
)
from app.core.memory_jobs.executor import (
    MemoryJobExecutionContext,
    MemoryJobExecutionError,
    MemoryJobExecutionResult,
)
from app.core.memory_jobs.vector_cleanup import (
    persist_staged_vector_reference,
)
from app.models.memory import (
    LongTermMemoryMutationOperation,
    LongTermMemorySource,
)
from app.providers.vector import (
    async_get_or_create_collection,
    async_upsert_collection_items,
)

from .handler_contracts import (
    _deterministic,
    _MemoryOrganizationMergeSnapshot,
    _MemoryPublicationSnapshot,
    _MemoryReplacementSnapshot,
    _retryable,
    logger,
)
from .handler_organization import _publish_organization_merge
from .handler_prepare import (
    _prepare_organization_merge,
    _prepare_publication,
    _prepare_replacement,
)
from .handler_publication import (
    _publish_replacement,
    _publish_version,
)
from .handler_vector import (
    _build_vector_metadata,
    _collection_metadata,
    _shield_best_effort_delete_item,
    _validate_embedding_result,
)

__all__ = []


async def _execute_publication(
    context: MemoryJobExecutionContext,
    operation: LongTermMemoryMutationOperation,
) -> MemoryJobExecutionResult:
    snapshot: _MemoryPublicationSnapshot | None = None
    item_id: str | None = None
    item_written = False
    published = False
    phase = "preparation"
    try:
        snapshot = await _prepare_publication(context, operation)
        await context.checkpoint()
        phase = "collection"
        await async_get_or_create_collection(
            snapshot.active_collection_name,
            metadata=_collection_metadata(snapshot),
            distance="cosine",
        )
        phase = "embedding"
        try:
            embeddings = await embed_texts_with_config(
                snapshot.runtime_config,
                [snapshot.payload["content"]],
                batch_size=1,
                dimensions=snapshot.active_embedding_dimensions,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_EMBEDDING_FAILED) from exc
        vector = _validate_embedding_result(embeddings, snapshot.active_embedding_dimensions)

        await context.checkpoint()
        next_version = snapshot.expected_version + 1
        item_id = build_memory_staged_vector_item_id(
            snapshot.memory_id,
            next_version,
            snapshot.job_id,
            snapshot.owner,
        )
        await persist_staged_vector_reference(
            context,
            collection_name=snapshot.active_collection_name,
            item_id=item_id,
        )
        metadata = _build_vector_metadata(snapshot, next_version)
        phase = "vector_write"
        item_written = True
        try:
            await async_upsert_collection_items(
                snapshot.active_collection_name,
                [item_id],
                [snapshot.payload["content"]],
                [vector],
                [metadata],
                batch_size=1,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc

        await context.checkpoint()
        phase = "publication"
        execution_result = await _publish_version(context, snapshot, item_id)
        published = True
        if snapshot.previous_vector_item_id and snapshot.previous_vector_item_id != item_id:
            await _shield_best_effort_delete_item(
                snapshot.uid,
                snapshot.job_id,
                snapshot.active_collection_name,
                snapshot.previous_vector_item_id,
                ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            )
        return execution_result
    except MemoryJobExecutionError:
        raise
    except (MemoryValidationError, MemoryConflictError, MemoryNotFoundError) as exc:
        if isinstance(exc, MemoryValidationError) or phase == "preparation":
            raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    except Exception as exc:
        message_key = {
            "preparation": ERR_MEMORY_JOB_PREPARATION_FAILED,
            "collection": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "embedding": ERR_MEMORY_JOB_EMBEDDING_FAILED,
            "vector_write": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "publication": ERR_MEMORY_JOB_PUBLICATION_FAILED,
        }.get(phase, ERR_MEMORY_JOB_PUBLICATION_FAILED)
        logger.bind(
            uid=context.job.uid,
            job_id=context.job.id,
            exception_type=type(exc).__name__,
        ).warning(t(message_key))
        raise _retryable(message_key) from exc
    finally:
        if item_written and not published and snapshot is not None and item_id is not None:
            await _shield_best_effort_delete_item(
                snapshot.uid,
                snapshot.job_id,
                snapshot.active_collection_name,
                item_id,
                ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
                context=context,
            )


async def _execute_replacement(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    snapshot: _MemoryReplacementSnapshot | None = None
    item_id: str | None = None
    item_written = False
    published = False
    phase = "preparation"
    try:
        snapshot = await _prepare_replacement(context)
        await context.checkpoint()
        phase = "collection"
        await async_get_or_create_collection(
            snapshot.active_collection_name,
            metadata=_collection_metadata(snapshot),
            distance="cosine",
        )
        phase = "embedding"
        try:
            embeddings = await embed_texts_with_config(
                snapshot.runtime_config,
                [snapshot.publication["content"]],
                batch_size=1,
                dimensions=snapshot.active_embedding_dimensions,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_EMBEDDING_FAILED) from exc
        vector = _validate_embedding_result(embeddings, snapshot.active_embedding_dimensions)

        await context.checkpoint()
        item_id = build_memory_staged_vector_item_id(snapshot.memory_id, 1, snapshot.job_id, snapshot.owner)
        await persist_staged_vector_reference(
            context,
            collection_name=snapshot.active_collection_name,
            item_id=item_id,
        )
        metadata = _build_vector_metadata(snapshot, 1)
        phase = "vector_write"
        item_written = True
        try:
            await async_upsert_collection_items(
                snapshot.active_collection_name,
                [item_id],
                [snapshot.publication["content"]],
                [vector],
                [metadata],
                batch_size=1,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc

        await context.checkpoint()
        phase = "publication"
        execution_result = await _publish_replacement(context, snapshot, item_id)
        published = True
        return execution_result
    except MemoryJobExecutionError:
        raise
    except (MemoryValidationError, MemoryConflictError, MemoryNotFoundError) as exc:
        if isinstance(exc, MemoryValidationError) or phase == "preparation":
            raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    except Exception as exc:
        message_key = {
            "preparation": ERR_MEMORY_JOB_PREPARATION_FAILED,
            "collection": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "embedding": ERR_MEMORY_JOB_EMBEDDING_FAILED,
            "vector_write": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "publication": ERR_MEMORY_JOB_PUBLICATION_FAILED,
        }.get(phase, ERR_MEMORY_JOB_PUBLICATION_FAILED)
        logger.bind(
            uid=context.job.uid,
            job_id=context.job.id,
            exception_type=type(exc).__name__,
        ).warning(t(message_key))
        raise _retryable(message_key) from exc
    finally:
        if item_written and not published and snapshot is not None and item_id is not None:
            await _shield_best_effort_delete_item(
                snapshot.uid,
                snapshot.job_id,
                snapshot.active_collection_name,
                item_id,
                ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
                context=context,
            )


async def _execute_organization_merge(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    snapshot: _MemoryOrganizationMergeSnapshot | None = None
    item_id: str | None = None
    item_written = False
    published = False
    phase = "preparation"
    try:
        snapshot = await _prepare_organization_merge(context)
        await context.checkpoint()
        phase = "collection"
        await async_get_or_create_collection(
            snapshot.active_collection_name,
            metadata=_collection_metadata(snapshot),
            distance="cosine",
        )
        phase = "embedding"
        try:
            embeddings = await embed_texts_with_config(
                snapshot.runtime_config,
                [snapshot.target["content"]],
                batch_size=1,
                dimensions=snapshot.active_embedding_dimensions,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_EMBEDDING_FAILED) from exc
        vector = _validate_embedding_result(embeddings, snapshot.active_embedding_dimensions)

        await context.checkpoint()
        next_version = snapshot.expected_version + 1
        item_id = build_memory_staged_vector_item_id(
            snapshot.primary_memory_id,
            next_version,
            snapshot.job_id,
            snapshot.owner,
        )
        await persist_staged_vector_reference(
            context,
            collection_name=snapshot.active_collection_name,
            item_id=item_id,
        )
        metadata = {
            "memory_id": snapshot.primary_memory_id,
            "uid": snapshot.uid,
            "memory_key": snapshot.target["memory_key"],
            "memory_type": snapshot.target["memory_type"],
            "version": next_version,
            "source": LongTermMemorySource.AUTO_ORGANIZE.value,
            "embedding_revision": snapshot.active_embedding_revision,
            "updated_at": snapshot.updated_at,
        }
        phase = "vector_write"
        item_written = True
        try:
            await async_upsert_collection_items(
                snapshot.active_collection_name,
                [item_id],
                [snapshot.target["content"]],
                [vector],
                [metadata],
                batch_size=1,
            )
        except MemoryJobExecutionError:
            raise
        except Exception as exc:
            raise _retryable(ERR_MEMORY_JOB_VECTOR_WRITE_FAILED) from exc

        await context.checkpoint()
        phase = "publication"
        execution_result = await _publish_organization_merge(context, snapshot, item_id)
        published = True
        if snapshot.previous_vector_item_id and snapshot.previous_vector_item_id != item_id:
            await _shield_best_effort_delete_item(
                snapshot.uid,
                snapshot.job_id,
                snapshot.active_collection_name,
                snapshot.previous_vector_item_id,
                ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            )
        return execution_result
    except MemoryJobExecutionError:
        raise
    except (MemoryValidationError, MemoryConflictError, MemoryNotFoundError) as exc:
        if isinstance(exc, MemoryValidationError) or phase == "preparation":
            raise _deterministic(ERR_MEMORY_JOB_PAYLOAD_INVALID) from exc
        raise _deterministic(ERR_MEMORY_JOB_TARGET_STATE_CONFLICT) from exc
    except Exception as exc:
        message_key = {
            "preparation": ERR_MEMORY_JOB_PREPARATION_FAILED,
            "collection": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "embedding": ERR_MEMORY_JOB_EMBEDDING_FAILED,
            "vector_write": ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
            "publication": ERR_MEMORY_JOB_PUBLICATION_FAILED,
        }.get(phase, ERR_MEMORY_JOB_PUBLICATION_FAILED)
        logger.bind(
            uid=context.job.uid,
            job_id=context.job.id,
            exception_type=type(exc).__name__,
        ).warning(t(message_key))
        raise _retryable(message_key) from exc
    finally:
        if item_written and not published and snapshot is not None and item_id is not None:
            await _shield_best_effort_delete_item(
                snapshot.uid,
                snapshot.job_id,
                snapshot.active_collection_name,
                item_id,
                ERR_MEMORY_JOB_VECTOR_WRITE_FAILED,
                context=context,
            )


async def _handle_create(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    return await _execute_publication(context, LongTermMemoryMutationOperation.CREATE)


async def _handle_create_with_eviction(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    return await _execute_replacement(context)


async def _handle_update(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    return await _execute_publication(context, LongTermMemoryMutationOperation.UPDATE)


async def _handle_organization_merge(context: MemoryJobExecutionContext) -> MemoryJobExecutionResult:
    return await _execute_organization_merge(context)
