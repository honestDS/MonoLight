from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
    ERR_MEMORY_MAINTENANCE_STATE_CONFLICT,
    MEMORY_CONTENT_MAX_TOKENS,
)
from app.core.crud.memory import (
    memory_embedding_revision_crud,
    memory_record_crud,
    memory_revision_crud,
    memory_store_crud,
)
from app.core.crud.memory_job import memory_job_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.i18n import t
from app.core.memory import (
    build_memory_content_hash,
    build_memory_organization_active_mutation_key,
    build_memory_record_snapshot,
    build_memory_staged_vector_item_id,
    build_memory_vector_item_id,
    normalize_memory_content,
    normalize_memory_key,
    retry_job,
)
from app.core.memory.errors import MemoryConflictError
from app.core.memory.organization import (
    MemoryOrganizationModelConfig,
    MemoryOrganizationValidatedItem,
    MemoryOrganizationValidatedSource,
    MemoryOrganizationValidatedTarget,
    build_organization_execution_request,
    build_organization_job_payload,
    build_organization_snapshot,
    calculate_organization_required_output_tokens,
)
from app.core.memory_jobs.consumer import MemoryJobConsumer
from app.core.memory_jobs.executor import MemoryJobExecutor
from app.core.memory_jobs.manager import MemoryJobManager, MemoryJobTargetBusyError
from app.core.utils.tokenizer import estimate_tokens
from app.models.channel import ModelProtocol, ModelUsage
from app.models.memory import (
    LongTermMemoryCapacityStatus,
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryEmbeddingRevisionStatus,
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
ACTIVE_EMBEDDING_REVISION = 3
INDEX_REVISION = 8
POLICY_VERSION = 5
COLLECTION_NAME = "organization-merge-collection"


def _staged_vector_id_prefix(memory_id: int, version: int, job_id: int) -> str:
    staged_id = build_memory_staged_vector_item_id(memory_id, version, job_id, "test-owner")
    return staged_id.rsplit("_o", 1)[0] + "_o"


class _FakeVectorBackend:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.runtime_configs: dict[tuple[int, str], EmbeddingRuntimeConfig] = {}
        self.embedding_calls: list[tuple[EmbeddingRuntimeConfig, list[str]]] = []
        self.upsert_calls: list[dict[str, Any]] = []
        self.delete_calls: list[tuple[str, list[str]]] = []
        self.delete_failures_remaining = 0
        self.embedding_hook: Callable[[EmbeddingRuntimeConfig, list[str]], Awaitable[None]] | None = None
        self.upsert_hook: Callable[[str, list[str]], Awaitable[None]] | None = None

    async def load_config(self, _db: Any, channel_id: int, model_id: str) -> EmbeddingRuntimeConfig:
        return self.runtime_configs[(channel_id, model_id)]

    async def get_or_create_collection(
        self,
        collection_name: str,
        *,
        metadata: dict[str, Any] | None = None,
        distance: str | None = None,
    ) -> dict[str, Any]:
        return self.collections.setdefault(
            collection_name,
            {
                "metadata": {**(metadata or {}), **({"hnsw:space": distance} if distance else {})},
                "items": {},
            },
        )

    async def embed(
        self,
        config: EmbeddingRuntimeConfig,
        texts: list[str],
        **_kwargs: Any,
    ) -> list[list[float]]:
        self.embedding_calls.append((config, list(texts)))
        if self.embedding_hook is not None:
            await self.embedding_hook(config, texts)
        return [[0.1, 0.2, 0.3] for _ in texts]

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
        collection = self.collections.setdefault(collection_name, {"metadata": {}, "items": {}})
        for item_id, document, embedding, metadata in zip(item_ids, documents, embeddings, metadatas, strict=True):
            collection["items"][item_id] = {
                "document": document,
                "embedding": list(embedding),
                "metadata": dict(metadata),
            }
        if self.upsert_hook is not None:
            await self.upsert_hook(collection_name, list(item_ids))
        return len(item_ids)

    async def validate(self, collection_name: str) -> SimpleNamespace:
        return SimpleNamespace(exists=collection_name in self.collections)

    async def delete(self, collection_name: str, item_ids: list[str], **_kwargs: Any) -> int:
        self.delete_calls.append((collection_name, list(item_ids)))
        if self.delete_failures_remaining > 0:
            self.delete_failures_remaining -= 1
            raise RuntimeError("simulated vector delete failure")
        collection = self.collections.get(collection_name)
        if collection is not None:
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
    ) -> None:
        collection = self.collections.setdefault(collection_name, {"metadata": {}, "items": {}})
        collection["items"][item_id] = {
            "document": document,
            "embedding": [0.1, 0.2, 0.3],
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
    database_path = tmp_path / "memory-organization-merge-stage10.db"
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
    migration_job_id: int | None = None,
    migration_status: LongTermMemoryMigrationStatus | None = None,
    capacity_status: LongTermMemoryCapacityStatus = LongTermMemoryCapacityStatus.NORMAL,
) -> None:
    async with session_factory() as db:
        await memory_store_crud.create(
            db,
            uid=uid,
            active_embedding_channel_id=1,
            active_embedding_model_id="memory-model-v1",
            active_embedding_dimensions=3,
            active_embedding_signature="organization-embedding-signature",
            active_embedding_revision=ACTIVE_EMBEDDING_REVISION,
            active_collection_name=COLLECTION_NAME,
            max_active_records=50,
            migration_job_id=migration_job_id,
            migration_status=migration_status,
            migration_delta_high_watermark=0,
            migration_delta_applied_watermark=0,
            index_revision=INDEX_REVISION,
            index_status=LongTermMemoryIndexStatus.READY,
            capacity_status=capacity_status,
        )


def _organization_model(snapshot_count: int) -> MemoryOrganizationModelConfig:
    required_output_tokens = calculate_organization_required_output_tokens(snapshot_count)
    context_window_k = 64
    return MemoryOrganizationModelConfig(
        channel_id=7,
        channel_name="organization-test-channel",
        model_id="organization-test-model",
        usage=ModelUsage.CHAT.value,
        protocol=ModelProtocol.OPENAI.value.lower(),
        context_window_k=context_window_k,
        context_window_tokens=context_window_k * CONTEXT_WINDOW_TOKENS_PER_K,
        max_tokens=required_output_tokens,
        snapshot_count=snapshot_count,
        required_output_tokens=required_output_tokens,
        policy_version=POLICY_VERSION,
        base_url="https://organization.invalid/v1",
        api_key="organization-test-api-key",
        http_proxy=None,
        custom_headers={},
        temperature=0.2,
        top_p=0.8,
        timeout=30.0,
    )


