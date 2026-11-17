import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import ERR_MEMORY_OVER_LIMIT, MEMORY_CONTENT_MAX_TOKENS
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import (
    memory_embedding_delta_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.i18n import t
from app.core.memory import (
    MemoryRecallStatus,
    build_memory_content_hash,
    build_memory_vector_item_id,
    memory_service,
    normalize_memory_content,
)
from app.core.memory import service_recall as memory_recall_service_module
from app.core.memory_jobs.consumer import MemoryJobConsumer, create_memory_job_consumer
from app.core.memory_jobs.executor import (
    MemoryJobExecutor,
)
from app.core.retrieval.schemas import RetrievalHit
from app.core.utils.tokenizer import estimate_tokens
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryRevision,
    LongTermMemorySource,
    LongTermMemoryStore,
    LongTermMemoryType,
)
from app.providers.database.time import get_database_time
from app.providers.vector.chroma import CollectionValidationResult


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass


with patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient):
    import app.core.memory_jobs.handler_cleanup as memory_cleanup_handler
    import app.core.memory_jobs.handler_execution as memory_execution_handler
    import app.core.memory_jobs.handler_prepare as memory_prepare_handler
    import app.core.memory_jobs.handler_publication as memory_publication_handler  # noqa: F401
    import app.core.memory_jobs.handler_publication_validation as memory_publication_validation_handler
    import app.core.memory_jobs.handler_vector as memory_vector_handler
    from app.core.memory_jobs import handlers as memory_handlers
    from app.core.memory_jobs import vector_cleanup as memory_vector_cleanup


MEMORY_TABLES = [
    LongTermMemoryStore.__table__,
    LongTermMemoryEmbeddingRevision.__table__,
    LongTermMemoryEmbeddingDelta.__table__,
    LongTermMemoryRecord.__table__,
    LongTermMemoryRevision.__table__,
    LongTermMemoryMutationJob.__table__,
]

WAIT_TIMEOUT_SECONDS = 2.0
POLL_INTERVAL_SECONDS = 0.01


class _FakeVectorBackend:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.embedding_error: BaseException | None = None
        self.upsert_error: BaseException | None = None
        self.delete_error: BaseException | None = None
        self.embedding_hook: Callable[[EmbeddingRuntimeConfig, list[str]], Awaitable[None]] | None = None
        self.embedding_calls: list[tuple[EmbeddingRuntimeConfig, list[str]]] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.delete_calls: list[tuple[str, list[str]]] = []
        self.load_calls: list[tuple[int, str]] = []
        self.runtime_configs: dict[tuple[int, str], EmbeddingRuntimeConfig] = {}

    async def load_config(self, _db: Any, channel_id: int, model_id: str) -> EmbeddingRuntimeConfig:
        self.load_calls.append((channel_id, model_id))
        return self.runtime_configs[(channel_id, model_id)]

    async def get_or_create_collection(
        self,
        collection_name: str,
        *,
        metadata: dict[str, Any] | None = None,
        distance: str | None = None,
    ) -> dict[str, Any]:
        collection = self.collections.setdefault(
            collection_name,
            {"metadata": {**(metadata or {}), **({"hnsw:space": distance} if distance else {})}, "items": {}},
        )
        return collection

    async def embed(
        self,
        config: EmbeddingRuntimeConfig,
        texts: list[str],
        **_kwargs: Any,
    ) -> list[list[float]]:
        self.embedding_calls.append((config, list(texts)))
        if self.embedding_hook is not None:
            await self.embedding_hook(config, texts)
        if self.embedding_error is not None:
            raise self.embedding_error
        return [[0.1, 0.2, 0.3]]

    async def upsert(
        self,
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        **_kwargs: Any,
    ) -> int:
        self.upsert_calls.append(
            {
                "collection_name": collection_name,
                "ids": list(item_ids),
                "documents": list(documents),
                "embeddings": [list(vector) for vector in embeddings],
                "metadatas": [dict(metadata) for metadata in metadatas],
            }
        )
        if self.upsert_error is not None:
            raise self.upsert_error
        collection = self.collections.setdefault(collection_name, {"metadata": {}, "items": {}})
        for item_id, document, embedding, metadata in zip(item_ids, documents, embeddings, metadatas, strict=True):
            collection["items"][item_id] = {
                "document": document,
                "embedding": list(embedding),
                "metadata": dict(metadata),
            }
        return len(item_ids)

    async def validate(self, collection_name: str) -> CollectionValidationResult:
        collection = self.collections.get(collection_name)
        if collection is None:
            return CollectionValidationResult(exists=False, valid=False, errors=("collection_not_found",))
        return CollectionValidationResult(exists=True, valid=True, count=len(collection["items"]))

    async def delete(self, collection_name: str, item_ids: list[str], **_kwargs: Any) -> int:
        self.delete_calls.append((collection_name, list(item_ids)))
        if self.delete_error is not None:
            raise self.delete_error
        collection = self.collections.get(collection_name)
        if collection is None:
            return len(item_ids)
        for item_id in item_ids:
            collection["items"].pop(item_id, None)
        return len(item_ids)

    def add_item(
        self,
        collection_name: str,
        item_id: str,
        *,
        document: str,
        metadata: dict[str, Any],
        embedding: list[float] | None = None,
    ) -> None:
        collection = self.collections.setdefault(collection_name, {"metadata": {}, "items": {}})
        collection["items"][item_id] = {
            "document": document,
            "embedding": list(embedding or [0.1, 0.2, 0.3]),
            "metadata": dict(metadata),
        }


