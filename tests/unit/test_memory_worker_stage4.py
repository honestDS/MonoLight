import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

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
    build_memory_staged_vector_item_id,
    build_memory_vector_item_id,
    memory_service,
    normalize_memory_content,
)
from app.core.memory import service as memory_service_module
from app.core.memory_jobs.consumer import MemoryJobConsumer, create_memory_job_consumer
from app.core.memory_jobs.executor import (
    MemoryJobDeterministicError,
    MemoryJobExecutionContext,
    MemoryJobExecutionResult,
    MemoryJobExecutor,
    MemoryJobLeaseLostError,
)
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


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass


with patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient):
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

    async def validate(self, collection_name: str) -> SimpleNamespace:
        return SimpleNamespace(exists=collection_name in self.collections)

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
    monkeypatch.setattr(memory_handlers, "load_embedding_runtime_config", backend.load_config)
    monkeypatch.setattr(memory_handlers, "embed_texts_with_config", backend.embed)
    monkeypatch.setattr(memory_handlers, "async_get_or_create_collection", backend.get_or_create_collection)
    monkeypatch.setattr(memory_handlers, "async_upsert_collection_items", backend.upsert)
    monkeypatch.setattr(memory_handlers, "async_validate_collection", backend.validate)
    monkeypatch.setattr(memory_handlers, "async_delete_collection_items", backend.delete)
    monkeypatch.setattr(memory_vector_cleanup, "async_validate_collection", backend.validate)
    monkeypatch.setattr(memory_vector_cleanup, "async_delete_collection_items", backend.delete)
    return backend


