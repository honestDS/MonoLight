import asyncio
from datetime import timedelta
from typing import Any

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud, memory_store_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.memory import build_memory_staged_vector_item_id, build_memory_vector_item_id
from app.core.memory_jobs.executor import (
    MemoryJobDeterministicError,
    MemoryJobExecutionContext,
    MemoryJobExecutionResult,
    MemoryJobExecutor,
    MemoryJobLeaseLostError,
)
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecordIndexStatus,
    LongTermMemorySource,
    LongTermMemoryType,
)
from app.providers.database.time import get_database_time
from tests.integration.memory import test_memory_worker as worker_tests

WAIT_TIMEOUT_SECONDS = worker_tests.WAIT_TIMEOUT_SECONDS
_FakeVectorBackend = worker_tests._FakeVectorBackend
_configure_store = worker_tests._configure_store
_consumer = worker_tests._consumer
_create_service_memory = worker_tests._create_service_memory
_get_job = worker_tests._get_job
_get_record = worker_tests._get_record
_get_revisions = worker_tests._get_revisions
_make_direct_job = worker_tests._make_direct_job
_make_available_now = worker_tests._make_available_now
_run_job = worker_tests._run_job
_runtime_config = worker_tests._runtime_config
_seed_ready_record = worker_tests._seed_ready_record
_update_service_memory = worker_tests._update_service_memory
_wait_for_job = worker_tests._wait_for_job
memory_execution_handler = worker_tests.memory_execution_handler
memory_publication_handler = worker_tests.memory_publication_handler
memory_session_factory = worker_tests.memory_session_factory
memory_vector_cleanup = worker_tests.memory_vector_cleanup
vector_backend = worker_tests.vector_backend


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

    monkeypatch.setattr(memory_execution_handler, "async_upsert_collection_items", coordinated_upsert)
    old_context = MemoryJobExecutionContext(
        job=old_claim,
        worker_id="old-owner",
        session_factory=memory_session_factory,
    )
    old_task = asyncio.create_task(memory_execution_handler._handle_update(old_context))
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
        new_result = await memory_execution_handler._handle_update(new_context)
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
    original_publish = memory_publication_handler.memory_record_crud.publish_pending_version

    async def return_none(*_args: Any, **_kwargs: Any) -> None:
        return None

    try:
        monkeypatch.setattr(memory_publication_handler.memory_record_crud, "publish_pending_version", return_none)
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

        monkeypatch.setattr(memory_publication_handler.memory_record_crud, "publish_pending_version", original_publish)
        memory_id = await _seed_ready_record(memory_session_factory, vector_backend, uid=uid, memory_key="stable-key")

        async def raise_revision(*_args: Any, **_kwargs: Any) -> None:
            raise RuntimeError("revision storage failure")

        monkeypatch.setattr(memory_publication_handler.memory_revision_crud, "create", raise_revision)
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
