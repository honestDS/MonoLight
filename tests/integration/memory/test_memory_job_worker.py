import asyncio
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud
from app.core.memory_jobs.consumer import MemoryJobConsumer
from app.core.memory_jobs.executor import (
    MemoryJobDeterministicError,
    MemoryJobExecutor,
    MemoryJobRetryableError,
)
from app.core.memory_jobs.manager import (
    MemoryJobManager,
)
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
)
from app.providers.database.time import get_database_time

MEMORY_JOB_TABLES = [LongTermMemoryRecord.__table__, LongTermMemoryMutationJob.__table__]
POLL_INTERVAL_SECONDS = 0.01
WAIT_TIMEOUT_SECONDS = 3.0


@pytest_asyncio.fixture
async def memory_job_database(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(tables=MEMORY_JOB_TABLES) as session_factory:
        yield session_factory


async def _wait_for_status(
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
            raise AssertionError("memory job did not reach the expected status")
        await asyncio.sleep(min(POLL_INTERVAL_SECONDS, remaining))


async def _create_direct_job(
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


async def _create_memory_record(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    memory_key: str,
    version: int = 1,
) -> int:
    async with session_factory() as db:
        record = await memory_record_crud.create(
            db,
            uid=uid,
            memory_key=memory_key,
            content=memory_key,
            content_hash=f"hash-{uid}-{memory_key}",
            version=version,
            is_active=True,
        )
    assert record.id is not None
    return record.id


async def _get_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
) -> LongTermMemoryMutationJob:
    async with session_factory() as db:
        job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)
    assert job is not None
    return job


async def _claim(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    job_id: int,
    owner: str,
    enabled_operations: list[LongTermMemoryMutationOperation] | None = None,
    lease_seconds: int = 30,
) -> LongTermMemoryMutationJob | None:
    async with session_factory() as db:
        return await memory_job_crud.try_claim(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            lease_seconds=lease_seconds,
            enabled_operations=enabled_operations,
        )


def _consumer(
    executor: MemoryJobExecutor,
    session_factory: async_sessionmaker[AsyncSession],
    *,
    max_concurrency: int = 1,
    shutdown_retry_delay_seconds: float = 1,
) -> MemoryJobConsumer:
    return MemoryJobConsumer(
        executor,
        session_factory,
        poll_interval_seconds=POLL_INTERVAL_SECONDS,
        lease_seconds=30,
        renew_interval_seconds=10,
        recovery_interval_seconds=1_000_000,
        max_concurrency=max_concurrency,
        recovery_retry_delay_seconds=1,
        shutdown_retry_delay_seconds=shutdown_retry_delay_seconds,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [operation for operation in LongTermMemoryMutationOperation if operation != LongTermMemoryMutationOperation.RESTORE],
)
async def test_executor_routes_each_memory_operation_once_and_returns_execution_result(
    memory_job_database: async_sessionmaker[AsyncSession],
    operation: LongTermMemoryMutationOperation,
) -> None:
    uid = f"executor-{operation.value}"
    job_id = await _create_direct_job(
        memory_job_database,
        uid=uid,
        operation=operation,
        dedupe_key=f"direct-{operation.value}",
        payload={"kind": operation.value},
    )
    routed: list[LongTermMemoryMutationOperation] = []

    async def handler(context) -> dict[str, Any]:
        routed.append(LongTermMemoryMutationOperation(context.job.operation))
        return {"routed": True}

    executor = MemoryJobExecutor({operation: handler}, session_factory=memory_job_database)
    claimed = await _claim(
        memory_job_database,
        uid=uid,
        job_id=job_id,
        owner="executor-owner",
        enabled_operations=[operation],
    )
    assert claimed is not None
    assert claimed.id is not None
    result = await executor.execute_claimed(claimed, "executor-owner")
    assert result.result == {"routed": True}
    assert result.finalized is False
    assert routed == [operation]

    async with memory_job_database() as db:
        assert await memory_job_crud.mark_succeeded(
            db,
            uid=uid,
            job_id=job_id,
            owner="executor-owner",
            result=result.result,
        )


@pytest.mark.asyncio
async def test_empty_executor_does_not_consume_pending_embedding_migration(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "empty-executor-user"
    job_id = await _create_direct_job(
        memory_job_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        dedupe_key="pending-migration",
    )
    executor = MemoryJobExecutor(session_factory=memory_job_database)
    assert executor.enabled_operations == frozenset()
    consumer = _consumer(executor, memory_job_database)
    try:
        assert await consumer.run_once() == 0
        pending = await _get_job(memory_job_database, uid=uid, job_id=job_id)
        assert pending.status == LongTermMemoryMutationStatus.PENDING
        assert pending.attempt_count == 0
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_consumer_success_deterministic_failure_and_retryable_retry(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "consumer-outcomes"
    success_id = await _create_direct_job(
        memory_job_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        dedupe_key="success",
    )
    deterministic_id = await _create_direct_job(
        memory_job_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.UPDATE,
        dedupe_key="deterministic",
    )
    retry_id = await _create_direct_job(
        memory_job_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
        dedupe_key="retryable",
    )
    attempts: dict[str, int] = {}

    async def success_handler(_context) -> dict[str, Any]:
        return {"outcome": "success"}

    async def deterministic_handler(_context) -> dict[str, Any]:
        raise MemoryJobDeterministicError("deterministic outcome")

    async def retry_handler(context) -> dict[str, Any]:
        attempts[context.job.uid] = attempts.get(context.job.uid, 0) + 1
        if attempts[context.job.uid] == 1:
            raise MemoryJobRetryableError("retryable outcome")
        return {"outcome": "retry-success"}

    executor = MemoryJobExecutor(
        {
            LongTermMemoryMutationOperation.REINDEX: success_handler,
            LongTermMemoryMutationOperation.UPDATE: deterministic_handler,
            LongTermMemoryMutationOperation.ORGANIZE_MERGE: retry_handler,
        },
        session_factory=memory_job_database,
    )
    consumer = _consumer(executor, memory_job_database, max_concurrency=3)
    try:
        assert await consumer.run_once() == 3
        await _wait_for_status(
            memory_job_database,
            uid=uid,
            job_id=success_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        failed = await _wait_for_status(
            memory_job_database,
            uid=uid,
            job_id=deterministic_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.error == "deterministic outcome"
        retried = await _wait_for_status(
            memory_job_database,
            uid=uid,
            job_id=retry_id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        assert retried.attempt_count == 1

        async with memory_job_database() as db:
            now = await get_database_time(db)
            await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == retry_id).values(available_at=now))
            await db.commit()
        assert await consumer.run_once() == 1
        completed = await _wait_for_status(
            memory_job_database,
            uid=uid,
            job_id=retry_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert completed.attempt_count == 2
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_running_cancel_finishes_cancelled_and_clears_target(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "cancel-user"
    memory_id = await _create_memory_record(memory_job_database, uid=uid, memory_key="cancel-target")
    active_key = f"{uid}:memory:{memory_id}"
    async with memory_job_database() as db:
        submission = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.UPDATE,
            dedupe_key="cancel-update",
            active_mutation_key=active_key,
            memory_id=memory_id,
            expected_version=1,
            payload={"kind": "cancel"},
        )
    assert submission.job.id is not None
    job_id = submission.job.id
    started = asyncio.Event()
    release = asyncio.Event()

    async def handler(_context) -> dict[str, Any]:
        started.set()
        await release.wait()
        return {"released": True}

    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.UPDATE: handler},
        session_factory=memory_job_database,
    )
    consumer = _consumer(executor, memory_job_database)
    try:
        assert await consumer.run_once() == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        async with memory_job_database() as db:
            cancellation = await manager.request_cancel(db, uid=uid, job_id=job_id)
        assert cancellation.accepted
        assert cancellation.changed
        requested = await _get_job(memory_job_database, uid=uid, job_id=job_id)
        assert requested.status == LongTermMemoryMutationStatus.RUNNING
        assert requested.cancel_requested_at is not None
        assert requested.active_mutation_key == active_key
        async with memory_job_database() as db:
            record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
        assert record is not None
        assert record.pending_mutation_job_id == job_id

        release.set()
        cancelled = await _wait_for_status(
            memory_job_database,
            uid=uid,
            job_id=job_id,
            status=LongTermMemoryMutationStatus.CANCELLED,
        )
        assert cancelled.active_mutation_key is None
        async with memory_job_database() as db:
            record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
        assert record is not None
        assert record.pending_mutation_job_id is None
    finally:
        release.set()
        await consumer.stop()


@pytest.mark.asyncio
async def test_renew_database_exception_cancels_running_job(
    memory_job_database: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "renew-exception-user"
    job_id = await _create_direct_job(
        memory_job_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        dedupe_key="renew-exception",
    )
    started = asyncio.Event()
    stopped = asyncio.Event()
    release = asyncio.Event()

    async def handler(_context) -> dict[str, Any]:
        started.set()
        try:
            await release.wait()
        finally:
            stopped.set()
        return {"unexpected": True}

    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.REINDEX: handler},
        session_factory=memory_job_database,
    )
    consumer = _consumer(executor, memory_job_database)
    try:
        assert await consumer.run_once() == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        entry = consumer._running[job_id]

        async def raise_renew_error(*_args: Any, **_kwargs: Any) -> bool:
            raise RuntimeError("renew failed")

        monkeypatch.setattr(memory_job_crud, "renew_lease", raise_renew_error)
        await consumer._renew_running_jobs()
        await asyncio.wait_for(stopped.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        await asyncio.wait_for(asyncio.gather(entry.task, return_exceptions=True), timeout=WAIT_TIMEOUT_SECONDS)
        assert entry.task.cancelled()
        assert job_id not in consumer._running
    finally:
        release.set()
        monkeypatch.undo()
        await consumer.stop()


@pytest.mark.asyncio
async def test_two_consumers_compete_for_one_job_and_handler_runs_once(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "multi-consumer-user"
    job_id = await _create_direct_job(
        memory_job_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        dedupe_key="one-job",
    )
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(_context) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        started.set()
        await release.wait()
        return {"calls": calls}

    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.REINDEX: handler},
        session_factory=memory_job_database,
    )
    first_consumer = _consumer(executor, memory_job_database)
    second_consumer = _consumer(executor, memory_job_database)
    try:
        first_claims, second_claims = await asyncio.gather(
            first_consumer.run_once(),
            second_consumer.run_once(),
        )
        assert first_claims + second_claims == 1
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        assert calls == 1
        release.set()
        await _wait_for_status(
            memory_job_database,
            uid=uid,
            job_id=job_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
    finally:
        release.set()
        await first_consumer.stop()
        await second_consumer.stop()


@pytest.mark.asyncio
async def test_consumer_stop_releases_retry_for_another_consumer_and_is_repeatable(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "shutdown-user"
    memory_id = await _create_memory_record(memory_job_database, uid=uid, memory_key="shutdown-target")
    active_key = f"{uid}:memory:{memory_id}"
    async with memory_job_database() as db:
        submission = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.UPDATE,
            dedupe_key="shutdown-update",
            active_mutation_key=active_key,
            memory_id=memory_id,
            expected_version=1,
            max_attempts=3,
            payload={"kind": "shutdown"},
        )
    assert submission.job.id is not None
    job_id = submission.job.id
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    async def handler(_context) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            await release.wait()
        return {"attempt": calls}

    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.UPDATE: handler},
        session_factory=memory_job_database,
    )
    first_consumer = _consumer(executor, memory_job_database, shutdown_retry_delay_seconds=1)
    second_consumer = _consumer(executor, memory_job_database)
    try:
        first_consumer.start()
        await asyncio.wait_for(started.wait(), timeout=WAIT_TIMEOUT_SECONDS)
        await first_consumer.stop()
        retried = await _wait_for_status(
            memory_job_database,
            uid=uid,
            job_id=job_id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        assert retried.locked_by is None
        assert retried.lock_until is None
        assert retried.active_mutation_key == active_key
        async with memory_job_database() as db:
            record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
        assert record is not None
        assert record.pending_mutation_job_id == job_id

        first_consumer.start()
        await first_consumer.stop()
        stopped = await _get_job(memory_job_database, uid=uid, job_id=job_id)
        assert stopped.status == LongTermMemoryMutationStatus.RETRY
        assert stopped.locked_by is None

        async with memory_job_database() as db:
            now = await get_database_time(db)
            await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).values(available_at=now))
            await db.commit()
        assert await second_consumer.run_once() == 1
        await _wait_for_status(
            memory_job_database,
            uid=uid,
            job_id=job_id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert calls == 2
    finally:
        release.set()
        await first_consumer.stop()
        await second_consumer.stop()