@pytest_asyncio.fixture
async def memory_session_factory(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "memory-worker-stage4.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=MEMORY_TABLES,
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


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
            change_evidence="stage4",
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
async def test_default_stage4_executor_operations_and_factory_are_shared(
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
    monkeypatch.setattr(memory_handlers.memory_store_crud, "lock_for_mutation", unlocked_store_lookup)
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
        ) -> list[SimpleNamespace]:
            assert collection_name == "memory-collection-v1"
            assert vector == [0.1, 0.2, 0.3]
            assert query == "credential recall query"
            assert limit == 10
            return [
                SimpleNamespace(
                    id=updated_record.vector_item_id,
                    metadata={
                        "uid": uid,
                        "memory_id": memory_id,
                        "version": 2,
                        "embedding_revision": 1,
                    },
                    fusion_score=1.0,
                )
            ]

        monkeypatch.setattr(memory_service_module, "load_embedding_runtime_config", fake_loader)
        monkeypatch.setattr(memory_service_module, "embed_texts_with_config", fake_embed)
        monkeypatch.setattr(memory_service_module, "_hybrid_query_collection", fake_query)

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


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["embedding", "vector"])
async def test_transient_embedding_or_vector_error_retries_without_damaging_ready_record_then_succeeds(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    failure_kind: str,
) -> None:
    uid = f"retry-{failure_kind}-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid)
    if failure_kind == "embedding":
        vector_backend.embedding_error = RuntimeError("temporary embedding failure")
    else:
        vector_backend.upsert_error = RuntimeError("temporary vector failure")
    consumer = _consumer(memory_session_factory)
    try:
        result = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key=f"retry-{failure_kind}",
            memory_id=memory_id,
            expected_version=1,
            content="retry content",
            memory_key="retry-key",
        )
        retried = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        assert retried.active_mutation_key is not None
        old_record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert old_record is not None
        assert old_record.version == 1
        assert old_record.index_status == LongTermMemoryRecordIndexStatus.READY
        assert old_record.vector_item_id == build_memory_vector_item_id(memory_id, 1)
        assert old_record.pending_mutation_job_id == result.job.id

        vector_backend.embedding_error = None
        vector_backend.upsert_error = None
        await _make_available_now(memory_session_factory, uid=uid, job_id=result.job.id)
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        finished_record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert finished_record is not None
        assert finished_record.version == 2
        assert finished_record.index_status == LongTermMemoryRecordIndexStatus.READY
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_recovered_owner_cannot_delete_reclaimed_owner_vector_item(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "staged-vector-race-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid)
    submission = await _update_service_memory(
        memory_session_factory,
        uid=uid,
        dedupe_key="staged-vector-race",
        memory_id=memory_id,
        expected_version=1,
        content="reclaimed owner content",
        memory_key="reclaimed-owner-key",
    )
    assert submission.job.id is not None
    job_id = submission.job.id

    async with memory_session_factory() as db:
        old_claim = await memory_job_crud.try_claim(
            db,
            uid=uid,
            job_id=job_id,
            owner="old-owner",
            lease_seconds=1,
            enabled_operations=[LongTermMemoryMutationOperation.UPDATE],
        )
    assert old_claim is not None

    old_written = asyncio.Event()
    release_old_upsert = asyncio.Event()
    upsert_calls = 0

    async def coordinated_upsert(
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict[str, Any]],
        **kwargs: Any,
    ) -> int:
        nonlocal upsert_calls
        upsert_calls += 1
        result = await vector_backend.upsert(
            collection_name,
            item_ids,
            documents,
            embeddings,
            metadatas,
            **kwargs,
        )
        if upsert_calls == 1:
            old_written.set()
            await release_old_upsert.wait()
        return result

    monkeypatch.setattr(memory_handlers, "async_upsert_collection_items", coordinated_upsert)
    old_context = MemoryJobExecutionContext(
        job=old_claim,
        worker_id="old-owner",
        session_factory=memory_session_factory,
    )
    old_task = asyncio.create_task(memory_handlers._handle_update(old_context))
    try:
        await asyncio.wait_for(old_written.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        old_item_id = build_memory_staged_vector_item_id(memory_id, 2, job_id, "old-owner")
        assert old_item_id in vector_backend.collections["memory-collection-v1"]["items"]

        async with memory_session_factory() as db:
            now = await get_database_time(db)
            await db.execute(
                update(LongTermMemoryMutationJob)
                .where(
                    LongTermMemoryMutationJob.uid == uid,
                    LongTermMemoryMutationJob.id == job_id,
                )
                .values(lock_until=now - timedelta(seconds=1))
            )
            await db.commit()
            recovery = await memory_job_crud.recover_expired(db, delay_seconds=0)
        assert recovery.retried == 1

        async with memory_session_factory() as db:
            new_claim = await memory_job_crud.try_claim(
                db,
                uid=uid,
                job_id=job_id,
                owner="new-owner",
                lease_seconds=30,
                enabled_operations=[LongTermMemoryMutationOperation.UPDATE],
            )
        assert new_claim is not None
        new_context = MemoryJobExecutionContext(
            job=new_claim,
            worker_id="new-owner",
            session_factory=memory_session_factory,
        )
        new_result = await memory_handlers._handle_update(new_context)
        assert new_result.finalized
        new_item_id = build_memory_staged_vector_item_id(memory_id, 2, job_id, "new-owner")
        assert new_item_id == old_item_id
        assert new_item_id in vector_backend.collections["memory-collection-v1"]["items"]

        release_old_upsert.set()
        with pytest.raises(MemoryJobLeaseLostError):
            await old_task

        record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert record is not None
        assert record.vector_item_id == new_item_id
        assert new_item_id in vector_backend.collections["memory-collection-v1"]["items"]
    finally:
        release_old_upsert.set()
        if not old_task.done():
            old_task.cancel()
        await asyncio.gather(old_task, return_exceptions=True)


@pytest.mark.asyncio
async def test_max_attempt_embedding_failure_fails_create_and_clears_placeholder_and_active_key(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "max-attempt-create-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    vector_backend.embedding_error = RuntimeError("permanent embedding failure")
    consumer = _consumer(memory_session_factory)
    try:
        result = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="max-attempt-create",
            content="never indexed",
            memory_key="never-indexed",
            max_attempts=1,
        )
        failed = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.active_mutation_key is None
        assert await _get_record(memory_session_factory, uid=uid, memory_id=failed.memory_id) is None
        assert await _get_revisions(memory_session_factory, uid=uid, memory_id=failed.memory_id) == []
        async with memory_session_factory() as db:
            assert await memory_job_crud.count_pending_create(db, uid=uid) == 0
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_retryable_create_failure_keeps_reservation_until_success(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "retry-create-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    vector_backend.embedding_error = RuntimeError("temporary embedding failure")
    consumer = _consumer(memory_session_factory)
    try:
        result = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="retry-create",
            content="retryable create",
            memory_key="retryable-create",
            max_attempts=2,
        )
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        async with memory_session_factory() as db:
            assert await memory_job_crud.count_pending_create(db, uid=uid) == 1
            assert await memory_record_crud.count_active(db, uid=uid) == 0

        vector_backend.embedding_error = None
        await _make_available_now(memory_session_factory, uid=uid, job_id=result.job.id)
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        async with memory_session_factory() as db:
            assert await memory_job_crud.count_pending_create(db, uid=uid) == 0
            assert await memory_record_crud.count_active(db, uid=uid) == 1
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_vector_written_before_publication_failure_is_cleaned_and_database_never_half_publishes(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "publication-transaction-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    consumer = _consumer(memory_session_factory)
    original_publish = memory_handlers.memory_record_crud.publish_pending_version

    async def return_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    try:
        monkeypatch.setattr(memory_handlers.memory_record_crud, "publish_pending_version", return_none)
        create_result = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="publish-none-create",
            content="create publication failure",
            memory_key="publish-none",
            max_attempts=1,
        )
        failed_create = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=create_result.job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed_create.active_mutation_key is None
        assert await _get_record(memory_session_factory, uid=uid, memory_id=failed_create.memory_id) is None
        assert vector_backend.collections["memory-collection-v1"]["items"] == {}

        monkeypatch.setattr(memory_handlers.memory_record_crud, "publish_pending_version", original_publish)
        memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid, memory_key="stable-key")

        async def raise_revision(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("revision storage failure")

        monkeypatch.setattr(memory_handlers.memory_revision_crud, "create", raise_revision)
        update_result = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="revision-write-failure",
            memory_id=memory_id,
            expected_version=1,
            content="revision failure",
            memory_key="revision-failure",
            max_attempts=2,
        )
        retried = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=update_result.job.id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        assert retried.active_mutation_key is not None
        stable = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert stable is not None
        assert stable.version == 1
        assert stable.content == "old content"
        assert stable.vector_item_id == build_memory_vector_item_id(memory_id, 1)
        assert build_memory_vector_item_id(memory_id, 2) not in vector_backend.collections["memory-collection-v1"]["items"]

        await _make_available_now(memory_session_factory, uid=uid, job_id=update_result.job.id)
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=update_result.job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        final_stable = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert final_stable is not None
        assert final_stable.version == 1
        assert final_stable.pending_mutation_job_id is None
        assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_running_cancel_after_embedding_does_not_publish_create_and_preserves_suppression_on_update(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "cancel-after-call-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    started = asyncio.Event()
    release = asyncio.Event()

    async def embedding_hook(_config: EmbeddingRuntimeConfig, _texts: list[str]) -> None:
        started.set()
        await release.wait()

    vector_backend.embedding_hook = embedding_hook
    consumer = _consumer(memory_session_factory)
    try:
        create_result = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="cancel-create",
            content="cancelled create",
            memory_key="cancel-create-key",
        )
        assert await consumer.run_once() == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        async with memory_session_factory() as db:
            cancellation = await memory_job_crud.request_cancel(db, uid=uid, job_id=create_result.job.id)
        assert cancellation.accepted
        release.set()
        await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=create_result.job.id,
            status=LongTermMemoryMutationStatus.CANCELLED,
        )
        cancelled_create = await _get_job(memory_session_factory, uid=uid, job_id=create_result.job.id)
        assert cancelled_create.active_mutation_key is None
        assert await _get_record(memory_session_factory, uid=uid, memory_id=cancelled_create.memory_id) is None
        assert vector_backend.collections["memory-collection-v1"]["items"] == {}
        async with memory_session_factory() as db:
            assert await memory_job_crud.count_pending_create(db, uid=uid) == 0

        vector_backend.embedding_hook = None
        old_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid, memory_key="cancel-old")
        gate_started = asyncio.Event()
        gate_release = asyncio.Event()

        async def update_hook(_config: EmbeddingRuntimeConfig, texts: list[str]) -> None:
            if texts == ["cancelled update"]:
                gate_started.set()
                await gate_release.wait()

        vector_backend.embedding_hook = update_hook
        update_result = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="cancel-update",
            memory_id=old_id,
            expected_version=1,
            content="cancelled update",
            memory_key="cancel-update-key",
            suppress_current=True,
        )
        assert await consumer.run_once() == 1
        await asyncio.wait_for(gate_started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        suppressed = await _get_record(memory_session_factory, uid=uid, memory_id=old_id)
        assert suppressed is not None
        assert suppressed.suppress_recall is True
        async with memory_session_factory() as db:
            cancellation = await memory_job_crud.request_cancel(db, uid=uid, job_id=update_result.job.id)
        assert cancellation.accepted
        gate_release.set()
        await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=update_result.job.id,
            status=LongTermMemoryMutationStatus.CANCELLED,
        )
        cancelled_update = await _get_record(memory_session_factory, uid=uid, memory_id=old_id)
        assert cancelled_update is not None
        assert cancelled_update.version == 1
        assert cancelled_update.content == "old content"
        assert cancelled_update.vector_item_id == build_memory_vector_item_id(old_id, 1)
        assert cancelled_update.pending_mutation_job_id is None
        assert cancelled_update.suppress_recall is True
        assert cancelled_update.suppressed_by_job_id == update_result.job.id
        assert build_memory_vector_item_id(old_id, 2) not in vector_backend.collections["memory-collection-v1"]["items"]
    finally:
        release.set()
        gate_release.set()
        await consumer.stop()