async def _seed_records(
    db: AsyncSession,
    *,
    uid: str,
    memory_ids: tuple[int, ...],
    pinned_ids: frozenset[int],
) -> list[LongTermMemoryRecord]:
    recalled_at = await get_database_time(db)
    records: list[LongTermMemoryRecord] = []
    for memory_id in memory_ids:
        memory_key = normalize_memory_key(f"source-key-{memory_id}")
        content = normalize_memory_content(f"source content {memory_id}")
        content_hash = build_memory_content_hash(content)
        token_count = estimate_tokens(content)
        vector_item_id = build_memory_vector_item_id(memory_id, 1)
        record = await memory_record_crud.create(
            db,
            uid=uid,
            id=memory_id,
            memory_key=memory_key,
            content=content,
            content_token_count=token_count,
            content_hash=content_hash,
            memory_type=LongTermMemoryType.FACT,
            version=1,
            indexed_version=1,
            vector_item_id=vector_item_id,
            source=LongTermMemorySource.USER_API,
            change_evidence=f"seed evidence {memory_id}",
            is_active=True,
            pinned=memory_id in pinned_ids,
            last_recalled_at=recalled_at if memory_id == min(memory_ids) else None,
            pending_mutation_job_id=None,
            suppress_recall=False,
            index_status=LongTermMemoryRecordIndexStatus.READY,
            indexed_at=recalled_at,
            commit=False,
        )
        await memory_revision_crud.create(
            db,
            uid=uid,
            memory_id=memory_id,
            version=1,
            memory_key=memory_key,
            memory_type=LongTermMemoryType.FACT,
            content=content,
            content_token_count=token_count,
            content_hash=content_hash,
            source=LongTermMemorySource.USER_API,
            source_job_id=None,
            change_evidence=f"seed evidence {memory_id}",
            published_at=recalled_at,
            commit=False,
        )
        records.append(record)
    return records


def _validated_item(
    records: list[LongTermMemoryRecord],
    *,
    action: str,
    primary_memory_id: int,
    target_content: str,
    target_memory_key: str,
) -> MemoryOrganizationValidatedItem:
    normalized_content = normalize_memory_content(target_content)
    normalized_key = normalize_memory_key(target_memory_key)
    target = MemoryOrganizationValidatedTarget(
        content=normalized_content,
        memory_key=normalized_key,
        memory_type=LongTermMemoryType.FACT,
        content_token_count=estimate_tokens(normalized_content),
        content_hash=build_memory_content_hash(normalized_content),
    )
    return MemoryOrganizationValidatedItem(
        action=action,
        sources=tuple(
            MemoryOrganizationValidatedSource(
                memory_id=record.id,
                expected_version=record.version,
                pinned=record.pinned,
            )
            for record in records
        ),
        primary_memory_id=primary_memory_id if action == "merge" else None,
        target=target,
    )


async def _prepare_merge(
    session_factory: async_sessionmaker[AsyncSession],
    backend: _FakeVectorBackend,
    *,
    uid: str,
    memory_ids: tuple[int, ...],
    primary_memory_id: int,
    action: str,
    pinned_ids: frozenset[int],
    target_content: str,
    target_memory_key: str,
    max_attempts: int = 3,
    migration_job_id: int | None = None,
    initial_capacity_status: LongTermMemoryCapacityStatus = LongTermMemoryCapacityStatus.NORMAL,
) -> tuple[int, int, tuple[int, ...]]:
    await _configure_store(
        session_factory,
        uid=uid,
        migration_job_id=migration_job_id,
        migration_status=(LongTermMemoryMigrationStatus.BUILDING if migration_job_id is not None else None),
        capacity_status=initial_capacity_status,
    )
    backend.runtime_configs[(1, "memory-model-v1")] = _runtime_config()
    async with session_factory() as db:
        await memory_embedding_revision_crud.create(
            db,
            uid=uid,
            revision=ACTIVE_EMBEDDING_REVISION,
            to_channel_id=1,
            to_model_id="memory-model-v1",
            to_dimensions=3,
            to_signature="organization-embedding-signature",
            to_collection=COLLECTION_NAME,
            status=LongTermMemoryEmbeddingRevisionStatus.CONFIRMED,
            commit=False,
        )
        records = await _seed_records(
            db,
            uid=uid,
            memory_ids=memory_ids,
            pinned_ids=pinned_ids,
        )
        snapshot = build_organization_snapshot(
            records,
            active_embedding_revision=ACTIVE_EMBEDDING_REVISION,
            index_revision=INDEX_REVISION,
            policy_version=POLICY_VERSION,
        )
        organization_model = _organization_model(snapshot.count)
        parent_payload = build_organization_job_payload(snapshot, organization_model)
        request = build_organization_execution_request(parent_payload)
        assert request.organization_model.usage == ModelUsage.CHAT.value
        assert not request.budget.exceeds_hard_window
        parent, created = await memory_job_crud.create(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
            dedupe_key=f"organization-parent-{uid}",
            active_mutation_key=build_memory_organization_active_mutation_key(uid),
            payload=parent_payload,
            max_attempts=max_attempts,
            available_at=await get_database_time(db),
            commit=False,
        )
        assert created and parent.id is not None
        claimed_parent = await memory_job_crud.try_claim(
            db,
            uid=uid,
            job_id=parent.id,
            owner="organization-parent-worker",
            lease_seconds=300,
            commit=False,
        )
        assert claimed_parent is not None
        source_records = records if action == "merge" else [record for record in records if record.id == primary_memory_id]
        assert len(source_records) == len(records) if action == "merge" else len(source_records) == 1
        item = _validated_item(
            source_records,
            action=action,
            primary_memory_id=primary_memory_id,
            target_content=target_content,
            target_memory_key=target_memory_key,
        )
        child = await MemoryJobManager().create_organization_merge_child(
            db,
            parent_job=claimed_parent,
            item=item,
            group_index=0,
            snapshot_digest=snapshot.digest,
            active_embedding_revision=ACTIVE_EMBEDDING_REVISION,
            index_revision=INDEX_REVISION,
            policy_version=POLICY_VERSION,
            commit=False,
        )
        assert child is not None and child.id is not None
        assert await memory_job_crud.mark_succeeded(
            db,
            uid=uid,
            job_id=parent.id,
            owner="organization-parent-worker",
            result={"child_job_ids": [child.id]},
            commit=False,
        )
        await db.commit()

    async with session_factory() as db:
        finalized_parent = await memory_job_crud.get_by_id(db, uid=uid, job_id=parent.id)
    assert finalized_parent is not None
    assert finalized_parent.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert finalized_parent.active_mutation_key is None
    for record in records:
        backend.add_item(
            COLLECTION_NAME,
            build_memory_vector_item_id(record.id, 1),
            document=record.content,
            metadata={
                "memory_id": record.id,
                "uid": uid,
                "memory_key": record.memory_key,
                "memory_type": LongTermMemoryType.FACT.value,
                "version": 1,
                "source": LongTermMemorySource.USER_API.value,
                "embedding_revision": ACTIVE_EMBEDDING_REVISION,
            },
        )
    return parent.id, child.id, memory_ids


def _consumer(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    executor: MemoryJobExecutor | None = None,
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
        max_concurrency=1,
        recovery_retry_delay_seconds=1,
        shutdown_retry_delay_seconds=0.01,
    )


async def _wait_for_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
    status: LongTermMemoryMutationStatus,
) -> LongTermMemoryMutationJob:
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


