from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.knowledge_jobs.consumer import KnowledgeJobConsumer
from app.core.knowledge_jobs.executor import (
    KnowledgeJobCancelledError,
    KnowledgeJobExecutor,
    KnowledgeJobLeaseLostError,
)
from app.core.knowledge_jobs.handlers import create_default_knowledge_job_executor
from app.core.knowledge_jobs.vector_cleanup import create_managed_vector_cleanup_job
from app.models.knowledge_base import (
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeItem,
)
from app.providers.database.time import get_database_time
from tests.integration.knowledge import test_knowledge_job as knowledge_job_module

knowledge_job_database = knowledge_job_module.knowledge_job_database
_create_container = knowledge_job_module._create_container
_submit_create = knowledge_job_module._submit_create
_claim = knowledge_job_module._claim


@pytest.mark.asyncio
async def test_expired_lease_recovers_and_old_worker_is_fenced(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    job_id = submission.job.id

    first_claim = await _claim(
        knowledge_job_database,
        job_id=job_id,
        owner="old-worker",
        lease_seconds=1,
    )
    assert first_claim is not None

    async with knowledge_job_database() as db:
        now = await get_database_time(db)
        await db.execute(update(KnowledgeJob).where(KnowledgeJob.id == job_id).values(lock_until=now - timedelta(seconds=10)))
        await db.commit()
        recovery = await knowledge_job_crud.recover_expired(db, delay_seconds=0)
    assert recovery.retried == 1

    second_claim = await _claim(knowledge_job_database, job_id=job_id, owner="new-worker")
    assert second_claim is not None
    assert second_claim.attempt_count == 2

    async with knowledge_job_database() as db:
        assert (
            await knowledge_job_crud.get_active_claim(
                db,
                uid="user-1",
                job_id=job_id,
                owner="old-worker",
            )
            is None
        )
        assert not await knowledge_job_crud.mark_succeeded(
            db,
            uid="user-1",
            job_id=job_id,
            owner="old-worker",
            result={"stale": True},
        )


@pytest.mark.asyncio
async def test_consumer_startup_recovers_expired_cleanup_even_after_max_attempts(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    parent_claim = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="publish-worker",
    )
    assert parent_claim is not None

    async with knowledge_job_database() as db:
        assert await knowledge_job_crud.mark_failed(
            db,
            uid="user-1",
            job_id=submission.job.id,
            owner="publish-worker",
            error="parent failed",
        )
        parent = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert parent is not None
        cleanup = await create_managed_vector_cleanup_job(
            db,
            source_job=parent,
            reason="staged",
            collection_name=knowledge_base.collection_name,
            vector_item_ids=["orphan-after-restart"],
        )
        await db.commit()

    cleanup_claim = await _claim(
        knowledge_job_database,
        job_id=cleanup.id,
        owner="crashed-cleanup-worker",
        lease_seconds=1,
    )
    assert cleanup_claim is not None

    async with knowledge_job_database() as db:
        now = await get_database_time(db)
        await db.execute(
            update(KnowledgeJob)
            .where(KnowledgeJob.id == cleanup.id)
            .values(
                attempt_count=cleanup_claim.max_attempts,
                lock_until=now - timedelta(seconds=10),
            )
        )
        await db.commit()

    startup_recovered = asyncio.Event()

    class _StartupRecoveryConsumer(KnowledgeJobConsumer):
        async def _recover_expired(self):
            result = await super()._recover_expired()
            startup_recovered.set()
            return result

    consumer = _StartupRecoveryConsumer(
        KnowledgeJobExecutor({}),
        session_factory=knowledge_job_database,
        poll_interval_seconds=60,
        recovery_interval_seconds=60,
    )
    consumer.start()
    await asyncio.wait_for(startup_recovered.wait(), timeout=1)
    await consumer.stop()

    async with knowledge_job_database() as db:
        current = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=cleanup.id,
        )

    assert current is not None
    assert current.status == KnowledgeJobStatus.RETRY
    assert current.locked_by is None
    assert current.lock_until is None
    assert current.attempt_count == current.max_attempts