@pytest.mark.asyncio
async def test_active_store_change_during_embedding_retries_cleans_new_item_and_restores_success(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "active-config-change-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    vector_backend.runtime_configs[(1, "memory-model-v2")] = _runtime_config(model_id="memory-model-v2")
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid)
    changed = False

    async def embedding_hook(_config: EmbeddingRuntimeConfig, _texts: list[str]) -> None:
        nonlocal changed
        if changed:
            return
        changed = True
        async with memory_session_factory() as db:
            await memory_store_crud.update_by_uid(
                db,
                uid=uid,
                active_embedding_model_id="memory-model-v2",
                active_embedding_signature="memory-signature-v2",
                active_embedding_revision=2,
                active_collection_name="memory-collection-v2",
            )

    vector_backend.embedding_hook = embedding_hook
    consumer = _consumer(memory_session_factory)
    try:
        result = await _update_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="active-config-change",
            memory_id=memory_id,
            expected_version=1,
            content="config changed content",
            memory_key="config-changed",
        )
        retried = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        assert retried.active_mutation_key is not None
        assert vector_backend.collections["memory-collection-v1"]["items"] == {build_memory_vector_item_id(memory_id, 1): vector_backend.collections["memory-collection-v1"]["items"][build_memory_vector_item_id(memory_id, 1)]}
        unchanged = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert unchanged is not None
        assert unchanged.version == 1
        assert unchanged.content == "old content"

        async with memory_session_factory() as db:
            await memory_store_crud.update_by_uid(
                db,
                uid=uid,
                active_embedding_model_id="memory-model-v1",
                active_embedding_signature="memory-signature-v1",
                active_embedding_revision=1,
                active_collection_name="memory-collection-v1",
            )
        await _make_available_now(memory_session_factory, uid=uid, job_id=result.job.id)
        vector_backend.embedding_hook = None
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        restored = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert restored is not None
        assert restored.version == 2
        assert restored.content == "config changed content"
        assert restored.vector_item_id is not None
        assert restored.vector_item_id.startswith(build_memory_vector_item_id(memory_id, 2))
        assert restored.vector_item_id in vector_backend.collections["memory-collection-v1"]["items"]
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_worker_rejects_finalized_handler_result_when_database_job_is_not_succeeded(
    memory_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    uid = "finalized-result-user"
    await _configure_store(memory_session_factory, uid=uid)
    job_id = await _make_direct_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        dedupe_key="fake-finalized",
    )

    async def fake_finalized_handler(_context: Any) -> MemoryJobExecutionResult:
        return MemoryJobExecutionResult(result={"finalized": True}, finalized=True)

    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.REINDEX: fake_finalized_handler},
        session_factory=memory_session_factory,
    )
    async with memory_session_factory() as db:
        claimed = await memory_job_crud.try_claim(
            db,
            uid=uid,
            job_id=job_id,
            owner="finalized-owner",
            enabled_operations=[LongTermMemoryMutationOperation.REINDEX],
        )
    assert claimed is not None
    with pytest.raises(MemoryJobDeterministicError):
        await executor.execute_claimed(claimed, "finalized-owner")
    current = await _get_job(memory_session_factory, uid=uid, job_id=job_id)
    assert current.status == LongTermMemoryMutationStatus.RUNNING
    assert current.locked_by == "finalized-owner"