async def _run_child(
    consumer: MemoryJobConsumer,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    child_id: int,
    status: LongTermMemoryMutationStatus,
) -> LongTermMemoryMutationJob:
    assert await consumer.run_once() == 1
    return await _wait_for_job(session_factory, uid=uid, job_id=child_id, status=status)


async def _get_record(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    memory_id: int,
) -> LongTermMemoryRecord | None:
    async with session_factory() as db:
        return await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)


async def _get_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
) -> LongTermMemoryMutationJob | None:
    async with session_factory() as db:
        return await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)


async def _get_revisions(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    memory_id: int,
) -> list[LongTermMemoryRevision]:
    async with session_factory() as db:
        return await memory_revision_crud.list_by_memory_id(db, uid=uid, memory_id=memory_id)


async def _get_all_deltas(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
) -> list[LongTermMemoryEmbeddingDelta]:
    async with session_factory() as db:
        result = await db.execute(select(LongTermMemoryEmbeddingDelta).where(LongTermMemoryEmbeddingDelta.uid == uid).order_by(LongTermMemoryEmbeddingDelta.sequence))
        return list(result.scalars().all())


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", [LongTermMemoryMutationStatus.PENDING, LongTermMemoryMutationStatus.RETRY])
async def test_pending_and_retry_organization_merge_cancel_releases_all_source_pending_references(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    initial_status: LongTermMemoryMutationStatus,
) -> None:
    uid = f"organization-merge-cancel-{initial_status.value}-worker"
    _parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="cancelled merge content",
        target_memory_key="cancelled-merge-key",
    )

    if initial_status == LongTermMemoryMutationStatus.RETRY:
        async with memory_session_factory() as db:
            claimed = await memory_job_crud.try_claim(
                db,
                uid=uid,
                job_id=child_id,
                owner="organization-merge-retry-worker",
                lease_seconds=30,
                commit=False,
            )
            assert claimed is not None
            assert await memory_job_crud.release_for_retry(
                db,
                uid=uid,
                job_id=child_id,
                owner="organization-merge-retry-worker",
                delay_seconds=0,
                commit=False,
            )
            await db.commit()

    async with memory_session_factory() as db:
        cancellation = await MemoryJobManager().request_cancel(db, uid=uid, job_id=child_id)
    assert cancellation.accepted is True
    assert cancellation.changed is True
    cancelled = await _get_job(memory_session_factory, uid=uid, job_id=child_id)
    assert cancelled is not None
    assert cancelled.status == LongTermMemoryMutationStatus.CANCELLED
    assert cancelled.active_mutation_key is None
    for memory_id in source_ids:
        record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
        assert record is not None
        assert record.is_active is True
        assert record.pending_mutation_job_id is None
    async with memory_session_factory() as db:
        recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=source_ids)
    assert [record.id for record in recallable] == list(source_ids)


@pytest.mark.asyncio
@pytest.mark.parametrize("cancel_phase", ["embedding", "vector_write"])
async def test_running_organization_merge_cancelled_after_external_call_cleans_orphan_and_preserves_old_recallable_records(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    cancel_phase: str,
) -> None:
    uid = f"organization-merge-running-cancel-{cancel_phase}-worker"
    _parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="running cancellation content",
        target_memory_key="running-cancellation-key",
    )
    started = asyncio.Event()
    release_external_call = asyncio.Event()

    async def block_external_call(*_args: Any, **_kwargs: Any) -> None:
        started.set()
        await release_external_call.wait()

    new_vector_prefix = _staged_vector_id_prefix(1, 2, child_id)
    staged_vector_id: str | None = None

    async def block_upsert(collection_name: str, item_ids: list[str]) -> None:
        nonlocal staged_vector_id
        if len(item_ids) != 1 or not item_ids[0].startswith(new_vector_prefix):
            return
        staged_vector_id = item_ids[0]
        await block_external_call(collection_name, item_ids)

    if cancel_phase == "embedding":
        vector_backend.embedding_hook = block_external_call
    else:
        vector_backend.upsert_hook = block_upsert

    consumer = _consumer(memory_session_factory)
    try:
        assert await consumer.run_once() == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        async with memory_session_factory() as db:
            cancellation = await MemoryJobManager().request_cancel(db, uid=uid, job_id=child_id)
        assert cancellation.accepted is True
        assert cancellation.changed is True
        release_external_call.set()

        cancelled = await _wait_for_job(
            memory_session_factory,
            uid=uid,
            job_id=child_id,
            status=LongTermMemoryMutationStatus.CANCELLED,
        )
        assert cancelled.active_mutation_key is None
        collection = vector_backend.collections[COLLECTION_NAME]
        assert staged_vector_id is None or staged_vector_id not in collection["items"]
        assert all(build_memory_vector_item_id(memory_id, 1) in collection["items"] for memory_id in source_ids)
        for memory_id in source_ids:
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None
            assert record.is_active is True
            assert record.pending_mutation_job_id is None
            assert record.version == 1
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=source_ids)
            assert [record.id for record in recallable] == list(source_ids)
            assert await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP) == 0
    finally:
        release_external_call.set()
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_update_publishes_version_and_replaces_vector(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-update-worker"
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1,),
        primary_memory_id=1,
        action="update",
        pinned_ids=frozenset({1}),
        target_content="organized update content",
        target_memory_key="organized-update-key",
    )
    before = await _get_record(memory_session_factory, uid=uid, memory_id=1)
    assert before is not None
    old_vector_id = build_memory_vector_item_id(1, 1)
    consumer = _consumer(memory_session_factory)
    try:
        finished = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        record = await _get_record(memory_session_factory, uid=uid, memory_id=1)
        assert record is not None
        assert record.version == 2
        assert record.indexed_version == 2
        assert record.memory_key == "organized-update-key"
        assert record.memory_type == LongTermMemoryType.FACT
        assert record.content == "organized update content"
        assert record.content_token_count == estimate_tokens("organized update content")
        assert record.content_hash == build_memory_content_hash("organized update content")
        assert record.source == LongTermMemorySource.AUTO_ORGANIZE
        assert record.source_job_id == child_id
        assert record.change_evidence is None
        assert record.pinned is before.pinned
        assert record.last_recalled_at == before.last_recalled_at
        assert record.pending_mutation_job_id is None
        assert record.index_status == LongTermMemoryRecordIndexStatus.READY

        revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=1)
        assert [revision.version for revision in revisions] == [2, 1]
        current_revision = revisions[0]
        assert current_revision.memory_key == record.memory_key
        assert current_revision.memory_type == record.memory_type
        assert current_revision.content == record.content
        assert current_revision.content_token_count == record.content_token_count
        assert current_revision.content_hash == record.content_hash
        assert current_revision.source == LongTermMemorySource.AUTO_ORGANIZE
        assert current_revision.source_job_id == child_id
        assert current_revision.change_evidence is None

        collection = vector_backend.collections[COLLECTION_NAME]
        assert old_vector_id not in collection["items"]
        assert record.vector_item_id is not None
        assert record.vector_item_id.startswith(build_memory_vector_item_id(1, 2))
        assert record.vector_item_id in collection["items"]
        new_item = collection["items"][record.vector_item_id]
        assert new_item["document"] == record.content
        assert new_item["metadata"] == {
            "memory_id": 1,
            "uid": uid,
            "memory_key": "organized-update-key",
            "memory_type": LongTermMemoryType.FACT.value,
            "version": 2,
            "source": LongTermMemorySource.AUTO_ORGANIZE.value,
            "embedding_revision": ACTIVE_EMBEDDING_REVISION,
            "updated_at": new_item["metadata"]["updated_at"],
        }
        assert finished.result is not None
        assert finished.result["vector_item_id"] == record.vector_item_id
        assert "content" not in finished.result
        assert "organized update content" not in json.dumps(finished.result)
        async with memory_session_factory() as db:
            assert await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP) == 0
            assert await memory_record_crud.count_active(db, uid=uid) == 1
    finally:
        await consumer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_stage", ["embedding", "upsert"])