@pytest.mark.asyncio
async def test_retryable_cleanup_continues_after_normal_max_attempts(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    parent_claim = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="publish-worker",
    )
    assert parent_claim is not None

    async with knowledge_job_database() as db:
        assert await knowledge_job_crud.mark_failed(
            db,
            uid="user-1",
            job_id=submission.job.id,
            owner="publish-worker",
            error="parent failed",
        )
        parent = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert parent is not None
        cleanup = await create_managed_vector_cleanup_job(
            db,
            source_job=parent,
            reason="staged",
            collection_name=knowledge_base.collection_name,
            vector_item_ids=["retry-forever-vector"],
        )
        await db.execute(update(KnowledgeJob).where(KnowledgeJob.id == cleanup.id).values(attempt_count=cleanup.max_attempts - 1))
        await db.commit()

    cleanup_claim = await _claim(
        knowledge_job_database,
        job_id=cleanup.id,
        owner="cleanup-worker",
    )
    assert cleanup_claim is not None
    assert cleanup_claim.attempt_count == cleanup_claim.max_attempts

    async def fail_delete(*_args, **_kwargs):
        raise RuntimeError("vector unavailable")

    monkeypatch.setattr(
        "app.core.knowledge_jobs.vector_cleanup.async_delete_collection_items",
        fail_delete,
    )
    consumer = KnowledgeJobConsumer(
        create_default_knowledge_job_executor(session_factory=knowledge_job_database),
        session_factory=knowledge_job_database,
    )
    await consumer._execute(cleanup_claim, "cleanup-worker")

    async with knowledge_job_database() as db:
        current = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=cleanup.id,
        )
    assert current is not None
    assert current.status == KnowledgeJobStatus.RETRY
    assert current.attempt_count == current.max_attempts
    async with knowledge_job_database() as db:
        database_now = await get_database_time(db)
    retry_delay = (current.available_at - database_now).total_seconds()
    assert 25 <= retry_delay <= 30