@pytest.mark.asyncio
async def test_create_with_eviction_publishes_replacement_and_migration_deltas_before_cleanup(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "create-with-eviction-success-user"
    migration_job_id = 801
    await _configure_store(
        memory_session_factory,
        uid=uid,
        migration_job_id=migration_job_id,
        migration_status=LongTermMemoryMigrationStatus.BUILDING,
        index_status=LongTermMemoryIndexStatus.READY,
    )
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    await _seed_full_ready_records(memory_session_factory, vector_backend, uid=uid)
    started = asyncio.Event()
    release = asyncio.Event()

    async def embedding_hook(_config: EmbeddingRuntimeConfig, texts: list[str]) -> None:
        if texts == ["replacement content"]:
            started.set()
            await release.wait()

    async def recall_loader(_db: AsyncSession, _channel_id: int, _model_id: str) -> EmbeddingRuntimeConfig:
        return _runtime_config()

    async def recall_embed(_config: object, _texts: list[str], **_kwargs: Any) -> list[list[float]]:
        return [[0.1, 0.2, 0.3]]

    async def recall_query(
        _collection_name: str,
        _vector: list[float],
        _query: str,
        limit: int,
        **_kwargs: Any,
    ) -> list[SimpleNamespace]:
        assert limit == 1
        return [
            SimpleNamespace(
                id=build_memory_vector_item_id(1, 1),
                metadata={
                    "uid": uid,
                    "memory_id": 1,
                    "version": 1,
                    "embedding_revision": 1,
                },
                fusion_score=1.0,
            )
        ]

    monkeypatch.setattr(memory_service_module, "load_embedding_runtime_config", recall_loader)
    monkeypatch.setattr(memory_service_module, "embed_texts_with_config", recall_embed)
    monkeypatch.setattr(memory_service_module, "_hybrid_query_collection", recall_query)
    vector_backend.embedding_hook = embedding_hook
    consumer = _consumer(memory_session_factory)
    try:
        submission = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-with-eviction-success",
            content="replacement content",
            memory_key="replacement-key",
        )
        assert submission.job.operation == LongTermMemoryMutationOperation.CREATE_WITH_EVICTION
        assert submission.job.memory_id is None
        candidate_id = submission.job.payload["candidate"]["memory_id"]
        assert candidate_id == 1
        old_item_id = build_memory_vector_item_id(candidate_id, 1)
        old_item = dict(vector_backend.collections["memory-collection-v1"]["items"][old_item_id])

        assert await consumer.run_once() == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        during = await _get_record(memory_session_factory, uid=uid, memory_id=candidate_id)
        assert during is not None
        assert during.is_active is True
        assert during.deleted_at is None
        assert during.pending_mutation_job_id == submission.job.id
        assert old_item_id in vector_backend.collections["memory-collection-v1"]["items"]

        async with memory_session_factory() as db:
            recalled = await memory_service.recall(
                db,
                uid=uid,
                query="seed recall",
                top_k=1,
                candidate_k=1,
            )
        assert recalled.status == MemoryRecallStatus.OK
        assert recalled.items[0].memory_id == candidate_id
        assert recalled.items[0].content == "seed content 1"

        release.set()
        finished = await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=submission.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        finished_memory_id = finished.memory_id
        assert finished_memory_id is not None
        replacement_memory_id = finished_memory_id
        assert finished.result is not None
        assert finished.result["memory_id"] == replacement_memory_id
        assert finished.result["evicted_memory_id"] == candidate_id
        cleanup_job_id = finished.result["cleanup_job_id"]
        assert isinstance(cleanup_job_id, int)

        replacement = await _get_record(memory_session_factory, uid=uid, memory_id=replacement_memory_id)
        assert replacement is not None
        assert replacement.version == 1
        assert replacement.indexed_version == 1
        assert replacement.index_status == LongTermMemoryRecordIndexStatus.READY
        assert replacement.is_active is True
        assert replacement.pending_mutation_job_id is None
        assert replacement.content_token_count == estimate_tokens("replacement content")
        assert replacement.source == LongTermMemorySource.USER_API
        assert replacement.vector_item_id is not None
        assert replacement.vector_item_id.startswith(build_memory_vector_item_id(replacement_memory_id, 1))
        replacement_revisions = await _get_revisions(
            memory_session_factory,
            uid=uid,
            memory_id=replacement_memory_id,
        )
        assert [revision.version for revision in replacement_revisions] == [1]
        assert replacement_revisions[0].source == LongTermMemorySource.USER_API

        tombstone = await _get_record(memory_session_factory, uid=uid, memory_id=candidate_id)
        assert tombstone is not None
        assert tombstone.is_active is False
        assert tombstone.deleted_at is not None
        assert tombstone.pending_mutation_job_id == cleanup_job_id
        assert tombstone.vector_item_id == old_item_id
        cleanup_job = await _get_job(memory_session_factory, uid=uid, job_id=cleanup_job_id)
        assert cleanup_job is not None
        assert cleanup_job.operation == LongTermMemoryMutationOperation.DELETE_CLEANUP
        assert cleanup_job.memory_id == candidate_id
        assert cleanup_job.expected_version == 1
        assert cleanup_job.status == LongTermMemoryMutationStatus.PENDING

        async with memory_session_factory() as db:
            assert await memory_record_crud.count_active(db, uid=uid) == 50
            store = await memory_store_crud.get_by_uid(db, uid=uid)
            assert store is not None
            assert store.migration_delta_high_watermark == 2
        assert old_item_id in vector_backend.collections["memory-collection-v1"]["items"]
        assert replacement.vector_item_id in vector_backend.collections["memory-collection-v1"]["items"]
        assert vector_backend.collections["memory-collection-v1"]["items"][old_item_id] == old_item

        deltas = await _get_deltas(memory_session_factory, uid=uid, migration_job_id=migration_job_id)
        assert [delta.sequence for delta in deltas] == [1, 2]
        assert [delta.action for delta in deltas] == [
            LongTermMemoryEmbeddingDeltaAction.UPSERT,
            LongTermMemoryEmbeddingDeltaAction.DELETE,
        ]
        assert deltas[0].memory_id == replacement_memory_id
        assert deltas[0].memory_version == 1
        assert deltas[0].source_mutation_job_id == submission.job.id
        assert deltas[1].memory_id == candidate_id
        assert deltas[1].memory_version == 1
        assert deltas[1].source_mutation_job_id == submission.job.id

        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=cleanup_job_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert await _get_record(memory_session_factory, uid=uid, memory_id=candidate_id) is None
        assert old_item_id not in vector_backend.collections["memory-collection-v1"]["items"]
        assert replacement.vector_item_id in vector_backend.collections["memory-collection-v1"]["items"]
        assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=candidate_id)] == [1]
    finally:
        release.set()
        await consumer.stop()