async def test_organization_merge_update_retries_external_vector_failure_and_publishes_once(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    failure_stage: str,
) -> None:
    uid = f"organization-merge-update-{failure_stage}-retry-worker"
    parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1,),
        primary_memory_id=1,
        action="update",
        pinned_ids=frozenset({1}),
        target_content="retryable organized update content",
        target_memory_key="retryable-organized-update-key",
        max_attempts=2,
    )
    before = await _get_record(memory_session_factory, uid=uid, memory_id=1)
    assert before is not None
    old_vector_id = build_memory_vector_item_id(1, 1)
    new_vector_prefix = _staged_vector_id_prefix(1, 2, child_id)
    failed_staged_vector_id: str | None = None
    failures_remaining = 1

    async def fail_embedding_once(*_args: Any, **_kwargs: Any) -> None:
        nonlocal failures_remaining
        if failures_remaining:
            failures_remaining -= 1
            raise RuntimeError("simulated vector failure")

    async def fail_upsert_once(_collection_name: str, item_ids: list[str]) -> None:
        nonlocal failed_staged_vector_id, failures_remaining
        if len(item_ids) != 1 or not item_ids[0].startswith(new_vector_prefix):
            return
        if failed_staged_vector_id is None:
            failed_staged_vector_id = item_ids[0]
        if failures_remaining:
            failures_remaining -= 1
            raise RuntimeError("simulated vector failure")

    if failure_stage == "embedding":
        vector_backend.embedding_hook = fail_embedding_once
    else:
        vector_backend.upsert_hook = fail_upsert_once

    consumer = _consumer(memory_session_factory)
    try:
        retried = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        assert retried.attempt_count == 1
        assert retried.active_mutation_key is not None
        parent = await _get_job(memory_session_factory, uid=uid, job_id=parent_id)
        assert parent is not None
        assert parent.status == LongTermMemoryMutationStatus.SUCCEEDED
        assert parent.active_mutation_key is None
        source = await _get_record(memory_session_factory, uid=uid, memory_id=source_ids[0])
        assert source is not None
        assert source.pending_mutation_job_id == child_id
        assert source.version == before.version == 1
        assert source.memory_key == before.memory_key
        assert source.content == before.content
        assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=1)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=source_ids)
        assert [record.id for record in recallable] == list(source_ids)
        collection = vector_backend.collections[COLLECTION_NAME]
        assert old_vector_id in collection["items"]
        if failure_stage == "upsert":
            assert failed_staged_vector_id is not None
            assert failed_staged_vector_id not in collection["items"]
            assert (COLLECTION_NAME, [failed_staged_vector_id]) in vector_backend.delete_calls
            assert vector_backend.upsert_calls[0]["ids"] == [failed_staged_vector_id]

        async with memory_session_factory() as db:
            now = await get_database_time(db)
            updated = await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == child_id).values(available_at=now))
            assert updated.rowcount == 1
            await db.commit()

        finished = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert finished.attempt_count == 2
        assert finished.active_mutation_key is None
        assert finished.result is not None
        assert finished.result["version"] == 2
        record = await _get_record(memory_session_factory, uid=uid, memory_id=1)
        assert record is not None
        assert record.version == 2
        assert record.pending_mutation_job_id is None
        revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=1)
        assert [revision.version for revision in revisions] == [2, 1]
        collection = vector_backend.collections[COLLECTION_NAME]
        assert old_vector_id not in collection["items"]
        assert record.vector_item_id is not None
        assert record.vector_item_id.startswith(new_vector_prefix)
        assert record.vector_item_id in collection["items"]
        assert finished.result["vector_item_id"] == record.vector_item_id
        assert vector_backend.upsert_calls[-1]["ids"] == [record.vector_item_id]
        if failed_staged_vector_id is not None:
            assert failed_staged_vector_id != record.vector_item_id
        assert (COLLECTION_NAME, [old_vector_id]) in vector_backend.delete_calls
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_update_recovers_expired_lease_and_does_not_publish_twice(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-update-lease-recovery-worker"
    _parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1,),
        primary_memory_id=1,
        action="update",
        pinned_ids=frozenset({1}),
        target_content="lease recovered organized update content",
        target_memory_key="lease-recovered-organized-update-key",
    )
    async with memory_session_factory() as db:
        claimed = await memory_job_crud.try_claim(
            db,
            uid=uid,
            job_id=child_id,
            owner="expired-organization-merge-owner",
            lease_seconds=1,
            commit=False,
        )
        assert claimed is not None
        await db.commit()
    async with memory_session_factory() as db:
        now = await get_database_time(db)
        updated = await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == child_id).values(lock_until=now - timedelta(seconds=10)))
        assert updated.rowcount == 1
        recovery = await memory_job_crud.recover_expired(db, delay_seconds=0)
    assert recovery.retried == 1
    recovered = await _get_job(memory_session_factory, uid=uid, job_id=child_id)
    assert recovered is not None
    assert recovered.status == LongTermMemoryMutationStatus.RETRY
    assert recovered.active_mutation_key is not None
    source = await _get_record(memory_session_factory, uid=uid, memory_id=source_ids[0])
    assert source is not None
    assert source.pending_mutation_job_id == child_id

    consumer = _consumer(memory_session_factory)
    try:
        finished = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert finished.attempt_count == 2
        assert finished.active_mutation_key is None
        record = await _get_record(memory_session_factory, uid=uid, memory_id=1)
        assert record is not None
        assert record.pending_mutation_job_id is None
        assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=1)] == [2, 1]
        assert len(vector_backend.upsert_calls) == 1
        assert len(vector_backend.delete_calls) == 1
        assert await consumer.run_once() == 0
        persisted = await _get_job(memory_session_factory, uid=uid, job_id=child_id)
        assert persisted is not None
        assert persisted.status == LongTermMemoryMutationStatus.SUCCEEDED
        assert persisted.attempt_count == 2
        assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=1)] == [2, 1]
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_recomputes_over_limit_to_normal_when_active_count_returns_within_limit(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-capacity-normal-worker"
    source_ids = tuple(range(1, 52))
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=source_ids,
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="capacity normal merge content",
        target_memory_key="capacity-normal-merge-key",
        initial_capacity_status=LongTermMemoryCapacityStatus.OVER_LIMIT,
    )
    consumer = _consumer(memory_session_factory)
    try:
        await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        async with memory_session_factory() as db:
            store = await memory_store_crud.get_by_uid(db, uid=uid)
            active_count = await memory_record_crud.count_active(db, uid=uid)
        assert store is not None
        assert active_count == 1
        assert store.capacity_status == LongTermMemoryCapacityStatus.NORMAL
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_update_keeps_over_limit_when_active_count_still_exceeds_limit(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-update-capacity-count-over-limit-worker"
    source_ids = tuple(range(1, 52))
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=source_ids,
        primary_memory_id=1,
        action="update",
        pinned_ids=frozenset({1}),
        target_content="capacity count over limit content",
        target_memory_key="capacity-count-over-limit-key",
        initial_capacity_status=LongTermMemoryCapacityStatus.OVER_LIMIT,
    )
    consumer = _consumer(memory_session_factory)
    try:
        await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        async with memory_session_factory() as db:
            store = await memory_store_crud.get_by_uid(db, uid=uid)
            active_count = await memory_record_crud.count_active(db, uid=uid)
        assert store is not None
        assert active_count == 51
        assert store.capacity_status == LongTermMemoryCapacityStatus.OVER_LIMIT
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_update_keeps_over_limit_when_untargeted_active_content_is_oversized(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-update-capacity-oversized-worker"
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2),
        primary_memory_id=1,
        action="update",
        pinned_ids=frozenset({1}),
        target_content="capacity oversized content",
        target_memory_key="capacity-oversized-key",
        initial_capacity_status=LongTermMemoryCapacityStatus.OVER_LIMIT,
    )
    async with memory_session_factory() as db:
        oversized = await memory_record_crud.update_if_version(
            db,
            uid=uid,
            memory_id=2,
            expected_version=1,
            indexed_version=2,
            content_token_count=MEMORY_CONTENT_MAX_TOKENS + 1,
            commit=False,
        )
        assert oversized is not None
        await db.commit()
    consumer = _consumer(memory_session_factory)
    try:
        await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        async with memory_session_factory() as db:
            store = await memory_store_crud.get_by_uid(db, uid=uid)
            oversized_count = await memory_record_crud.count_active_oversized(
                db,
                uid=uid,
                max_tokens=MEMORY_CONTENT_MAX_TOKENS,
            )
        assert store is not None
        assert oversized_count == 1
        assert store.capacity_status == LongTermMemoryCapacityStatus.OVER_LIMIT
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_tombstones_non_primary_sources_and_creates_uncancellable_cleanups(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-three-record-worker"
    parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="organized three record content",
        target_memory_key="organized-three-record-key",
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in source_ids}
    assert all(record is not None for record in before.values())
    source_snapshots = {memory_id: build_memory_record_snapshot(record) for memory_id, record in before.items() if record is not None}
    consumer = _consumer(memory_session_factory)
    try:
        finished = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        primary = await _get_record(memory_session_factory, uid=uid, memory_id=1)
        assert primary is not None
        assert primary.version == 2
        assert primary.memory_key == "organized-three-record-key"
        assert primary.content == "organized three record content"
        assert primary.content_token_count == estimate_tokens(primary.content)
        assert primary.content_hash == build_memory_content_hash(primary.content)
        assert primary.source == LongTermMemorySource.AUTO_ORGANIZE
        assert primary.source_job_id == child_id
        assert primary.change_evidence is None
        assert primary.pinned is True
        assert primary.pending_mutation_job_id is None
        primary_revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=1)
        assert [revision.version for revision in primary_revisions] == [2, 1]
        assert primary_revisions[0].memory_key == primary.memory_key
        assert primary_revisions[0].content == primary.content
        assert primary_revisions[0].content_token_count == primary.content_token_count
        assert primary_revisions[0].content_hash == primary.content_hash
        assert primary_revisions[0].source == LongTermMemorySource.AUTO_ORGANIZE
        assert primary_revisions[0].source_job_id == child_id
        assert primary_revisions[0].change_evidence is None

        cleanup_jobs: list[LongTermMemoryMutationJob]
        async with memory_session_factory() as db:
            cleanup_jobs = await memory_job_crud.list_children_by_parent_job_id(
                db,
                uid=uid,
                parent_job_id=child_id,
            )
        assert [job.operation for job in cleanup_jobs] == [
            LongTermMemoryMutationOperation.DELETE_CLEANUP,
            LongTermMemoryMutationOperation.DELETE_CLEANUP,
        ]
        assert [job.memory_id for job in cleanup_jobs] == [2, 3]
        assert all(job.parent_job_id == child_id for job in cleanup_jobs)
        assert all(job.uid == uid for job in cleanup_jobs)
        assert all(job.payload["source"] == LongTermMemorySource.AUTO_ORGANIZE.value for job in cleanup_jobs)
        assert all(job.status == LongTermMemoryMutationStatus.PENDING for job in cleanup_jobs)
        for cleanup_job in cleanup_jobs:
            source = before[cleanup_job.memory_id]
            assert source is not None
            assert cleanup_job.payload["organization_parent_job_id"] == parent_id
            assert cleanup_job.payload["organization_merge_job_id"] == child_id
            assert cleanup_job.payload["record_snapshot"] == source_snapshots[cleanup_job.memory_id]
            async with memory_session_factory() as db:
                cancellation = await MemoryJobManager().request_cancel(db, uid=uid, job_id=cleanup_job.id)
            assert cancellation.accepted is False
            assert cancellation.changed is False

        for memory_id in (2, 3):
            tombstone = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert tombstone is not None
            assert tombstone.is_active is False
            assert tombstone.deleted_at is not None
            assert tombstone.memory_key is None
            assert tombstone.content_hash is None
            assert tombstone.pending_mutation_job_id == next(job.id for job in cleanup_jobs if job.memory_id == memory_id)
            assert tombstone.content == before[memory_id].content
            assert tombstone.vector_item_id == build_memory_vector_item_id(memory_id, 1)

        collection = vector_backend.collections[COLLECTION_NAME]
        assert build_memory_vector_item_id(1, 1) not in collection["items"]
        assert primary.vector_item_id is not None
        assert primary.vector_item_id.startswith(build_memory_vector_item_id(1, 2))
        assert primary.vector_item_id in collection["items"]
        assert build_memory_vector_item_id(2, 1) in collection["items"]
        assert build_memory_vector_item_id(3, 1) in collection["items"]
        primary_item = collection["items"][primary.vector_item_id]
        assert primary_item["document"] == "organized three record content"
        assert primary_item["metadata"]["memory_id"] == 1
        assert primary_item["metadata"]["uid"] == uid
        assert primary_item["metadata"]["memory_key"] == "organized-three-record-key"
        assert primary_item["metadata"]["memory_type"] == LongTermMemoryType.FACT.value
        assert primary_item["metadata"]["version"] == 2
        assert primary_item["metadata"]["source"] == LongTermMemorySource.AUTO_ORGANIZE.value
        assert primary_item["metadata"]["embedding_revision"] == ACTIVE_EMBEDDING_REVISION
        assert finished.result is not None
        assert finished.result["vector_item_id"] == primary.vector_item_id
        assert finished.result["cleanup_job_ids"] == [job.id for job in cleanup_jobs]
        assert finished.result["tombstoned_memory_ids"] == [2, 3]
        assert "organized three record content" not in json.dumps(finished.result)
        assert finished.result["parent_job_id"] == parent_id

        parent = await _get_job(memory_session_factory, uid=uid, job_id=parent_id)
        merge = await _get_job(memory_session_factory, uid=uid, job_id=child_id)
        assert parent is not None
        assert parent.uid == uid
        assert parent.operation == LongTermMemoryMutationOperation.ORGANIZE
        assert parent.result == {"child_job_ids": [child_id]}
        assert merge is not None
        assert merge.uid == uid
        assert merge.operation == LongTermMemoryMutationOperation.ORGANIZE_MERGE
        assert merge.parent_job_id == parent_id

        async with memory_session_factory() as db:
            cancellation = await MemoryJobManager().request_cancel(db, uid=uid, job_id=child_id)
        assert cancellation.accepted is False
        assert cancellation.changed is False
        published_merge = await _get_job(memory_session_factory, uid=uid, job_id=child_id)
        assert published_merge is not None
        assert published_merge.status == LongTermMemoryMutationStatus.SUCCEEDED
        assert published_merge.result == finished.result

        for cleanup_job in cleanup_jobs:
            assert cleanup_job.id is not None
            cleanup_finished = await _run_child(
                consumer,
                memory_session_factory,
                uid=uid,
                child_id=cleanup_job.id,
                status=LongTermMemoryMutationStatus.SUCCEEDED,
            )
            assert cleanup_finished.result is not None
            assert cleanup_finished.result["memory_id"] == cleanup_job.memory_id
            assert cleanup_finished.result["record_snapshot"] == source_snapshots[cleanup_job.memory_id]
            assert cleanup_finished.result["operation"] == LongTermMemoryMutationOperation.DELETE_CLEANUP.value
            assert cleanup_finished.result["organization_parent_job_id"] == parent_id
            assert cleanup_finished.result["organization_merge_job_id"] == child_id

            assert await _get_record(memory_session_factory, uid=uid, memory_id=cleanup_job.memory_id) is None
            revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=cleanup_job.memory_id)
            assert [revision.version for revision in revisions] == [1]
            assert build_memory_record_snapshot(revisions[0]) == source_snapshots[cleanup_job.memory_id]
            source_vector_id = build_memory_vector_item_id(cleanup_job.memory_id, 1)
            assert source_vector_id not in vector_backend.collections[COLLECTION_NAME]["items"]
            assert (COLLECTION_NAME, [source_vector_id]) in vector_backend.delete_calls

            persisted_cleanup = await _get_job(memory_session_factory, uid=uid, job_id=cleanup_job.id)
            assert persisted_cleanup is not None
            assert persisted_cleanup.payload == cleanup_job.payload
            assert persisted_cleanup.result == cleanup_finished.result

        parent_after = await _get_job(memory_session_factory, uid=uid, job_id=parent_id)
        merge_after = await _get_job(memory_session_factory, uid=uid, job_id=child_id)
        assert parent_after is not None
        assert parent_after.operation == LongTermMemoryMutationOperation.ORGANIZE
        assert parent_after.result == {"child_job_ids": [child_id]}
        assert merge_after is not None
        assert merge_after.operation == LongTermMemoryMutationOperation.ORGANIZE_MERGE
        assert merge_after.result == finished.result

        assert await _get_job(memory_session_factory, uid=f"{uid}-other", job_id=child_id) is None
        async with memory_session_factory() as db:
            assert await memory_record_crud.list_recallable_by_ids(db, uid=f"{uid}-other", memory_ids=source_ids) == []
            next_memory_id = await memory_record_crud.get_next_memory_id(db)
        assert next_memory_id not in source_ids
        assert next_memory_id > max(source_ids)
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_cleanup_failure_retries_with_tombstone_and_preserves_history(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-cleanup-retry-worker"
    parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="cleanup retry organization content",
        target_memory_key="cleanup-retry-organization-key",
        max_attempts=1,
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in source_ids}
    source_snapshots = {memory_id: build_memory_record_snapshot(record) for memory_id, record in before.items() if record is not None}
    consumer = _consumer(memory_session_factory)
    try:
        await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        cleanup_jobs = await _get_cleanup_jobs(memory_session_factory, uid=uid, merge_job_id=child_id)
        assert len(cleanup_jobs) == 1
        failed_cleanup = cleanup_jobs[0]
        assert failed_cleanup.id is not None
        legacy_payload = dict(failed_cleanup.payload)
        legacy_payload.pop("organization_parent_job_id")
        legacy_payload.pop("organization_merge_job_id")
        async with memory_session_factory() as db:
            updated = await db.execute(
                update(LongTermMemoryMutationJob)
                .where(
                    LongTermMemoryMutationJob.uid == uid,
                    LongTermMemoryMutationJob.id == failed_cleanup.id,
                )
                .values(payload=legacy_payload)
            )
            assert updated.rowcount == 1
            await db.commit()
        legacy_cleanup = await _get_job(memory_session_factory, uid=uid, job_id=failed_cleanup.id)
        assert legacy_cleanup is not None
        assert "organization_parent_job_id" not in legacy_cleanup.payload
        assert "organization_merge_job_id" not in legacy_cleanup.payload
        assert legacy_cleanup.payload["record_snapshot"] == source_snapshots[failed_cleanup.memory_id]
        vector_backend.delete_failures_remaining = 1

        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=failed_cleanup.id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.result is None
        assert failed.error == t(ERR_MEMORY_JOB_DELETE_CLEANUP_FAILED)
        assert failed.error != t(ERR_MEMORY_JOB_PAYLOAD_INVALID)
        tombstone = await _get_record(memory_session_factory, uid=uid, memory_id=failed_cleanup.memory_id)
        assert tombstone is not None
        assert tombstone.is_active is False
        assert tombstone.deleted_at is not None
        async with memory_session_factory() as db:
            assert await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=(failed_cleanup.memory_id,)) == []
            failed_persisted = await memory_job_crud.get_by_id(db, uid=uid, job_id=failed_cleanup.id)
        assert failed_persisted is not None
        assert failed_persisted.uid == uid
        assert failed_persisted.parent_job_id == child_id
        assert "organization_parent_job_id" not in failed_persisted.payload
        assert "organization_merge_job_id" not in failed_persisted.payload
        assert failed_persisted.payload["record_snapshot"] == source_snapshots[failed_cleanup.memory_id]

        async with memory_session_factory() as db:
            retried = await retry_job(db, uid=uid, job_id=failed_cleanup.id)
        retry_job_view = retried["job"]
        assert retried["status"] == "accepted"
        assert retry_job_view["id"] != failed_cleanup.id
        retry_id = retry_job_view["id"]

        retried_cleanup = await _get_job(memory_session_factory, uid=uid, job_id=retry_id)
        assert retried_cleanup is not None
        assert retried_cleanup.uid == uid
        assert retried_cleanup.parent_job_id == child_id
        assert retried_cleanup.operation == LongTermMemoryMutationOperation.DELETE_CLEANUP
        assert retried_cleanup.status == LongTermMemoryMutationStatus.PENDING
        assert retried_cleanup.payload["organization_parent_job_id"] == parent_id
        assert retried_cleanup.payload["organization_merge_job_id"] == child_id
        assert retried_cleanup.payload["record_snapshot"] == source_snapshots[failed_cleanup.memory_id]
        reoccupied = await _get_record(memory_session_factory, uid=uid, memory_id=failed_cleanup.memory_id)
        assert reoccupied is not None
        assert reoccupied.is_active is False
        assert reoccupied.deleted_at is not None
        assert reoccupied.pending_mutation_job_id == retry_id
        async with memory_session_factory() as db:
            assert await memory_record_crud.list_recallable_by_ids(db, uid=f"{uid}-other", memory_ids=source_ids) == []

        vector_backend.delete_failures_remaining = 0
        succeeded = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=retry_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert succeeded.result is not None
        assert succeeded.result["record_snapshot"] == source_snapshots[failed_cleanup.memory_id]
        assert succeeded.result["organization_parent_job_id"] == parent_id
        assert succeeded.result["organization_merge_job_id"] == child_id
        assert await _get_record(memory_session_factory, uid=uid, memory_id=failed_cleanup.memory_id) is None
        revisions = await _get_revisions(memory_session_factory, uid=uid, memory_id=failed_cleanup.memory_id)
        assert [revision.version for revision in revisions] == [1]
        assert build_memory_record_snapshot(revisions[0]) == source_snapshots[failed_cleanup.memory_id]
        source_vector_id = build_memory_vector_item_id(failed_cleanup.memory_id, 1)
        assert source_vector_id not in vector_backend.collections[COLLECTION_NAME]["items"]
        assert await _get_job(memory_session_factory, uid=f"{uid}-other", job_id=retry_id) is None
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_active_embedding_change_after_vector_write_fails_and_cleans_orphan(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-config-change-worker"
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="config changed organization content",
        target_memory_key="config-changed-organization-key",
        max_attempts=1,
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in (1, 2, 3)}
    new_vector_prefix = _staged_vector_id_prefix(1, 2, child_id)
    staged_vector_id: str | None = None
    old_vector_items = {build_memory_vector_item_id(memory_id, 1): dict(vector_backend.collections[COLLECTION_NAME]["items"][build_memory_vector_item_id(memory_id, 1)]) for memory_id in (1, 2, 3)}
    changed = False

    async def change_active_store(_collection_name: str, item_ids: list[str]) -> None:
        nonlocal changed, staged_vector_id
        if changed or len(item_ids) != 1 or not item_ids[0].startswith(new_vector_prefix):
            return
        changed = True
        staged_vector_id = item_ids[0]
        async with memory_session_factory() as db:
            updated = await memory_store_crud.update_by_uid(
                db,
                uid=uid,
                active_embedding_signature="organization-embedding-signature-changed",
                active_embedding_revision=ACTIVE_EMBEDDING_REVISION + 1,
            )
            assert updated is not None

    vector_backend.upsert_hook = change_active_store
    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.active_mutation_key is None
        assert changed is True
        assert staged_vector_id is not None
        assert any(staged_vector_id in call["ids"] for call in vector_backend.upsert_calls)
        collection = vector_backend.collections[COLLECTION_NAME]
        assert staged_vector_id not in collection["items"]
        assert all(build_memory_vector_item_id(memory_id, 1) in collection["items"] for memory_id in (1, 2, 3))
        assert all(collection["items"][item_id] == item for item_id, item in old_vector_items.items())
        for memory_id in (1, 2, 3):
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.index_status == LongTermMemoryRecordIndexStatus.READY
            assert record.pending_mutation_job_id is None
            assert (record.content, record.memory_key, record.content_hash, record.vector_item_id) == (
                before[memory_id].content,
                before[memory_id].memory_key,
                before[memory_id].content_hash,
                before[memory_id].vector_item_id,
            )
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=(1, 2, 3))
            assert [record.id for record in recallable] == [1, 2, 3]
            assert await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP) == 0
            store = await memory_store_crud.get_by_uid(db, uid=uid)
        assert store is not None
        assert store.active_embedding_revision == ACTIVE_EMBEDDING_REVISION + 1
        assert store.active_embedding_signature == "organization-embedding-signature-changed"
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_active_migration_after_vector_write_fails_before_publish(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-active-migration-after-vector-write-worker"
    _parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="migration during publish content",
        target_memory_key="migration-during-publish-key",
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in source_ids}
    new_vector_prefix = _staged_vector_id_prefix(1, 2, child_id)
    staged_vector_id: str | None = None
    changed = False

    async def start_migration(_collection_name: str, item_ids: list[str]) -> None:
        nonlocal changed, staged_vector_id
        if changed or len(item_ids) != 1 or not item_ids[0].startswith(new_vector_prefix):
            return
        changed = True
        staged_vector_id = item_ids[0]
        async with memory_session_factory() as db:
            updated = await memory_store_crud.update_by_uid(
                db,
                uid=uid,
                migration_job_id=9104,
                migration_status=LongTermMemoryMigrationStatus.BUILDING,
                commit=False,
            )
            assert updated is not None
            await db.commit()

    vector_backend.upsert_hook = start_migration
    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.attempt_count == 1
        assert failed.error == t(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        assert failed.active_mutation_key is None
        assert changed is True
        assert staged_vector_id is not None
        assert any(staged_vector_id in call["ids"] for call in vector_backend.upsert_calls)
        assert (COLLECTION_NAME, [staged_vector_id]) in vector_backend.delete_calls
        collection = vector_backend.collections[COLLECTION_NAME]
        assert staged_vector_id not in collection["items"]
        assert all(build_memory_vector_item_id(memory_id, 1) in collection["items"] for memory_id in source_ids)
        for memory_id in source_ids:
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.pending_mutation_job_id is None
            assert record.index_status == LongTermMemoryRecordIndexStatus.READY
            assert (record.content, record.memory_key, record.content_hash, record.vector_item_id) == (
                before[memory_id].content,
                before[memory_id].memory_key,
                before[memory_id].content_hash,
                before[memory_id].vector_item_id,
            )
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=source_ids)
            assert await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP) == 0
        assert [record.id for record in recallable] == list(source_ids)
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
    finally:
        await consumer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reindex_phase", ["before_execution", "after_vector_write"])