@pytest.mark.asyncio
async def test_lease_renewal_database_error_immediately_cancels_running_job(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    consumer = KnowledgeJobConsumer(
        KnowledgeJobExecutor({}),
        session_factory=knowledge_job_database,
    )
    running_task = asyncio.create_task(asyncio.Event().wait())
    consumer._running[123] = SimpleNamespace(
        uid="user-1",
        worker_id="lease-worker",
        task=running_task,
        waiting_for_children=False,
    )

    async def fail_renew(*_args, **_kwargs):
        raise RuntimeError("database unavailable")

    monkeypatch.setattr(knowledge_job_crud, "renew_lease", fail_renew)
    await consumer._renew_running()
    await asyncio.sleep(0)

    assert running_task.cancelled()


@pytest.mark.asyncio
async def test_lost_lease_checkpoint_rejects_old_worker(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="old-worker",
        lease_seconds=1,
    )
    assert claimed is not None

    async with knowledge_job_database() as db:
        now = await get_database_time(db)
        await db.execute(update(KnowledgeJob).where(KnowledgeJob.id == submission.job.id).values(lock_until=now - timedelta(seconds=10)))
        await db.commit()

    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    with pytest.raises(KnowledgeJobLeaseLostError):
        await executor.execute_claimed(claimed, "old-worker")


@pytest.mark.asyncio
async def test_running_cancellation_is_observed_before_lease_expiry(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="cancel-worker",
    )
    assert claimed is not None

    async with knowledge_job_database() as db:
        cancellation = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert cancellation.accepted is True
        assert cancellation.changed is True

    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    with pytest.raises(KnowledgeJobCancelledError):
        await executor.execute_claimed(claimed, "cancel-worker")


@pytest.mark.asyncio
async def test_cancel_request_blocks_terminal_success_race(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="cancel-race-worker",
    )
    assert claimed is not None

    async with knowledge_job_database() as db:
        cancellation = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert cancellation.accepted is True
        assert cancellation.changed is True
        assert not await knowledge_job_crud.mark_succeeded(
            db,
            uid="user-1",
            job_id=submission.job.id,
            owner="cancel-race-worker",
        )
        current = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )

    assert current is not None
    assert current.status == KnowledgeJobStatus.RUNNING
    assert current.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_running_cancel_request_is_idempotent(
    knowledge_job_database: async_sessionmaker[AsyncSession],
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="cancel-idempotent-worker",
    )
    assert claimed is not None

    async with knowledge_job_database() as db:
        first = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        second = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )

    assert first.accepted is True and first.changed is True
    assert second.accepted is True and second.changed is False
    assert second.job is not None and second.job.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_cancel_request_retries_after_stale_pending_read(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="cancel-transition-worker",
    )
    assert claimed is not None
    stale = claimed.model_copy(update={"status": KnowledgeJobStatus.PENDING})
    original_get_by_id = knowledge_job_crud.get_by_id
    calls = 0

    async def stale_once(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return stale
        return await original_get_by_id(*args, **kwargs)

    monkeypatch.setattr(knowledge_job_crud, "get_by_id", stale_once)
    async with knowledge_job_database() as db:
        cancellation = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        current = await original_get_by_id(db, uid="user-1", job_id=submission.job.id)

    assert cancellation.accepted is True
    assert cancellation.changed is True
    assert current is not None
    assert current.status == KnowledgeJobStatus.RUNNING
    assert current.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_shutdown_release_does_not_create_retry_with_cancel_requested(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="shutdown-race-worker",
    )
    assert claimed is not None
    stale = claimed.model_copy(update={"cancel_requested_at": None})

    async with knowledge_job_database() as db:
        cancellation = await knowledge_job_crud.request_cancel(
            db,
            uid="user-1",
            job_id=submission.job.id,
        )
        assert cancellation.accepted is True
        assert cancellation.changed is True

    original_get_by_id = knowledge_job_crud.get_by_id

    async def stale_claim(*_args, **_kwargs):
        return stale

    monkeypatch.setattr(knowledge_job_crud, "get_by_id", stale_claim)
    async with knowledge_job_database() as db:
        changed = await knowledge_job_crud.release_claim_for_shutdown(
            db,
            uid="user-1",
            job_id=submission.job.id,
            owner="shutdown-race-worker",
            delay_seconds=1,
            max_attempts_error="max attempts",
        )

    monkeypatch.setattr(knowledge_job_crud, "get_by_id", original_get_by_id)
    async with knowledge_job_database() as db:
        current = await original_get_by_id(db, uid="user-1", job_id=submission.job.id)

    assert changed is False
    assert current is not None
    assert current.status == KnowledgeJobStatus.RUNNING
    assert current.cancel_requested_at is not None


@pytest.mark.asyncio
async def test_stale_worker_cleanup_cannot_delete_new_worker_published_vectors(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    job_id = submission.job.id
    old_claim = await _claim(knowledge_job_database, job_id=job_id, owner="old-worker")
    assert old_claim is not None

    old_upsert_started = asyncio.Event()
    release_old_upsert = asyncio.Event()
    upsert_call_count = 0
    upserted_ids: list[list[str]] = []
    deleted_ids: list[list[str]] = []

    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.load_embedding_runtime_config",
        AsyncMock(
            return_value=EmbeddingRuntimeConfig(
                channel_id=1,
                channel_name="test",
                model_id="embedding-model",
                declared_dimensions=2,
                protocol="openai_embedding",
                timeout=30.0,
                base_url="https://embedding.invalid/v1",
                api_key="secret",
            )
        ),
    )
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", AsyncMock())

    async def fake_embed(_config, texts, **_kwargs):
        return [[0.1, 0.2] for _ in texts]

    async def fake_upsert(_collection_name, item_ids, *_args, **_kwargs):
        nonlocal upsert_call_count
        upsert_call_count += 1
        upserted_ids.append(list(item_ids))
        if upsert_call_count == 1:
            old_upsert_started.set()
            await release_old_upsert.wait()
        return len(item_ids)

    async def fake_delete(_collection_name, item_ids, **_kwargs):
        deleted_ids.append(list(item_ids))
        return len(item_ids)

    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)
    monkeypatch.setattr("app.core.knowledge_jobs.vector_cleanup.async_delete_collection_items", fake_delete)

    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    old_task = asyncio.create_task(executor.execute_claimed(old_claim, "old-worker"))
    await old_upsert_started.wait()

    async with knowledge_job_database() as db:
        now = await get_database_time(db)
        await db.execute(update(KnowledgeJob).where(KnowledgeJob.id == job_id).values(lock_until=now - timedelta(seconds=10)))
        await db.commit()
        recovery = await knowledge_job_crud.recover_expired(db, delay_seconds=0)
    assert recovery.retried == 1

    new_claim = await _claim(knowledge_job_database, job_id=job_id, owner="new-worker")
    assert new_claim is not None
    new_result = await executor.execute_claimed(new_claim, "new-worker")
    assert new_result.finalized is True

    async with knowledge_job_database() as db:
        published_item = await db.get(ManagedKnowledgeItem, submission.item.id)
    assert published_item is not None
    published_ids = set(published_item.vector_item_ids)
    assert published_ids

    release_old_upsert.set()
    with pytest.raises(KnowledgeJobLeaseLostError):
        await old_task

    assert len(upserted_ids) == 2
    old_vector_ids = set(upserted_ids[0])
    async with knowledge_job_database() as db:
        cleanup_result = await db.execute(
            select(KnowledgeJob)
            .where(
                KnowledgeJob.parent_job_id == job_id,
                KnowledgeJob.operation == KnowledgeJobOperation.MANAGED_VECTOR_CLEANUP,
            )
            .order_by(KnowledgeJob.id)
        )
        cleanup_jobs = list(cleanup_result.scalars().all())
        old_cleanup = next(job for job in cleanup_jobs if old_vector_ids == set(job.payload.get("vector_item_ids", [])))
        now = await get_database_time(db)
        await db.execute(update(KnowledgeJob).where(KnowledgeJob.id == old_cleanup.id).values(available_at=now))
        await db.commit()

    cleanup_claim = await _claim(
        knowledge_job_database,
        job_id=old_cleanup.id,
        owner="cleanup-worker",
    )
    assert cleanup_claim is not None
    cleanup_result = await executor.execute_claimed(cleanup_claim, "cleanup-worker")
    assert cleanup_result.finalized is False
    assert deleted_ids
    assert published_ids.isdisjoint({item_id for batch in deleted_ids for item_id in batch})