@pytest.fixture
def vector_backend(monkeypatch: pytest.MonkeyPatch) -> _FakeVectorBackend:
    backend = _FakeVectorBackend()
    monkeypatch.setattr(memory_prepare_handler, "load_embedding_runtime_config", backend.load_config)
    monkeypatch.setattr(memory_publication_validation_handler, "load_embedding_runtime_config", backend.load_config)
    monkeypatch.setattr(memory_execution_handler, "embed_texts_with_config", backend.embed)
    monkeypatch.setattr(memory_execution_handler, "async_get_or_create_collection", backend.get_or_create_collection)
    monkeypatch.setattr(memory_execution_handler, "async_upsert_collection_items", backend.upsert)
    monkeypatch.setattr(memory_cleanup_handler, "async_validate_collection", backend.validate)
    monkeypatch.setattr(memory_cleanup_handler, "async_delete_collection_items", backend.delete)
    monkeypatch.setattr(memory_vector_handler, "async_validate_collection", backend.validate)
    monkeypatch.setattr(memory_vector_handler, "async_delete_collection_items", backend.delete)
    monkeypatch.setattr(memory_vector_cleanup, "async_validate_collection", backend.validate)
    monkeypatch.setattr(memory_vector_cleanup, "async_delete_collection_items", backend.delete)
    return backend


@pytest_asyncio.fixture
async def memory_session_factory(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(tables=MEMORY_TABLES) as factory:
        yield factory


def _runtime_config(
    *,
    channel_id: int = 1,
    model_id: str = "memory-model-v1",
    dimensions: int = 3,
) -> EmbeddingRuntimeConfig:
    return EmbeddingRuntimeConfig(
        channel_id=channel_id,
        channel_name=f"channel-{channel_id}",
        model_id=model_id,
        declared_dimensions=dimensions,
        protocol="openai_embedding",
        timeout=30.0,
        base_url="https://embedding.invalid/v1",
        api_key="test-api-key",
    )


async def _configure_store(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    channel_id: int = 1,
    model_id: str = "memory-model-v1",
    dimensions: int = 3,
    signature: str = "memory-signature-v1",
    revision: int = 1,
    collection_name: str = "memory-collection-v1",
    migration_job_id: int | None = None,
    migration_status: LongTermMemoryMigrationStatus | None = None,
    index_status: LongTermMemoryIndexStatus | None = None,
) -> None:
    values: dict[str, Any] = {
        "uid": uid,
        "active_embedding_channel_id": channel_id,
        "active_embedding_model_id": model_id,
        "active_embedding_dimensions": dimensions,
        "active_embedding_signature": signature,
        "active_embedding_revision": revision,
        "active_collection_name": collection_name,
        "max_active_records": 50,
        "migration_job_id": migration_job_id,
        "migration_status": migration_status,
        "migration_delta_high_watermark": 0,
    }
    if index_status is not None:
        values["index_status"] = index_status
    async with session_factory() as db:
        await memory_store_crud.create(db, **values)


def _consumer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    executor: MemoryJobExecutor | None = None,
    max_concurrency: int = 1,
) -> MemoryJobConsumer:
    if executor is None:
        executor = memory_handlers.create_default_memory_job_executor(session_factory=session_factory)
    return MemoryJobConsumer(
        executor,
        session_factory,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        lease_seconds=30,
        renew_interval_seconds=10,
        recovery_interval_seconds=1_000_000,
        max_concurrency=max_concurrency,
        recovery_retry_delay_seconds=1,
        shutdown_retry_delay_seconds=0.01,
    )


async def _wait_for_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
    status: LongTermMemoryMutationStatus,
) -> Any:
    deadline = asyncio.get_running_loop().time() + WAIT_TIMEOUT_SECONDS
    while True:
        async with session_factory() as db:
            job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)
        if job is not None and job.status == status:
            return job
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"job {job_id} did not reach {status.value}")
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))


async def _get_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
) -> Any:
    async with session_factory() as db:
        return await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)


async def _get_record(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    memory_id: int,
) -> Any:
    async with session_factory() as db:
        return await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)


async def _get_revisions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    memory_id: int,
) -> list[Any]:
    async with session_factory() as db:
        return await memory_revision_crud.list_by_memory_id(db, uid=uid, memory_id=memory_id)


async def _get_deltas(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    migration_job_id: int,
) -> list[Any]:
    async with session_factory() as db:
        return await memory_embedding_delta_crud.list_by_migration_job(
            db,
            uid=uid,
            migration_job_id=migration_job_id,
        )


