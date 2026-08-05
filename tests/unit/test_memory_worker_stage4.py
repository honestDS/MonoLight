import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.memory import (
    memory_embedding_delta_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.memory import (
    MemoryNotFoundError,
    MemoryRecallStatus,
    build_memory_content_hash,
    build_memory_vector_item_id,
    memory_service,
    normalize_memory_content,
)
from app.core.memory import service as memory_service_module
from app.core.memory_jobs.consumer import MemoryJobConsumer, create_memory_job_consumer
from app.core.memory_jobs.executor import (
    MemoryJobDeterministicError,
    MemoryJobExecutionResult,
    MemoryJobExecutor,
)
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingDeltaAction,
    LongTermMemoryEmbeddingRevision,
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
) -> None:
    async with session_factory() as db:
        await memory_store_crud.create(
            db,
            uid=uid,
            active_embedding_channel_id=channel_id,
            active_embedding_model_id=model_id,
            active_embedding_dimensions=dimensions,
            active_embedding_signature=signature,
            active_embedding_revision=revision,
            active_collection_name=collection_name,
            max_active_records=50,
            migration_job_id=migration_job_id,
            migration_status=migration_status,
            migration_delta_high_watermark=0,
        )


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
    assert await consumer.run_once() == 1
    return await _wait_for_job(session_factory, uid=uid, job_id=job_id, status=status)


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
            importance=7,
            scope="test",
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
            importance=8,
            scope="test",
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
            content_hash=content_hash,
            memory_type=LongTermMemoryType.FACT,
            importance=5,
            scope="test",
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
            importance=5,
            scope="test",
            content=content,
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
            "importance": 5,
            "version": version,
            "source": LongTermMemorySource.USER_API.value,
            "embedding_revision": 1,
        },
    )
    return record.id


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
            LongTermMemoryMutationOperation.RESTORE,
            LongTermMemoryMutationOperation.DELETE_CLEANUP,
            LongTermMemoryMutationOperation.REINDEX,
            LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
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
        job = await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=result.job.id,
            status=LongTermMemoryMutationStatus.PENDING,
        )
        assert job.active_mutation_key is not None
        assert job.payload["content"] == "Alice uses a local test store."

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
        assert record.vector_item_id == build_memory_vector_item_id(record.id, 1)

        revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=record.id)
        assert [revision.version for revision in revisions] == [1]
        assert revisions[0].content == record.content

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

        deltas = await _get_deltas(memory_session_factory, uid=uid, migration_job_id=migration_job_id)
        assert len(deltas) == 1
        assert deltas[0].action == LongTermMemoryEmbeddingDeltaAction.UPSERT
        assert deltas[0].memory_id == record.id
        assert deltas[0].memory_version == 1
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
        assert record.suppress_recall is False
        assert record.vector_item_id == build_memory_vector_item_id(memory_id, 2)
        assert build_memory_vector_item_id(memory_id, 1) not in vector_backend.collections["memory-collection-v1"]["items"]
        assert build_memory_vector_item_id(memory_id, 2) in vector_backend.collections["memory-collection-v1"]["items"]
        assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [2, 1]
        assert finished.result["version"] == 2
    finally:
        release.set()
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
        assert created_record.vector_item_id == build_memory_vector_item_id(memory_id, 1)
        assert build_memory_vector_item_id(memory_id, 1) in vector_backend.collections["memory-collection-v1"]["items"]

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
        assert updated_record.vector_item_id == build_memory_vector_item_id(memory_id, 2)
        assert build_memory_vector_item_id(memory_id, 1) not in vector_backend.collections["memory-collection-v1"]["items"]
        assert build_memory_vector_item_id(memory_id, 2) in vector_backend.collections["memory-collection-v1"]["items"]

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
                    id=build_memory_vector_item_id(memory_id, 2),
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
async def test_completed_delete_rejects_restore_and_keeps_history(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "restore-worker-user"
    await _configure_store(memory_session_factory, uid=uid)
    vector_backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid)
    consumer = _consumer(memory_session_factory)
    try:
        async with memory_session_factory() as db:
            deleted = await memory_service.delete(
                db,
                uid=uid,
                dedupe_key="delete-before-restore",
                memory_id=memory_id,
                expected_version=1,
            )
        assert deleted.job is not None
        tombstone = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert tombstone is not None
        assert tombstone.is_active is False
        assert tombstone.deleted_at is not None
        await _run_job(
            consumer,
            memory_session_factory,
            uid=uid,
            job_id=deleted.job.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )

        async with memory_session_factory() as db:
            with pytest.raises(MemoryNotFoundError):
                await memory_service.restore(
                    db,
                    uid=uid,
                    dedupe_key="restore-v1",
                    memory_id=memory_id,
                    revision_version=1,
                    expected_version=1,
                )
        record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert record is None
        revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)
        assert [revision.version for revision in revisions] == [1]
        assert revisions[0].content == "old content"
        assert vector_backend.collections["memory-collection-v1"]["items"] == {}
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
        assert restored.vector_item_id == build_memory_vector_item_id(memory_id, 2)
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