async def test_organization_merge_reindex_boundary_fails_without_publication(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    reindex_phase: str,
) -> None:
    uid = f"organization-merge-reindex-{reindex_phase}-worker"
    _parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="reindex boundary organization content",
        target_memory_key="reindex-boundary-organization-key",
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in source_ids}
    old_vector_items = {build_memory_vector_item_id(memory_id, 1): dict(vector_backend.collections[COLLECTION_NAME]["items"][build_memory_vector_item_id(memory_id, 1)]) for memory_id in source_ids}
    new_vector_prefix = _staged_vector_id_prefix(1, 2, child_id)
    staged_vector_id: str | None = None
    switched = False

    if reindex_phase == "before_execution":
        async with memory_session_factory() as db:
            updated = await memory_store_crud.update_by_uid(
                db,
                uid=uid,
                index_status=LongTermMemoryIndexStatus.REINDEXING,
            )
            assert updated is not None
    else:

        async def switch_to_reindexing(_collection_name: str, item_ids: list[str]) -> None:
            nonlocal switched, staged_vector_id
            if switched or len(item_ids) != 1 or not item_ids[0].startswith(new_vector_prefix):
                return
            switched = True
            staged_vector_id = item_ids[0]
            async with memory_session_factory() as db:
                updated = await memory_store_crud.update_by_uid(
                    db,
                    uid=uid,
                    index_status=LongTermMemoryIndexStatus.REINDEXING,
                    commit=False,
                )
                assert updated is not None
                await db.commit()

        vector_backend.upsert_hook = switch_to_reindexing

    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.attempt_count == 1
        assert failed.error == t(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        assert failed.active_mutation_key is None
        if reindex_phase == "before_execution":
            assert vector_backend.upsert_calls == []
        else:
            assert switched is True
            assert staged_vector_id is not None
            assert any(call["ids"] == [staged_vector_id] for call in vector_backend.upsert_calls)
            assert (COLLECTION_NAME, [staged_vector_id]) in vector_backend.delete_calls

        collection = vector_backend.collections[COLLECTION_NAME]
        if staged_vector_id is not None:
            assert staged_vector_id not in collection["items"]
        for item_id, item in old_vector_items.items():
            assert collection["items"][item_id] == item
        for memory_id in source_ids:
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.deleted_at is None
            assert record.pending_mutation_job_id is None
            assert record.version == before[memory_id].version == 1
            assert record.memory_key == before[memory_id].memory_key
            assert record.content == before[memory_id].content
            assert record.content_hash == before[memory_id].content_hash
            assert record.vector_item_id == before[memory_id].vector_item_id
            assert record.index_status == LongTermMemoryRecordIndexStatus.READY
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=source_ids)
            cleanup_count = await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP)
        assert [record.id for record in recallable] == list(source_ids)
        assert cleanup_count == 0
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_active_migration_before_execution_fails_and_releases_sources(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-active-migration-before-execution-worker"
    migration_job_id = 9101
    _parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="migration delta organization content",
        target_memory_key="migration-delta-organization-key",
    )
    async with memory_session_factory() as db:
        updated = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            migration_job_id=migration_job_id,
            migration_status=LongTermMemoryMigrationStatus.BUILDING,
            commit=False,
        )
        assert updated is not None
        await db.commit()

    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.attempt_count == 1
        assert failed.error == t(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        assert failed.active_mutation_key is None
        assert vector_backend.upsert_calls == []
        assert not any(item_id.startswith(build_memory_vector_item_id(1, 2)) for item_id in vector_backend.collections[COLLECTION_NAME]["items"])
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
        assert await _get_cleanup_jobs(memory_session_factory, uid=uid, merge_job_id=child_id) == []
        for memory_id in source_ids:
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None
            assert record.pending_mutation_job_id is None
            assert record.is_active is True
            assert record.index_status == LongTermMemoryRecordIndexStatus.READY
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=source_ids)
        assert [record.id for record in recallable] == list(source_ids)
        async with memory_session_factory() as db:
            store = await memory_store_crud.get_by_uid(db, uid=uid)
        assert store is not None
        assert store.migration_job_id == migration_job_id
        assert store.migration_status == LongTermMemoryMigrationStatus.BUILDING
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_second_delta_conflict_rolls_back_publication_and_releases_sources(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-merge-delta-rollback-worker"
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="delta conflict organization content",
        target_memory_key="delta-conflict-organization-key",
        max_attempts=1,
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in (1, 2, 3)}
    original_append = memory_handlers.append_memory_embedding_delta
    append_calls = 0

    async def raise_on_second_delta(*args: Any, **kwargs: Any) -> Any:
        nonlocal append_calls
        append_calls += 1
        if append_calls == 2:
            raise MemoryConflictError("second migration delta conflict")
        return await original_append(*args, **kwargs)

    monkeypatch.setattr(memory_handlers, "append_memory_embedding_delta", raise_on_second_delta)
    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert append_calls == 2
        assert failed.active_mutation_key is None
        assert not any(item_id.startswith(build_memory_vector_item_id(1, 2)) for item_id in vector_backend.collections[COLLECTION_NAME]["items"])
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
        assert await _get_cleanup_jobs(memory_session_factory, uid=uid, merge_job_id=child_id) == []
        for memory_id in (1, 2, 3):
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.deleted_at is None
            assert record.memory_key == before[memory_id].memory_key
            assert record.content_hash == before[memory_id].content_hash
            assert record.pending_mutation_job_id is None
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            store = await memory_store_crud.get_by_uid(db, uid=uid)
        assert store is not None
        assert store.migration_delta_high_watermark == 0
    finally:
        await consumer.stop()