@pytest.mark.asyncio
async def test_unknown_commit_result_does_not_delete_published_vectors(
    knowledge_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await _create_container(knowledge_job_database)
    submission = await _submit_create(knowledge_job_database, knowledge_base.id)
    claimed = await _claim(
        knowledge_job_database,
        job_id=submission.job.id,
        owner="commit-race-worker",
    )
    assert claimed is not None

    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.load_embedding_runtime_config",
        AsyncMock(
            return_value=EmbeddingRuntimeConfig(
                channel_id=1,
                channel_name="test",
                model_id="embedding-model",
                declared_dimensions=2,
                protocol="openai_embedding",
                timeout=30.0,
                base_url="https://embedding.invalid/v1",
                api_key="secret",
            )
        ),
    )
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", AsyncMock())
    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.embed_texts_with_config",
        AsyncMock(return_value=[[0.1, 0.2]]),
    )
    monkeypatch.setattr(
        "app.core.knowledge_jobs.handlers.async_upsert_collection_items",
        AsyncMock(return_value=1),
    )
    delete_mock = AsyncMock(return_value=1)
    monkeypatch.setattr("app.core.knowledge_jobs.vector_cleanup.async_delete_collection_items", delete_mock)

    original_commit = AsyncSession.commit
    commit_count = 0

    async def commit_then_raise(session: AsyncSession) -> None:
        nonlocal commit_count
        commit_count += 1
        await original_commit(session)
        if commit_count == 2:
            raise RuntimeError("commit result unknown")

    monkeypatch.setattr(AsyncSession, "commit", commit_then_raise)
    executor = create_default_knowledge_job_executor(session_factory=knowledge_job_database)
    with pytest.raises(RuntimeError, match="commit result unknown"):
        await executor.execute_claimed(claimed, "commit-race-worker")

    delete_mock.assert_not_awaited()
    async with knowledge_job_database() as db:
        item = await db.get(ManagedKnowledgeItem, submission.item.id)
        job = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=submission.job.id)
    assert item is not None and item.is_recallable is True
    assert item.vector_item_ids
    assert job is not None and job.status == KnowledgeJobStatus.SUCCEEDED
