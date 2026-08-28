from __future__ import annotations

from dataclasses import dataclass

from chromadb.errors import NotFoundError as ChromaNotFoundError

from app.core.constants import (
    ERR_KNOWLEDGE_JOB_DELETE_CLEANUP_FAILED,
    ERR_KNOWLEDGE_JOB_EMBEDDING_FAILED,
    ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE,
    ERR_KNOWLEDGE_JOB_PUBLICATION_FAILED,
    ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT,
    ERR_KNOWLEDGE_JOB_VECTOR_WRITE_FAILED,
    MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
    MANAGED_KNOWLEDGE_VECTOR_CHUNK_OVERLAP,
    MANAGED_KNOWLEDGE_VECTOR_CHUNK_SIZE,
)
from app.core.crud.knowledge_base import knowledge_base_crud
from app.core.crud.knowledge_job import knowledge_job_crud
from app.core.crud.managed_knowledge import managed_knowledge_item_crud
from app.core.embedding.common import EmbeddingRuntimeConfig, embed_texts_with_config, load_embedding_runtime_config
from app.core.embedding.knowledge_base_runtime import resolve_active_knowledge_base_embedding
from app.core.i18n import t
from app.core.knowledge_jobs.executor import (
    KnowledgeJobDeterministicError,
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutionResult,
    KnowledgeJobExecutor,
    KnowledgeJobLeaseLostError,
    KnowledgeJobRetryableError,
    SessionFactory,
)
from app.core.knowledge_jobs.vector_cleanup import (
    create_managed_vector_cleanup_job,
    execute_managed_vector_cleanup,
)
from app.core.utils.text_splitter import TextSplitter
from app.models.knowledge_base import KnowledgeBaseType, KnowledgeJobOperation
from app.providers.database import AsyncSessionLocal
from app.providers.vector import (
    async_delete_collection_items,
    async_get_or_create_collection,
    async_upsert_collection_items,
)


@dataclass(frozen=True, slots=True)
class _ManagedPublicationSnapshot:
    uid: str
    job_id: int
    knowledge_base_id: int
    knowledge_id: int
    version: int
    attempt_count: int
    content: str
    collection_name: str
    embedding_channel_id: int
    embedding_model_id: str
    embedding_dimensions: int | None
    previous_vector_item_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class _ManagedDeleteSnapshot:
    uid: str
    job_id: int
    knowledge_base_id: int
    knowledge_id: int
    version: int
    collection_name: str
    vector_item_ids: tuple[str, ...]


def _positive_int(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return None
    return value


async def _load_container(db, *, uid: str, knowledge_base_id: int):
    knowledge_base = await knowledge_base_crud.get(db, knowledge_base_id)
    if (
        knowledge_base is None
        or knowledge_base.uid != uid
        or knowledge_base.knowledge_base_type != KnowledgeBaseType.LLM_MANAGED
    ):
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    return knowledge_base


async def _prepare_publication(
    context: KnowledgeJobExecutionContext,
) -> tuple[_ManagedPublicationSnapshot, EmbeddingRuntimeConfig]:
    job = await context.checkpoint()
    job_id = _positive_int(job.id)
    knowledge_id = _positive_int(job.knowledge_id)
    expected_version = _positive_int(job.expected_version)
    attempt_count = _positive_int(job.attempt_count)
    if job_id is None or knowledge_id is None or expected_version is None or attempt_count is None:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))

    async with context.session_factory() as db:
        item = await managed_knowledge_item_crud.get_by_id(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
            knowledge_id=knowledge_id,
        )
        if (
            item is None
            or item.deleted_at is not None
            or item.version != expected_version
            or item.pending_job_id != job_id
        ):
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        knowledge_base = await _load_container(db, uid=job.uid, knowledge_base_id=job.knowledge_base_id)
        active_embedding = resolve_active_knowledge_base_embedding(knowledge_base)
        channel_id = active_embedding.channel_id
        model_id = active_embedding.model_id
        dimensions = active_embedding.dimensions
        collection_name = active_embedding.collection_name
        if _positive_int(channel_id) is None or not model_id or not collection_name:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        try:
            embedding_config = await load_embedding_runtime_config(db, channel_id, model_id)
        except Exception as exc:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_EMBEDDING_FAILED)) from exc
        snapshot = _ManagedPublicationSnapshot(
            uid=job.uid,
            job_id=job_id,
            knowledge_base_id=job.knowledge_base_id,
            knowledge_id=knowledge_id,
            version=expected_version,
            attempt_count=attempt_count,
            content=item.content,
            collection_name=collection_name,
            embedding_channel_id=channel_id,
            embedding_model_id=model_id,
            embedding_dimensions=dimensions,
            previous_vector_item_ids=tuple(item.vector_item_ids or ()),
        )
    return snapshot, embedding_config