async def _run_job(
    consumer: MemoryJobConsumer,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
    status: LongTermMemoryMutationStatus,
) -> Any:
    deadline = asyncio.get_running_loop().time() + WAIT_TIMEOUT_SECONDS
    while True:
        async with session_factory() as db:
            job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)
        if job is not None and job.status == status:
            return job
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"job {job_id} did not reach {status.value}")
        await consumer.run_once()
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            raise AssertionError(f"job {job_id} did not reach {status.value}")
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))


async def _make_direct_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    operation: LongTermMemoryMutationOperation,
    dedupe_key: str,
    payload: dict[str, Any] | None = None,
    active_mutation_key: str | None = None,
    memory_id: int | None = None,
    expected_version: int | None = None,
    max_attempts: int = 3,
) -> int:
    async with session_factory() as db:
        available_at = await get_database_time(db)
        job, created = await memory_job_crud.create(
            db,
            uid=uid,
            operation=operation,
            dedupe_key=dedupe_key,
            payload=payload or {},
            active_mutation_key=active_mutation_key,
            memory_id=memory_id,
            expected_version=expected_version,
            max_attempts=max_attempts,
            available_at=available_at,
        )
    assert created
    assert job.id is not None
    return job.id


async def _create_service_memory(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    dedupe_key: str,
    content: str,
    memory_key: str,
    max_attempts: int = 3,
) -> Any:
    async with session_factory() as db:
        result = await memory_service.create(
            db,
            uid=uid,
            dedupe_key=dedupe_key,
            content=content,
            memory_key=memory_key,
            memory_type=LongTermMemoryType.FACT,
            change_evidence="memory_worker",
            source=LongTermMemorySource.USER_API,
            max_attempts=max_attempts,
        )
    assert result.job is not None
    assert result.job.id is not None
    return result


async def _update_service_memory(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    dedupe_key: str,
    memory_id: int,
    expected_version: int,
    content: str,
    memory_key: str,
    suppress_current: bool = False,
    max_attempts: int = 3,
) -> Any:
    async with session_factory() as db:
        result = await memory_service.update(
            db,
            uid=uid,
            dedupe_key=dedupe_key,
            memory_id=memory_id,
            expected_version=expected_version,
            content=content,
            memory_key=memory_key,
            memory_type=LongTermMemoryType.FACT,
            change_evidence="updated",
            source=LongTermMemorySource.USER_API,
            suppress_current=suppress_current,
            max_attempts=max_attempts,
        )
    assert result.job is not None
    assert result.job.id is not None
    return result


async def _seed_ready_record(
    session_factory: async_sessionmaker[AsyncSession],
    backend: _FakeVectorBackend,
    *,
    uid: str,
    memory_key: str = "old-key",
    content: str = "old content",
    collection_name: str = "memory-collection-v1",
    version: int = 1,
) -> int:
    content_hash = build_memory_content_hash(content)
    async with session_factory() as db:
        record = await memory_record_crud.create(
            db,
            uid=uid,
            memory_key=memory_key,
            content=content,
            content_token_count=estimate_tokens(normalize_memory_content(content)),
            content_hash=content_hash,
            memory_type=LongTermMemoryType.FACT,
            version=version,
            indexed_version=version,
            source=LongTermMemorySource.USER_API,
            is_active=True,
            index_status=LongTermMemoryRecordIndexStatus.READY,
        )
        assert record.id is not None
        vector_item_id = build_memory_vector_item_id(record.id, version)
        record.vector_item_id = vector_item_id
        await db.commit()
        await memory_revision_crud.create(
            db,
            uid=uid,
            memory_id=record.id,
            version=version,
            memory_key=memory_key,
            memory_type=LongTermMemoryType.FACT,
            content=content,
            content_token_count=estimate_tokens(normalize_memory_content(content)),
            content_hash=content_hash,
            source=LongTermMemorySource.USER_API,
            change_evidence="seed",
        )
    backend.add_item(
        collection_name,
        vector_item_id,
        document=content,
        metadata={
            "memory_id": record.id,
            "uid": uid,
            "memory_key": memory_key,
            "memory_type": LongTermMemoryType.FACT.value,
            "version": version,
            "source": LongTermMemorySource.USER_API.value,
            "embedding_revision": 1,
        },
    )
    return record.id