@pytest.mark.asyncio
async def test_create_with_eviction_permanent_embedding_failure_preserves_capacity_and_candidate(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "create-with-eviction-embedding-failure-user"
    migration_job_id = 802
    await _configure_store(
        memory_session_factory,
        uid=uid,
        migration_job_id=migration_job_id,
        migration_status=LongTermMemoryMigrationStatus.BUILDING,
        index_status=LongTermMemoryIndexStatus.READY,
    )
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    await _seed_full_ready_records(memory_session_factory, vector_backend, uid=uid)
    vector_backend.embedding_error = RuntimeError("permanent replacement embedding failure")
    consumer = _consumer(memory_session_factory)
    try:
        submission = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-with-eviction-embedding-failure",
            content="failed replacement content",
            memory_key="failed-replacement-key",
            max_attempts=1,
        )
        candidate_id = submission.job.payload["candidate"]["memory_id"]
        candidate_item_id = build_memory_vector_item_id(candidate_id, 1)
        old_item = dict(vector_backend.collections["memory-collection-v1"]["items"][candidate_item_id])
        failed = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=submission.job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.active_mutation_key is None
        assert failed.memory_id is not None
        replacement_memory_id = failed.memory_id
        assert await _get_record(memory_session_factory, uid=uid, memory_id=replacement_memory_id) is None
        assert await _get_revisions(memory_session_factory, uid=uid, memory_id=replacement_memory_id) == []
        candidate = await _get_record(memory_session_factory, uid=uid, memory_id=candidate_id)
        assert candidate is not None
        assert candidate.is_active is True
        assert candidate.deleted_at is None
        assert candidate.version == 1
        assert candidate.indexed_version == 1
        assert candidate.vector_item_id == candidate_item_id
        assert candidate.content == "seed content 1"
        assert candidate.content_token_count == estimate_tokens(candidate.content)
        assert candidate.source == LongTermMemorySource.USER_API
        assert candidate.pending_mutation_job_id is None
        assert vector_backend.upsert_calls == []
        assert build_memory_vector_item_id(replacement_memory_id, 1) not in vector_backend.collections["memory-collection-v1"]["items"]
        assert vector_backend.collections["memory-collection-v1"]["items"][candidate_item_id] == old_item
        assert await _get_deltas(memory_session_factory, uid=uid, migration_job_id=migration_job_id) == []
        async with memory_session_factory() as db:
            assert await memory_record_crud.count_active(db, uid=uid) == 50
            assert (
                await memory_job_crud.list_by_uid(
                    db,
                    uid=uid,
                    operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                )
                == []
            )
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_create_with_eviction_retry_reuses_placeholder_memory_id(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "create-with-eviction-retry-user"
    await _configure_store(memory_session_factory, uid=uid, index_status=LongTermMemoryIndexStatus.READY)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    await _seed_full_ready_records(memory_session_factory, vector_backend, uid=uid)
    vector_backend.embedding_error = RuntimeError("temporary replacement embedding failure")
    consumer = _consumer(memory_session_factory)
    try:
        submission = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-with-eviction-retry",
            content="retry replacement content",
            memory_key="retry-replacement-key",
            max_attempts=2,
        )
        retried = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=submission.job.id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        placeholder_id = retried.memory_id
        assert placeholder_id is not None
        placeholder = await _get_record(memory_session_factory, uid=uid, memory_id=placeholder_id)
        assert placeholder is not None
        assert placeholder.version == 0
        assert placeholder.pending_mutation_job_id == retried.id

        vector_backend.embedding_error = None
        await _make_available_now(memory_session_factory, uid=uid, job_id=submission.job.id)
        succeeded = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=submission.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert succeeded.memory_id == placeholder_id
        published = await _get_record(memory_session_factory, uid=uid, memory_id=placeholder_id)
        assert published is not None
        assert published.version == 1
        assert published.is_active is True
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_running_create_with_eviction_cancel_before_embedding_return_cleans_orphan_and_releases_candidate(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "create-with-eviction-cancel-user"
    migration_job_id = 803
    await _configure_store(
        memory_session_factory,
        uid=uid,
        migration_job_id=migration_job_id,
        migration_status=LongTermMemoryMigrationStatus.BUILDING,
        index_status=LongTermMemoryIndexStatus.READY,
    )
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    await _seed_full_ready_records(memory_session_factory, vector_backend, uid=uid)
    started = asyncio.Event()
    release = asyncio.Event()

    async def embedding_hook(_config: EmbeddingRuntimeConfig, texts: list[str]) -> None:
        if texts == ["cancelled replacement content"]:
            started.set()
            await release.wait()

    vector_backend.embedding_hook = embedding_hook
    consumer = _consumer(memory_session_factory)
    try:
        submission = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-with-eviction-cancel",
            content="cancelled replacement content",
            memory_key="cancelled-replacement-key",
        )
        assert await consumer.run_once() == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        candidate_id = submission.job.payload["candidate"]["memory_id"]
        async with memory_session_factory() as db:
            cancellation = await memory_job_crud.request_cancel(db, uid=uid, job_id=submission.job.id)
        assert cancellation.accepted
        release.set()
        cancelled = await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=submission.job.id,
            status=LongTermMemoryMutationStatus.CANCELLED,
        )
        assert cancelled.active_mutation_key is None
        assert cancelled.memory_id is not None
        replacement_memory_id = cancelled.memory_id
        candidate = await _get_record(memory_session_factory, uid=uid, memory_id=candidate_id)
        assert candidate is not None
        assert candidate.is_active is True
        assert candidate.deleted_at is None
        assert candidate.pending_mutation_job_id is None
        assert await _get_record(memory_session_factory, uid=uid, memory_id=replacement_memory_id) is None
        assert await _get_revisions(memory_session_factory, uid=uid, memory_id=replacement_memory_id) == []
        assert build_memory_vector_item_id(replacement_memory_id, 1) not in vector_backend.collections["memory-collection-v1"]["items"]
        assert await _get_deltas(memory_session_factory, uid=uid, migration_job_id=migration_job_id) == []
        async with memory_session_factory() as db:
            assert await memory_record_crud.count_active(db, uid=uid) == 50
            assert (
                await memory_job_crud.list_by_uid(
                    db,
                    uid=uid,
                    operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                )
                == []
            )
    finally:
        release.set()
        await consumer.stop()