def _build_vector_items(snapshot: _ManagedPublicationSnapshot) -> tuple[list[str], list[str], list[dict]]:
    chunks = TextSplitter(
        chunk_size=MANAGED_KNOWLEDGE_VECTOR_CHUNK_SIZE,
        chunk_overlap=MANAGED_KNOWLEDGE_VECTOR_CHUNK_OVERLAP,
    ).split(snapshot.content)
    if not chunks:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    item_ids = [
        f"managed_{snapshot.knowledge_base_id}_{snapshot.knowledge_id}_v{snapshot.version}_a{snapshot.attempt_count}_chunk_{index}"
        for index in range(len(chunks))
    ]
    metadatas = [
        {
            "knowledge_type": "managed",
            "knowledge_base_id": snapshot.knowledge_base_id,
            "managed_knowledge_id": snapshot.knowledge_id,
            "managed_knowledge_version": snapshot.version,
            "chunk_index": index,
            "chunk_count": len(chunks),
        }
        for index in range(len(chunks))
    ]
    return item_ids, chunks, metadatas


async def handle_managed_publication(context: KnowledgeJobExecutionContext) -> KnowledgeJobExecutionResult:
    snapshot, embedding_config = await _prepare_publication(context)
    vector_item_ids, chunks, metadatas = _build_vector_items(snapshot)

    await context.checkpoint()
    try:
        await async_get_or_create_collection(snapshot.collection_name)
    except Exception as exc:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_VECTOR_WRITE_FAILED)) from exc

    await context.checkpoint()
    try:
        embeddings = await embed_texts_with_config(
            embedding_config,
            chunks,
            batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
            dimensions=snapshot.embedding_dimensions,
        )
    except Exception as exc:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_EMBEDDING_FAILED)) from exc
    if len(embeddings) != len(chunks) or any(
        not vector
        or (
            snapshot.embedding_dimensions is not None
            and len(vector) != snapshot.embedding_dimensions
        )
        for vector in embeddings
    ):
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_EMBEDDING_FAILED))

    await context.checkpoint()
    async with context.session_factory() as db:
        current = await knowledge_job_crud.get_active_claim(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            worker_id=context.worker_id,
        )
        if current is None:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        await create_managed_vector_cleanup_job(
            db,
            source_job=current,
            reason="staged",
            collection_name=snapshot.collection_name,
            vector_item_ids=vector_item_ids,
        )
        await db.commit()

    await context.checkpoint()
    try:
        await async_upsert_collection_items(
            snapshot.collection_name,
            vector_item_ids,
            chunks,
            embeddings,
            metadatas,
            batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
        )
    except Exception as exc:
        raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_VECTOR_WRITE_FAILED)) from exc

    await context.checkpoint()
    async with context.session_factory() as db:
        current = await knowledge_job_crud.get_active_claim(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            worker_id=context.worker_id,
        )
        if current is None:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        published = await managed_knowledge_item_crud.publish_indexed_version(
            db,
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            knowledge_id=snapshot.knowledge_id,
            expected_version=snapshot.version,
            job_id=snapshot.job_id,
            vector_item_ids=vector_item_ids,
            commit=False,
        )
        if published is None:
            await db.rollback()
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        stale_vector_ids = [
            item_id
            for item_id in snapshot.previous_vector_item_ids
            if item_id not in set(vector_item_ids)
        ]
        if stale_vector_ids:
            await create_managed_vector_cleanup_job(
                db,
                source_job=current,
                reason="superseded",
                collection_name=snapshot.collection_name,
                vector_item_ids=stale_vector_ids,
            )
        succeeded = await knowledge_job_crud.mark_succeeded(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            owner=context.worker_id,
            result={
                "knowledge_id": snapshot.knowledge_id,
                "version": snapshot.version,
                "vector_item_count": len(vector_item_ids),
            },
            commit=False,
        )
        if not succeeded:
            await db.rollback()
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        await db.commit()
    return KnowledgeJobExecutionResult(
        result={
            "knowledge_id": snapshot.knowledge_id,
            "version": snapshot.version,
            "vector_item_count": len(vector_item_ids),
        },
        finalized=True,
    )