async def _get_cleanup_jobs(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    merge_job_id: int,
) -> list[LongTermMemoryMutationJob]:
    async with session_factory() as db:
        return [
            job
            for job in await memory_job_crud.list_children_by_parent_job_id(
                db,
                uid=uid,
                parent_job_id=merge_job_id,
            )
            if job.operation == LongTermMemoryMutationOperation.DELETE_CLEANUP
        ]


@pytest.mark.asyncio
async def test_organization_merge_cleanup_creation_conflict_rolls_back_all_database_publication(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-merge-cleanup-conflict-worker"
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="cleanup conflict organization content",
        target_memory_key="cleanup-conflict-organization-key",
        max_attempts=1,
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in (1, 2, 3)}

    async def raise_cleanup_conflict(*_args: Any, **_kwargs: Any) -> Any:
        raise MemoryJobTargetBusyError("existing cleanup business conflict")

    monkeypatch.setattr(memory_handlers.memory_job_manager, "create_organization_cleanup_job", raise_cleanup_conflict)
    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.active_mutation_key is None
        assert not any(item_id.startswith(build_memory_vector_item_id(1, 2)) for item_id in vector_backend.collections[COLLECTION_NAME]["items"])
        assert await _get_cleanup_jobs(memory_session_factory, uid=uid, merge_job_id=child_id) == []
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
        for memory_id in (1, 2, 3):
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.deleted_at is None
            assert record.memory_key == before[memory_id].memory_key
            assert record.content_hash == before[memory_id].content_hash
            assert record.pending_mutation_job_id is None
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=(1, 2, 3))
            assert [record.id for record in recallable] == [1, 2, 3]
            assert await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP) == 0
    finally:
        await consumer.stop()