@pytest.mark.asyncio
async def test_create_with_eviction_pinned_candidate_competition_rejects_publication_and_cleans_new_vector(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "create-with-eviction-pinned-user"
    migration_job_id = 804
    await _configure_store(
        memory_session_factory,
        uid=uid,
        migration_job_id=migration_job_id,
        migration_status=LongTermMemoryMigrationStatus.BUILDING,
        index_status=LongTermMemoryIndexStatus.READY,
    )
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    await _seed_full_ready_records(memory_session_factory, vector_backend, uid=uid)
    started = asyncio.Event()
    release = asyncio.Event()

    async def embedding_hook(_config: EmbeddingRuntimeConfig, texts: list[str]) -> None:
        if texts == ["pinned replacement content"]:
            started.set()
            await release.wait()

    vector_backend.embedding_hook = embedding_hook
    consumer = _consumer(memory_session_factory)
    try:
        submission = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-with-eviction-pinned",
            content="pinned replacement content",
            memory_key="pinned-replacement-key",
            max_attempts=1,
        )
        assert await consumer.run_once() == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        candidate_id = submission.job.payload["candidate"]["memory_id"]
        async with memory_session_factory() as db:
            pinned = await memory_record_crud.set_pinned(db, uid=uid, memory_id=candidate_id, pinned=True)
        assert pinned is not None
        assert pinned.pinned is True
        release.set()
        failed = await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=submission.job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.memory_id is not None
        replacement_memory_id = failed.memory_id
        candidate = await _get_record(memory_session_factory, uid=uid, memory_id=candidate_id)
        assert candidate is not None
        assert candidate.pinned is True
        assert candidate.is_active is True
        assert candidate.deleted_at is None
        assert candidate.pending_mutation_job_id is None
        assert await _get_record(memory_session_factory, uid=uid, memory_id=replacement_memory_id) is None
        assert await _get_revisions(memory_session_factory, uid=uid, memory_id=replacement_memory_id) == []
        assert build_memory_vector_item_id(replacement_memory_id, 1) not in vector_backend.collections["memory-collection-v1"]["items"]
        assert await _get_deltas(memory_session_factory, uid=uid, migration_job_id=migration_job_id) == []
        async with memory_session_factory() as db:
            assert await memory_record_crud.count_active(db, uid=uid) == 50
            assert (
                await memory_job_crud.list_by_uid(
                    db,
                    uid=uid,
                    operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                )
                == []
            )
    finally:
        release.set()
        await consumer.stop()