async def _seed_full_ready_records(
    session_factory: async_sessionmaker[AsyncSession],
    backend: _FakeVectorBackend,
    *,
    uid: str,
    collection_name: str = "memory-collection-v1",
    count: int = 50,
) -> list[int]:
    """Seed a complete, deterministically ordered active capacity in one transaction."""
    async with session_factory() as db:
        now = await get_database_time(db)
        records: list[LongTermMemoryRecord] = []
        revisions: list[LongTermMemoryRevision] = []
        for memory_id in range(1, count + 1):
            memory_key = f"seed-key-{memory_id}"
            content = f"seed content {memory_id}"
            normalized_content = normalize_memory_content(content)
            content_hash = build_memory_content_hash(normalized_content)
            content_token_count = estimate_tokens(normalized_content)
            vector_item_id = build_memory_vector_item_id(memory_id, 1)
            records.append(
                LongTermMemoryRecord(
                    id=memory_id,
                    uid=uid,
                    memory_key=memory_key,
                    memory_type=LongTermMemoryType.FACT,
                    content=normalized_content,
                    content_token_count=content_token_count,
                    content_hash=content_hash,
                    version=1,
                    indexed_version=1,
                    vector_item_id=vector_item_id,
                    source=LongTermMemorySource.USER_API,
                    change_evidence="capacity seed",
                    is_active=True,
                    pinned=False,
                    pending_mutation_job_id=None,
                    suppress_recall=False,
                    index_status=LongTermMemoryRecordIndexStatus.READY,
                    created_at=now,
                    updated_at=now,
                    indexed_at=now,
                )
            )
        db.add_all(records)
        await db.flush()
        for record in records:
            revisions.append(
                LongTermMemoryRevision(
                    uid=uid,
                    memory_id=record.id,
                    version=1,
                    memory_key=record.memory_key or "",
                    memory_type=LongTermMemoryType.FACT,
                    content=record.content,
                    content_token_count=record.content_token_count,
                    content_hash=record.content_hash,
                    source=LongTermMemorySource.USER_API,
                    change_evidence="capacity seed",
                    published_at=now,
                    created_at=now,
                )
            )
        db.add_all(revisions)
        await db.commit()

    for record in records:
        backend.add_item(
            collection_name,
            record.vector_item_id or "",
            document=record.content,
            metadata={
                "memory_id": record.id,
                "uid": uid,
                "memory_key": record.memory_key,
                "memory_type": LongTermMemoryType.FACT.value,
                "version": 1,
                "source": LongTermMemorySource.USER_API.value,
                "embedding_revision": 1,
            },
        )
    return [record.id for record in records if record.id is not None]


async def _make_available_now(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
) -> None:
    async with session_factory() as db:
        now = await get_database_time(db)
        await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).values(available_at=now))
        await db.commit()


