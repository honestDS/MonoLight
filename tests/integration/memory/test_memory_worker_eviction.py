import asyncio
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.integration.memory import test_memory_worker as worker_tests

_FakeVectorBackend = worker_tests._FakeVectorBackend
EmbeddingRuntimeConfig = worker_tests.EmbeddingRuntimeConfig
MemoryRecallStatus = worker_tests.MemoryRecallStatus
LongTermMemoryEmbeddingDeltaAction = worker_tests.LongTermMemoryEmbeddingDeltaAction
LongTermMemoryIndexStatus = worker_tests.LongTermMemoryIndexStatus
LongTermMemoryMigrationStatus = worker_tests.LongTermMemoryMigrationStatus
LongTermMemoryMutationOperation = worker_tests.LongTermMemoryMutationOperation
LongTermMemoryMutationStatus = worker_tests.LongTermMemoryMutationStatus
LongTermMemoryRecordIndexStatus = worker_tests.LongTermMemoryRecordIndexStatus
LongTermMemorySource = worker_tests.LongTermMemorySource
RetrievalHit = worker_tests.RetrievalHit
WAIT_TIMEOUT_SECONDS = worker_tests.WAIT_TIMEOUT_SECONDS
_configure_store = worker_tests._configure_store
_consumer = worker_tests._consumer
_create_service_memory = worker_tests._create_service_memory
_get_deltas = worker_tests._get_deltas
_get_job = worker_tests._get_job
_get_record = worker_tests._get_record
_get_revisions = worker_tests._get_revisions
_make_available_now = worker_tests._make_available_now
_run_job = worker_tests._run_job
_runtime_config = worker_tests._runtime_config
_seed_full_ready_records = worker_tests._seed_full_ready_records
_wait_for_job = worker_tests._wait_for_job
build_memory_vector_item_id = worker_tests.build_memory_vector_item_id
estimate_tokens = worker_tests.estimate_tokens
memory_handlers = worker_tests.memory_handlers
memory_job_crud = worker_tests.memory_job_crud
memory_publication_handler = worker_tests.memory_publication_handler
memory_recall_service_module = worker_tests.memory_recall_service_module
memory_record_crud = worker_tests.memory_record_crud
memory_service = worker_tests.memory_service
memory_session_factory = worker_tests.memory_session_factory
memory_store_crud = worker_tests.memory_store_crud
vector_backend = worker_tests.vector_backend


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
    ) -> list[RetrievalHit]:
        assert limit == 1
        return [
            RetrievalHit(
                id=build_memory_vector_item_id(1, 1),
                metadata={
                    "uid": uid,
                    "memory_id": 1,
                    "version": 1,
                    "embedding_revision": 1,
                },
                fusion_score=1.0,
                content="untrusted vector content",
            )
        ]

    monkeypatch.setattr(memory_recall_service_module, "load_embedding_runtime_config", recall_loader)
    monkeypatch.setattr(memory_recall_service_module, "embed_texts_with_config", recall_embed)
    monkeypatch.setattr(memory_recall_service_module, "_hybrid_query_collection", recall_query)
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

    monkeypatch.setattr(memory_publication_handler.memory_job_manager, "create_eviction_cleanup_job", raise_cleanup_job)
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