async def _prepare_delete(context: KnowledgeJobExecutionContext) -> _ManagedDeleteSnapshot:
    job = await context.checkpoint()
    job_id = _positive_int(job.id)
    knowledge_id = _positive_int(job.knowledge_id)
    expected_version = _positive_int(job.expected_version)
    if job_id is None or knowledge_id is None or expected_version is None:
        raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
    async with context.session_factory() as db:
        item = await managed_knowledge_item_crud.get_by_id(
            db,
            uid=job.uid,
            knowledge_base_id=job.knowledge_base_id,
            knowledge_id=knowledge_id,
        )
        if (
            item is None
            or item.deleted_at is None
            or item.is_recallable
            or item.version != expected_version
            or item.pending_job_id != job_id
        ):
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        knowledge_base = await _load_container(db, uid=job.uid, knowledge_base_id=job.knowledge_base_id)
        collection_name = resolve_active_knowledge_base_embedding(knowledge_base).collection_name
        if not collection_name:
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        return _ManagedDeleteSnapshot(
            uid=job.uid,
            job_id=job_id,
            knowledge_base_id=job.knowledge_base_id,
            knowledge_id=knowledge_id,
            version=expected_version,
            collection_name=collection_name,
            vector_item_ids=tuple(item.vector_item_ids or ()),
        )


async def handle_managed_delete_cleanup(context: KnowledgeJobExecutionContext) -> KnowledgeJobExecutionResult:
    snapshot = await _prepare_delete(context)
    if snapshot.vector_item_ids:
        try:
            await async_delete_collection_items(
                snapshot.collection_name,
                list(snapshot.vector_item_ids),
                batch_size=MANAGED_KNOWLEDGE_VECTOR_BATCH_SIZE,
            )
        except ChromaNotFoundError:
            pass
        except Exception as exc:
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_DELETE_CLEANUP_FAILED)) from exc

    await context.checkpoint()
    async with context.session_factory() as db:
        current = await knowledge_job_crud.get_active_claim(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            worker_id=context.worker_id,
        )
        if current is None:
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        deleted = await managed_knowledge_item_crud.hard_delete_tombstoned(
            db,
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            knowledge_id=snapshot.knowledge_id,
            expected_version=snapshot.version,
            job_id=snapshot.job_id,
            commit=False,
        )
        if not deleted:
            await db.rollback()
            raise KnowledgeJobDeterministicError(t(ERR_KNOWLEDGE_JOB_TARGET_STATE_CONFLICT))
        succeeded = await knowledge_job_crud.mark_succeeded(
            db,
            uid=snapshot.uid,
            job_id=snapshot.job_id,
            owner=context.worker_id,
            result={"knowledge_id": snapshot.knowledge_id, "version": snapshot.version},
            commit=False,
        )
        if not succeeded:
            await db.rollback()
            raise KnowledgeJobLeaseLostError(t(ERR_KNOWLEDGE_JOB_LEASE_UNAVAILABLE))
        try:
            await db.commit()
        except Exception as exc:
            await db.rollback()
            raise KnowledgeJobRetryableError(t(ERR_KNOWLEDGE_JOB_PUBLICATION_FAILED)) from exc
    return KnowledgeJobExecutionResult(
        result={"knowledge_id": snapshot.knowledge_id, "version": snapshot.version},
        finalized=True,
    )


def create_default_knowledge_job_executor(
    *,
    session_factory: SessionFactory = AsyncSessionLocal,
) -> KnowledgeJobExecutor:
    return KnowledgeJobExecutor(
        {
            KnowledgeJobOperation.MANAGED_CREATE: handle_managed_publication,
            KnowledgeJobOperation.MANAGED_UPDATE: handle_managed_publication,
            KnowledgeJobOperation.MANAGED_DELETE_CLEANUP: handle_managed_delete_cleanup,
            KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP: execute_managed_vector_cleanup,
        },
        session_factory=session_factory,
    )


__all__ = [
    "create_default_knowledge_job_executor",
    "handle_managed_delete_cleanup",
    "handle_managed_publication",
]