@pytest.mark.asyncio
async def test_default_memory_worker_executor_operations_and_factory_are_shared(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "default-executor-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()

    executor = memory_handlers.create_default_memory_job_executor(session_factory=memory_session_factory)
    expected = frozenset(
        {
            LongTermMemoryMutationOperation.CREATE,
            LongTermMemoryMutationOperation.UPDATE,
            LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            LongTermMemoryMutationOperation.DELETE_CLEANUP,
            LongTermMemoryMutationOperation.VECTOR_CLEANUP,
            LongTermMemoryMutationOperation.REINDEX,
            LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            LongTermMemoryMutationOperation.ORGANIZE,
            LongTermMemoryMutationOperation.ORGANIZE_MERGE,
        }
    )
    assert executor.enabled_operations == expected
    assert LongTermMemoryMutationOperation.EXTRACT not in executor.enabled_operations

    consumer = create_memory_job_consumer(session_factory=memory_session_factory)
    assert consumer._session_factory is memory_session_factory
    assert consumer._executor._session_factory is memory_session_factory
    try:
        result = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="default-create",
            content="default worker content",
            memory_key="default-key",
        )
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_create_publishes_record_revision_vector_and_migration_delta_atomically(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "create-e2e-user"
    migration_job_id = 91
    await _configure_store(
        memory_session_factory,
        uid=uid,
        migration_job_id=migration_job_id,
        migration_status=LongTermMemoryMigrationStatus.BUILDING,
    )
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    consumer = _consumer(memory_session_factory)
    try:
        result = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-e2e",
            content="Alice uses a local test store.",
            memory_key="profile.fact",
        )
        async with memory_session_factory() as db:
            assert await memory_job_crud.count_pending_create(db, uid=uid) == 1
            assert await memory_record_crud.count_active(db, uid=uid) == 0
        job = await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.PENDING,
        )
        assert job.active_mutation_key is not None
        assert job.payload["content"] == "Alice uses a local test store."
        assert job.payload["content_token_count"] == estimate_tokens(job.payload["content"])

        finished = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        record = await _get_record(memory_session_factory, uid=uid, memory_id=finished.memory_id)
        assert record is not None
        assert record.version == 1
        assert record.indexed_version == 1
        assert record.index_status == LongTermMemoryRecordIndexStatus.READY
        assert record.is_active is True
        assert record.pending_mutation_job_id is None
        assert record.content_token_count == estimate_tokens(record.content)
        assert record.vector_item_id is not None
        assert record.vector_item_id.startswith(build_memory_vector_item_id(record.id, 1))

        revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=record.id)
        assert [revision.version for revision in revisions] == [1]
        assert revisions[0].content == record.content
        assert revisions[0].content_token_count == record.content_token_count

        collection = vector_backend.collections["memory-collection-v1"]
        assert set(collection["items"]) == {record.vector_item_id}
        item = collection["items"][record.vector_item_id]
        assert item["metadata"]["uid"] == uid
        assert item["metadata"]["version"] == 1
        assert item["metadata"]["embedding_revision"] == 1
        assert uid not in collection["metadata"]
        assert collection["metadata"]["uid_sha256"]
        assert collection["metadata"]["uid_sha256"] != uid
        assert finished.result is not None
        assert "content" not in finished.result
        assert "content" not in (finished.payload or {}) or finished.result.get("content") is None
        assert finished.active_mutation_key is None
        assert finished.result["version"] == 1
        async with memory_session_factory() as db:
            assert await memory_job_crud.count_pending_create(db, uid=uid) == 0
            assert await memory_record_crud.count_active(db, uid=uid) == 1

        deltas = await _get_deltas(memory_session_factory, uid=uid, migration_job_id=migration_job_id)
        assert len(deltas) == 1
        assert deltas[0].action == LongTermMemoryEmbeddingDeltaAction.UPSERT
        assert deltas[0].memory_id == record.id
        assert deltas[0].memory_version == 1
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_distinct_user_creates_allocate_database_ids_concurrently(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    users = ("concurrent-id-user-a", "concurrent-id-user-b")
    for uid in users:
        await _configure_store(memory_session_factory, uid=uid, collection_name=f"memory-collection-{uid}")
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    placeholder_barrier = asyncio.Barrier(2)
    original_create_placeholder = memory_record_crud.create_pending_placeholder

    async def create_placeholder_at_barrier(db: AsyncSession, **kwargs: Any) -> Any:
        await placeholder_barrier.wait()
        return await original_create_placeholder(db, **kwargs)

    async def unlocked_store_lookup(db: AsyncSession, *, uid: str, commit: bool = True) -> Any:
        return await memory_store_crud.get_by_uid(db, uid=uid)

    monkeypatch.setattr(memory_record_crud, "create_pending_placeholder", create_placeholder_at_barrier)
    monkeypatch.setattr(memory_prepare_handler.memory_store_crud, "lock_for_mutation", unlocked_store_lookup)
    submissions = await asyncio.gather(
        *(
            _create_service_memory(
                memory_session_factory,
                uid=uid,
                dedupe_key=f"concurrent-create-{uid}",
                content=f"content for {uid}",
                memory_key=f"key-{uid}",
            )
            for uid in users
        )
    )
    assert all(submission.job.memory_id is None for submission in submissions)
    consumer = _consumer(memory_session_factory, max_concurrency=2)
    try:
        assert await consumer.run_once() == 2
        finished_jobs = await asyncio.gather(
            *(
                _wait_for_job(
                    memory_session_factory,
                    uid=uid,
                    job_id=submission.job.id,
                    status=LongTermMemoryMutationStatus.SUCCEEDED,
                )
                for uid, submission in zip(users, submissions, strict=True)
            )
        )
    finally:
        await consumer.stop()

    memory_ids = [job.memory_id for job in finished_jobs]
    assert all(isinstance(memory_id, int) and memory_id > 0 for memory_id in memory_ids)
    assert len(set(memory_ids)) == 2
    for uid, job in zip(users, finished_jobs, strict=True):
        memory_id = job.memory_id
        assert memory_id is not None
        record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)
        assert record is not None
        assert record.id == memory_id
        assert record.version == 1
        assert record.vector_item_id is not None
        assert revisions and revisions[0].memory_id == memory_id
        assert record.vector_item_id in vector_backend.collections[f"memory-collection-{uid}"]["items"]