@pytest.mark.asyncio
async def test_create_with_eviction_cleanup_job_failure_rolls_back_publication_and_cleans_new_vector(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "create-with-eviction-cleanup-failure-user"
    migration_job_id = 805
    await _configure_store(
        memory_session_factory,
        uid=uid,
        migration_job_id=migration_job_id,
        migration_status=LongTermMemoryMigrationStatus.BUILDING,
        index_status=LongTermMemoryIndexStatus.READY,
    )
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    await _seed_full_ready_records(memory_session_factory, vector_backend, uid=uid)

    async def raise_cleanup_job(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("cleanup job creation failure")

    monkeypatch.setattr(memory_handlers.memory_job_manager, "create_eviction_cleanup_job", raise_cleanup_job)
    consumer = _consumer(memory_session_factory)
    try:
        submission = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-with-eviction-cleanup-failure",
            content="cleanup failure replacement content",
            memory_key="cleanup-failure-replacement-key",
            max_attempts=1,
        )
        candidate_id = submission.job.payload["candidate"]["memory_id"]
        candidate_item_id = build_memory_vector_item_id(candidate_id, 1)
        failed = await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=submission.job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.memory_id is not None
        replacement_memory_id = failed.memory_id
        assert await _get_record(memory_session_factory, uid=uid, memory_id=replacement_memory_id) is None
        assert await _get_revisions(memory_session_factory, uid=uid, memory_id=replacement_memory_id) == []
        candidate = await _get_record(memory_session_factory, uid=uid, memory_id=candidate_id)
        assert candidate is not None
        assert candidate.is_active is True
        assert candidate.deleted_at is None
        assert candidate.pending_mutation_job_id is None
        assert candidate.vector_item_id == candidate_item_id
        assert build_memory_vector_item_id(replacement_memory_id, 1) not in vector_backend.collections["memory-collection-v1"]["items"]
        assert candidate_item_id in vector_backend.collections["memory-collection-v1"]["items"]
        assert await _get_deltas(memory_session_factory, uid=uid, migration_job_id=migration_job_id) == []
        async with memory_session_factory() as db:
            assert await memory_record_crud.count_active(db, uid=uid) == 50
            assert (
                await memory_job_crud.list_by_uid(
                    db,
                    uid=uid,
                    operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
                )
                == []
            )
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_create_with_eviction_store_active_or_index_revision_change_during_embedding_rejects_publication(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "create-with-eviction-store-change-user"
    migration_job_id = 806
    await _configure_store(
        memory_session_factory,
        uid=uid,
        migration_job_id=migration_job_id,
        migration_status=LongTermMemoryMigrationStatus.BUILDING,
        index_status=LongTermMemoryIndexStatus.READY,
    )
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    await _seed_full_ready_records(memory_session_factory, vector_backend, uid=uid)
    started = asyncio.Event()
    release = asyncio.Event()

    async def embedding_hook(_config: EmbeddingRuntimeConfig, texts: list[str]) -> None:
        if texts == ["store changed replacement content"]:
            started.set()
            async with memory_session_factory() as db:
                changed = await memory_store_crud.update_by_uid(
                    db,
                    uid=uid,
                    active_embedding_model_id="memory-model-v2",
                    active_embedding_signature="memory-signature-v2",
                    active_embedding_revision=2,
                    active_collection_name="memory-collection-v2",
                    index_revision=2,
                )
                assert changed is not None
            await release.wait()

    vector_backend.embedding_hook = embedding_hook
    consumer = _consumer(memory_session_factory)
    try:
        submission = await _create_service_memory(
            memory_session_factory,
            uid=uid,
            dedupe_key="create-with-eviction-store-change",
            content="store changed replacement content",
            memory_key="store-changed-replacement-key",
            max_attempts=1,
        )
        assert await consumer.run_once() == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        candidate_id = submission.job.payload["candidate"]["memory_id"]
        release.set()
        failed = await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=submission.job.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.memory_id is not None
        replacement_memory_id = failed.memory_id
        candidate = await _get_record(memory_session_factory, uid=uid, memory_id=candidate_id)
        assert candidate is not None
        assert candidate.is_active is True
        assert candidate.deleted_at is None
        assert candidate.pending_mutation_job_id is None
        assert await _get_record(memory_session_factory, uid=uid, memory_id=replacement_memory_id) is None
        assert await _get_revisions(memory_session_factory, uid=uid, memory_id=replacement_memory_id) == []
        assert build_memory_vector_item_id(replacement_memory_id, 1) not in vector_backend.collections["memory-collection-v1"]["items"]
        assert await _get_deltas(memory_session_factory, uid=uid, migration_job_id=migration_job_id) == []
        async with memory_session_factory() as db:
            assert await memory_record_crud.count_active(db, uid=uid) == 50
            store = await memory_store_crud.get_by_uid(db, uid=uid)
            assert store is not None
            assert store.active_embedding_revision == 2
            assert store.index_revision == 2
    finally:
        release.set()
        await consumer.stop()


@pytest.mark.asyncio
async def test_expired_update_recovery_creates_staged_vector_cleanup_job(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "staged-vector-recovery-cleanup-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid, version=1)
    old_item_id = build_memory_vector_item_id(memory_id, 1)
    submission = await _update_service_memory(
        memory_session_factory,
        uid=uid,
        dedupe_key="staged-vector-recovery-cleanup",
        memory_id=memory_id,
        expected_version=1,
        content="staged recovery content",
        memory_key="staged-recovery-key",
        max_attempts=1,
    )
    assert submission.job.id is not None
    parent_job_id = submission.job.id

    async with memory_session_factory() as db:
        crashed_claim = await memory_job_crud.try_claim(
            db,
            uid=uid,
            job_id=parent_job_id,
            owner="crashed-owner",
            lease_seconds=1,
            enabled_operations=[LongTermMemoryMutationOperation.UPDATE],
        )
    assert crashed_claim is not None
    staged_item_id = build_memory_staged_vector_item_id(memory_id, 2, parent_job_id, "crashed-owner")
    context = MemoryJobExecutionContext(
        job=crashed_claim,
        worker_id="crashed-owner",
        session_factory=memory_session_factory,
    )
    await memory_vector_cleanup.persist_staged_vector_reference(
        context,
        collection_name="memory-collection-v1",
        item_id=staged_item_id,
    )
    vector_backend.add_item(
        "memory-collection-v1",
        staged_item_id,
        document="staged recovery content",
        metadata={
            "memory_id": memory_id,
            "uid": uid,
            "memory_key": "staged-recovery-key",
            "memory_type": LongTermMemoryType.FACT.value,
            "version": 2,
            "source": LongTermMemorySource.USER_API.value,
            "embedding_revision": 1,
        },
    )
    assert staged_item_id in vector_backend.collections["memory-collection-v1"]["items"]

    async with memory_session_factory() as db:
        now = await get_database_time(db)
        await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == parent_job_id,
            )
            .values(lock_until=now - timedelta(seconds=1))
        )
        await db.commit()

    consumer = _consumer(memory_session_factory)
    try:
        await consumer._recover_expired()
        assert await consumer.run_once() == 1
        await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=parent_job_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )

        async with memory_session_factory() as db:
            cleanup_jobs = await memory_job_crud.list_children_by_parent_job_id(
                db,
                uid=uid,
                parent_job_id=parent_job_id,
            )
        assert len(cleanup_jobs) == 1
        cleanup_job = cleanup_jobs[0]
        assert cleanup_job.operation == LongTermMemoryMutationOperation.VECTOR_CLEANUP
        assert cleanup_job.payload["reason"] == "staged"
        assert cleanup_job.payload["item_id"] == staged_item_id
        assert cleanup_job.payload["collection_name"] == "memory-collection-v1"
        assert cleanup_job.id is not None

        await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=cleanup_job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert staged_item_id not in vector_backend.collections["memory-collection-v1"]["items"]
        assert old_item_id in vector_backend.collections["memory-collection-v1"]["items"]
    finally:
        await consumer.stop()