@pytest.mark.asyncio
async def test_create_fails_before_embedding_when_existing_active_record_is_over_limit(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "create-over-limit-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    consumer = _consumer(memory_session_factory)
    try:
        result = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-over-limit",
            content="new create content",
            memory_key="new-create-key",
        )
        async with memory_session_factory() as db:
            await memory_record_crud.create(
                db,
                uid=uid,
                memory_key="legacy-over-limit",
                content="legacy content",
                content_token_count=MEMORY_CONTENT_MAX_TOKENS + 1,
                content_hash=build_memory_content_hash("legacy content"),
                memory_type=LongTermMemoryType.FACT,
                version=1,
                indexed_version=1,
                source=LongTermMemorySource.USER_API,
                is_active=True,
                index_status=LongTermMemoryRecordIndexStatus.READY,
            )

        failed = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.error == t(ERR_MEMORY_OVER_LIMIT)
        assert failed.memory_id is None
        assert vector_backend.embedding_calls == []
        async with memory_session_factory() as db:
            assert await memory_job_crud.count_pending_create(db, uid=uid) == 0
            assert await memory_record_crud.count_active(db, uid=uid) == 1
            assert await memory_record_crud.get_by_key(db, uid=uid, memory_key="new-create-key") is None
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_update_shortens_existing_active_over_limit_record_and_publishes(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "update-over-limit-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    async with memory_session_factory() as db:
        old_record = await memory_record_crud.create(
            db,
            uid=uid,
            memory_key="legacy-over-limit",
            content="legacy content",
            content_token_count=MEMORY_CONTENT_MAX_TOKENS + 1,
            content_hash=build_memory_content_hash("legacy content"),
            memory_type=LongTermMemoryType.FACT,
            version=1,
            indexed_version=1,
            source=LongTermMemorySource.USER_API,
            is_active=True,
            index_status=LongTermMemoryRecordIndexStatus.READY,
        )
    assert old_record.id is not None

    consumer = _consumer(memory_session_factory)
    try:
        result = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="update-over-limit",
            memory_id=old_record.id,
            expected_version=1,
            content="short content",
            memory_key="shortened-key",
        )
        finished = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert finished.memory_id == old_record.id
        record = await _get_record(memory_session_factory, uid=uid, memory_id=old_record.id)
        assert record is not None
        assert record.version == 2
        assert record.content == "short content"
        assert record.content_token_count == estimate_tokens(record.content)
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_update_keeps_old_ready_version_during_embedding_then_publishes_v2_and_cleans_v1(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "update-e2e-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid)
    started = asyncio.Event()
    release = asyncio.Event()

    async def embedding_hook(_config: EmbeddingRuntimeConfig, texts: list[str]) -> None:
        if texts == ["new content"]:
            started.set()
            await release.wait()

    vector_backend.embedding_hook = embedding_hook
    consumer = _consumer(memory_session_factory)
    try:
        result = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="update-e2e",
            memory_id=memory_id,
            expected_version=1,
            content="new content",
            memory_key="new-key",
        )
        assert await consumer.run_once() == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        during = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert during is not None
        assert during.version == 1
        assert during.indexed_version == 1
        assert during.index_status == LongTermMemoryRecordIndexStatus.READY
        assert during.is_active is True
        assert during.vector_item_id == build_memory_vector_item_id(memory_id, 1)

        release.set()
        finished = await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert record is not None
        assert record.version == 2
        assert record.indexed_version == 2
        assert record.index_status == LongTermMemoryRecordIndexStatus.READY
        assert record.content == "new content"
        assert record.content_token_count == estimate_tokens(record.content)
        assert record.suppress_recall is False
        assert record.vector_item_id is not None
        assert record.vector_item_id.startswith(build_memory_vector_item_id(memory_id, 2))
        assert record.vector_item_id in vector_backend.collections["memory-collection-v1"]["items"]
        assert build_memory_vector_item_id(memory_id, 1) not in vector_backend.collections["memory-collection-v1"]["items"]
        assert [
            revision.version
            for revision in await _get_revisions(
                memory_session_factory,
                uid=uid,
                memory_id=memory_id,
            )
        ] == [2, 1]
        revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)
        assert revisions[0].content_token_count == record.content_token_count
        assert finished.result["version"] == 2
    finally:
        release.set()
        await consumer.stop()


@pytest.mark.asyncio
async def test_update_persists_superseded_vector_cleanup_when_immediate_delete_fails(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "update-vector-cleanup-compensation-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid, version=1)
    old_vector_item_id = build_memory_vector_item_id(memory_id, 1)
    consumer = _consumer(memory_session_factory)
    try:
        result = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="update-vector-cleanup-compensation",
            memory_id=memory_id,
            expected_version=1,
            content="updated content",
            memory_key="updated-key",
        )
        vector_backend.delete_error = RuntimeError("delete failed")
        finished = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )

        assert finished.result is not None
        cleanup_job_id = finished.result["superseded_vector_cleanup_job_id"]
        assert isinstance(cleanup_job_id, int) and cleanup_job_id > 0
        new_vector_item_id = finished.result["vector_item_id"]
        collection = vector_backend.collections["memory-collection-v1"]
        assert old_vector_item_id in collection["items"]
        assert new_vector_item_id in collection["items"]

        cleanup_job = await _get_job(
            memory_session_factory,
            uid=uid,
            job_id=cleanup_job_id,
        )
        assert cleanup_job is not None
        assert cleanup_job.parent_job_id == result.job.id
        assert cleanup_job.operation == LongTermMemoryMutationOperation.VECTOR_CLEANUP
        assert cleanup_job.status == LongTermMemoryMutationStatus.PENDING
        assert cleanup_job.payload["reason"] == "superseded"
        assert cleanup_job.payload["collection_name"] == "memory-collection-v1"
        assert cleanup_job.payload["item_id"] == old_vector_item_id

        vector_backend.delete_error = None
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=cleanup_job_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert old_vector_item_id not in collection["items"]
        assert new_vector_item_id in collection["items"]
    finally:
        await consumer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "credential_kind, create_content, update_content",
    [
        pytest.param(
            "password",
            "password = p@ss word",
            "password = n3w p@ss word",
            id="password",
        ),
        pytest.param(
            "Token",
            "Token: bearer-test-token",
            "Token: bearer-updated-token",
            id="token",
        ),
        pytest.param(
            "API Key",
            "API Key: api-key-test-value",
            "API Key: api-key-updated-value",
            id="api-key",
        ),
        pytest.param(
            "PRIVATE KEY",
            "-----BEGIN PRIVATE KEY-----\nprivate-key-body\n-----END PRIVATE KEY-----",
            "-----BEGIN PRIVATE KEY-----\nupdated-private-key-body\n-----END PRIVATE KEY-----",
            id="private-key",
        ),
    ],
)
async def test_plaintext_credential_lifecycle_is_published_recalled_deleted_and_retained_in_history(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
    credential_kind: str,
    create_content: str,
    update_content: str,
) -> None:
    uid = f"plaintext-{credential_kind.lower().replace(' ', '-')}-lifecycle"
    create_input = f"  {create_content}\n"
    update_input = f"\t{update_content} \n"
    expected_create = normalize_memory_content(create_input)
    expected_update = normalize_memory_content(update_input)
    create_key = f"credential.{credential_kind.lower().replace(' ', '_')}"
    update_key = f"{create_key}.updated"

    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    consumer = _consumer(memory_session_factory)
    try:
        create_result = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key=f"{credential_kind}-lifecycle-create",
            content=create_input,
            memory_key=create_key,
        )
        created_job = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=create_result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert created_job.memory_id is not None
        assert created_job.result["version"] == 1
        memory_id = created_job.memory_id
        created_record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert created_record is not None
        assert created_record.version == 1
        assert created_record.content == expected_create
        assert created_record.content_hash == build_memory_content_hash(expected_create)
        assert created_record.vector_item_id is not None
        assert created_record.vector_item_id.startswith(build_memory_vector_item_id(memory_id, 1))
        assert created_record.vector_item_id in vector_backend.collections["memory-collection-v1"]["items"]

        update_result = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key=f"{credential_kind}-lifecycle-update",
            memory_id=memory_id,
            expected_version=1,
            content=update_input,
            memory_key=update_key,
        )
        updated_job = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=update_result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert updated_job.memory_id == memory_id
        assert updated_job.result["version"] == 2
        updated_record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert updated_record is not None
        assert updated_record.version == 2
        assert updated_record.content == expected_update
        assert updated_record.content_hash == build_memory_content_hash(expected_update)
        assert updated_record.vector_item_id is not None
        assert updated_record.vector_item_id.startswith(build_memory_vector_item_id(memory_id, 2))
        assert updated_record.vector_item_id in vector_backend.collections["memory-collection-v1"]["items"]
        assert build_memory_vector_item_id(memory_id, 1) not in vector_backend.collections["memory-collection-v1"]["items"]

        async def fake_loader(_db: AsyncSession, _channel_id: int, _model_id: str) -> object:
            return _runtime_config()

        async def fake_embed(_config: object, _texts: list[str], **_kwargs: Any) -> list[list[float]]:
            return [[0.1, 0.2, 0.3]]

        async def fake_query(
            collection_name: str,
            vector: list[float],
            query: str,
            limit: int,
        ) -> list[RetrievalHit]:
            assert collection_name == "memory-collection-v1"
            assert vector == [0.1, 0.2, 0.3]
            assert query == "credential recall query"
            assert limit == 10
            return [
                RetrievalHit(
                    id=updated_record.vector_item_id,
                    metadata={
                        "uid": uid,
                        "memory_id": memory_id,
                        "version": 2,
                        "embedding_revision": 1,
                    },
                    fusion_score=1.0,
                    content="untrusted vector content",
                )
            ]

        monkeypatch.setattr(memory_recall_service_module, "load_embedding_runtime_config", fake_loader)
        monkeypatch.setattr(memory_recall_service_module, "embed_texts_with_config", fake_embed)
        monkeypatch.setattr(memory_recall_service_module, "_hybrid_query_collection", fake_query)

        async with memory_session_factory() as db:
            recalled = await memory_service.recall(
                db,
                uid=uid,
                query=" credential\trecall query ",
                top_k=1,
                candidate_k=10,
            )
        assert recalled.status == MemoryRecallStatus.OK
        assert len(recalled.items) == 1
        assert recalled.items[0].version == 2
        assert recalled.items[0].content == expected_update

        async with memory_session_factory() as db:
            delete_result = await memory_service.delete(
                db,
                uid=uid,
                dedupe_key=f"{credential_kind}-lifecycle-delete",
                memory_id=memory_id,
                expected_version=2,
            )
        assert delete_result.job is not None
        assert delete_result.job.operation == LongTermMemoryMutationOperation.DELETE_CLEANUP

        tombstone = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert tombstone is not None
        assert tombstone.is_active is False
        assert tombstone.deleted_at is not None
        async with memory_session_factory() as db:
            empty = await memory_service.recall(db, uid=uid, query="credential recall query")
        assert empty.status == MemoryRecallStatus.EMPTY

        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=delete_result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        cleaned = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert cleaned is None
        assert vector_backend.collections["memory-collection-v1"]["items"] == {}

        delete_job = await _get_job(
            memory_session_factory,
            uid=uid,
            job_id=delete_result.job.id,
        )
        assert delete_job is not None
        assert delete_job.payload["record_snapshot"]["content"] == expected_update
        assert delete_job.payload["record_snapshot"]["content_token_count"] == estimate_tokens(expected_update)
        assert delete_job.payload["record_snapshot"]["memory_key"] == update_key
        assert delete_job.payload["record_snapshot"]["version"] == 2
        assert delete_job.result["record_snapshot"] == delete_job.payload["record_snapshot"]

        revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)
        assert [revision.version for revision in revisions] == [2, 1]
        assert {revision.version: revision.content for revision in revisions} == {
            1: expected_create,
            2: expected_update,
        }
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_successful_suppressed_update_clears_suppression_when_v2_is_published(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "suppressed-success-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid)
    consumer = _consumer(memory_session_factory)
    try:
        result = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="suppressed-success",
            memory_id=memory_id,
            expected_version=1,
            content="suppressed success content",
            memory_key="suppressed-success-key",
            suppress_current=True,
        )
        during = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert during is not None
        assert during.suppress_recall is True
        assert during.suppressed_by_job_id == result.job.id
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert record is not None
        assert record.version == 2
        assert record.suppress_recall is False
        assert record.suppressed_by_job_id is None
        assert record.pending_mutation_job_id is None
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_update_suppress_current_is_cleared_after_success_and_worker_rechecks_version_conflict(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "update-conflict-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid)
    consumer = _consumer(memory_session_factory)
    try:
        suppressed = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="suppressed-update",
            memory_id=memory_id,
            expected_version=1,
            content="suppressed update",
            memory_key="suppressed-key",
            suppress_current=True,
        )
        before = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert before is not None
        assert before.suppress_recall is True
        assert before.suppressed_by_job_id == suppressed.job.id

        async with memory_session_factory() as db:
            await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == suppressed.job.id).values(expected_version=0))
            await db.commit()
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=suppressed.job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        conflicted_record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert conflicted_record is not None
        assert conflicted_record.version == 1
        assert conflicted_record.content == "old content"
        assert conflicted_record.pending_mutation_job_id is None
        assert conflicted_record.suppress_recall is True
        assert conflicted_record.suppressed_by_job_id == suppressed.job.id

        async with memory_session_factory() as db:
            resumed = await memory_service.resume_current(
                db,
                uid=uid,
                memory_id=memory_id,
                expected_version=1,
            )
        assert resumed.status.value == "resumed"
        after_resume = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert after_resume is not None
        assert after_resume.suppress_recall is False
        assert after_resume.suppressed_by_job_id is None
    finally:
        await consumer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("collection_present", [True, False])
async def test_delete_cleanup_tombstones_immediately_then_physically_deletes_record(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    collection_present: bool,
) -> None:
    uid = f"delete-worker-user-{collection_present}"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid)
    consumer = _consumer(memory_session_factory)
    try:
        async with memory_session_factory() as db:
            submission = await memory_service.delete(
                db,
                uid=uid,
                dedupe_key="delete-cleanup",
                memory_id=memory_id,
                expected_version=1,
            )
        assert submission.job is not None
        immediate = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert immediate is not None
        assert immediate.is_active is False
        assert immediate.deleted_at is not None
        assert immediate.content == "old content"
        assert immediate.vector_item_id == build_memory_vector_item_id(memory_id, 1)

        if not collection_present:
            vector_backend.collections.pop("memory-collection-v1", None)
        finished = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=submission.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert record is None
        assert len(await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)) == 1
        assert finished.active_mutation_key is None
        assert finished.payload["record_snapshot"]["content"] == "old content"
        assert finished.result["record_snapshot"] == finished.payload["record_snapshot"]

        if collection_present:
            item_id = build_memory_vector_item_id(memory_id, 1)
            await vector_backend.delete("memory-collection-v1", [item_id])
            await vector_backend.delete("memory-collection-v1", [item_id])
            assert item_id not in vector_backend.collections["memory-collection-v1"]["items"]
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_create_after_physical_delete_does_not_reuse_memory_id(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "delete-id-allocation-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    deleted_memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid)
    consumer = _consumer(memory_session_factory)
    try:
        async with memory_session_factory() as db:
            deleted = await memory_service.delete(
                db,
                uid=uid,
                dedupe_key="delete-before-create",
                memory_id=deleted_memory_id,
                expected_version=1,
            )
        assert deleted.job is not None
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=deleted.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )

        created = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-after-delete",
            content="new memory after deletion",
            memory_key="new-key-after-delete",
        )
        finished = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=created.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )

        assert finished.memory_id > deleted_memory_id
        assert (
            await _get_record(
                memory_session_factory,
                uid=uid,
                memory_id=deleted_memory_id,
            )
            is None
        )
        assert (
            await _get_record(
                memory_session_factory,
                uid=uid,
                memory_id=finished.memory_id,
            )
            is not None
        )
    finally:
        await consumer.stop()
