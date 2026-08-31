import asyncio
from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.constants import (
    ERR_MEMORY_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED,
    ERR_MEMORY_JOB_OPERATION_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud
from app.core.i18n import t
from app.core.memory_jobs.consumer import MemoryJobConsumer, retry_delay_seconds
from app.core.memory_jobs.executor import (
    MemoryJobDeterministicError,
    MemoryJobExecutor,
    MemoryJobRetryableError,
)
from app.core.memory_jobs.manager import (
    MemoryJobManager,
    MemoryJobTargetBusyError,
    MemoryJobValidationError,
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
async def memory_job_database(tmp_path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "memory-job-worker-stage3.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=MEMORY_JOB_TABLES,
            )
        )

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


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
async def test_manager_dedupes_by_uid_validates_identity_and_isolates_reads(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    payload = {"kind": "embedding-migration"}

    async with memory_job_database() as db:
        first = await manager.submit(
            db,
            uid="user-a",
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            dedupe_key="migration-1",
            payload=payload,
        )
        second = await manager.submit(
            db,
            uid="user-a",
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            dedupe_key="migration-1",
            payload=payload,
        )
        other_user = await manager.submit(
            db,
            uid="user-b",
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            dedupe_key="migration-1",
            payload=payload,
        )
        parented = await manager.submit(
            db,
            uid="user-a",
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            dedupe_key="migration-parented",
            payload=payload,
            parent_job_id=42,
        )
        parented_duplicate = await manager.submit(
            db,
            uid="user-a",
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            dedupe_key="migration-parented",
            payload=payload,
            parent_job_id=42,
        )

    assert first.created
    assert not second.created
    assert first.job.id is not None
    assert second.job.id == first.job.id
    assert other_user.created
    assert other_user.job.id is not None
    assert other_user.job.id != first.job.id
    assert parented.created
    assert not parented_duplicate.created
    assert parented.job.parent_job_id == 42
    assert parented_duplicate.job.id == parented.job.id

    async with memory_job_database() as db:
        with pytest.raises(MemoryJobValidationError):
            await manager.submit(
                db,
                uid="user-a",
                operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
                dedupe_key="migration-parented",
                payload=payload,
                parent_job_id=43,
            )
        for invalid_parent_job_id in (True, 0):
            with pytest.raises(MemoryJobValidationError):
                await manager.submit(
                    db,
                    uid="user-a",
                    operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
                    dedupe_key=f"migration-invalid-parent-{invalid_parent_job_id}",
                    payload=payload,
                    parent_job_id=invalid_parent_job_id,
                )
        with pytest.raises(MemoryJobValidationError):
            await manager.submit(
                db,
                uid="user-a",
                operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
                dedupe_key="migration-1",
                payload={"kind": "different"},
            )
        with pytest.raises(MemoryJobValidationError):
            await manager.submit(
                db,
                uid="user-a",
                operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
                dedupe_key="migration-2",
                payload={"uid": "must-be-rejected"},
            )
        with pytest.raises(MemoryJobValidationError) as exc_info:
            await manager.submit(
                db,
                uid="user-a",
                operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
                dedupe_key="migration-3",
                payload={"pinned": True},
            )
        assert str(exc_info.value) == t(ERR_MEMORY_JOB_PAYLOAD_INVALID)

        assert await manager.get_job(db, uid="user-b", job_id=first.job.id) is None
        user_a_jobs = await manager.list_jobs(db, uid="user-a")
        user_b_jobs = await manager.list_jobs(db, uid="user-b")
        assert len(user_a_jobs) == 2
        assert [job.uid for job in user_a_jobs] == ["user-a", "user-a"]
        assert len(user_b_jobs) == 1
        assert [job.uid for job in user_b_jobs] == ["user-b"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    [
        LongTermMemoryMutationOperation.EXTRACT,
        LongTermMemoryMutationOperation.ORGANIZE_MERGE,
    ],
)
async def test_manager_rejects_non_currently_submittable_operations(
    memory_job_database: async_sessionmaker[AsyncSession],
    operation: LongTermMemoryMutationOperation,
) -> None:
    manager = MemoryJobManager()

    async with memory_job_database() as db:
        with pytest.raises(MemoryJobValidationError) as exc_info:
            await manager.submit(
                db,
                uid="operation-validation-user",
                operation=operation,
                dedupe_key=f"invalid-{operation.value}",
                payload={},
            )

    assert str(exc_info.value) == t(ERR_MEMORY_JOB_OPERATION_INVALID)


@pytest.mark.asyncio
async def test_manager_replacement_rules_require_active_key_allow_unassigned_id_and_do_not_reserve_target(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "replacement-manager-rules"
    existing_memory_id = await _create_memory_record(
        memory_job_database,
        uid=uid,
        memory_key="existing-candidate",
    )
    payload = {"candidate": {"memory_id": existing_memory_id}}

    async with memory_job_database() as db:
        with pytest.raises(MemoryJobValidationError):
            await manager.submit(
                db,
                uid=uid,
                operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
                dedupe_key="replacement-missing-active-key",
                payload=payload,
                memory_id=None,
            )
        with pytest.raises(MemoryJobValidationError):
            await manager.submit(
                db,
                uid=uid,
                operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
                dedupe_key="replacement-expected-version",
                payload=payload,
                active_mutation_key="replacement-expected-version-key",
                expected_version=1,
            )

        unassigned = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            dedupe_key="replacement-unassigned",
            payload=payload,
            active_mutation_key="replacement-unassigned-key",
            memory_id=None,
        )
        allocated = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            dedupe_key="replacement-allocated",
            payload=payload,
            active_mutation_key="replacement-allocated-key",
            memory_id=existing_memory_id,
        )

    assert unassigned.created
    assert unassigned.job.memory_id is None
    assert unassigned.job.expected_version is None
    assert allocated.created
    assert allocated.job.memory_id == existing_memory_id
    async with memory_job_database() as db:
        existing = await memory_record_crud.get_by_id(db, uid=uid, memory_id=existing_memory_id)
    assert existing is not None
    assert existing.pending_mutation_job_id is None


@pytest.mark.asyncio
async def test_target_mutation_reservation_is_busy_until_failure_and_then_reusable(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "target-user"
    memory_id = await _create_memory_record(memory_job_database, uid=uid, memory_key="target")

    async with memory_job_database() as db:
        first = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.UPDATE,
            dedupe_key="update-1",
            active_mutation_key=f"{uid}:memory:{memory_id}",
            memory_id=memory_id,
            expected_version=1,
            payload={"kind": "update"},
        )
    assert first.job.id is not None
    first_job_id = first.job.id

    async with memory_job_database() as db:
        record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
    assert record is not None
    assert record.pending_mutation_job_id == first_job_id

    async with memory_job_database() as db:
        with pytest.raises(MemoryJobTargetBusyError):
            await manager.submit(
                db,
                uid=uid,
                operation=LongTermMemoryMutationOperation.UPDATE,
                dedupe_key="update-2",
                active_mutation_key=f"{uid}:memory:{memory_id}",
                memory_id=memory_id,
                expected_version=1,
                payload={"kind": "other-update"},
            )

    claimed = await _claim(
        memory_job_database,
        uid=uid,
        job_id=first_job_id,
        owner="failure-owner",
        enabled_operations=[LongTermMemoryMutationOperation.UPDATE],
    )
    assert claimed is not None
    async with memory_job_database() as db:
        assert await memory_job_crud.mark_failed(
            db,
            uid=uid,
            job_id=first_job_id,
            owner="failure-owner",
            error="deterministic failure",
        )

    failed = await _get_job(memory_job_database, uid=uid, job_id=first_job_id)
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    assert failed.active_mutation_key is None
    async with memory_job_database() as db:
        record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
    assert record is not None
    assert record.pending_mutation_job_id is None

    async with memory_job_database() as db:
        reusable = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.UPDATE,
            dedupe_key="update-3",
            active_mutation_key=f"{uid}:memory:{memory_id}",
            memory_id=memory_id,
            expected_version=1,
            payload={"kind": "retry-update"},
        )
    assert reusable.created
    assert reusable.job.id is not None
    assert reusable.job.id != first_job_id


@pytest.mark.asyncio
async def test_request_cancel_finishes_pending_update_but_rejects_delete_cleanup(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "pending-cancel-user"
    memory_id = await _create_memory_record(memory_job_database, uid=uid, memory_key="cancel-target")
    active_key = f"{uid}:memory:{memory_id}"

    async with memory_job_database() as db:
        update_submission = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.UPDATE,
            dedupe_key="pending-update",
            active_mutation_key=active_key,
            memory_id=memory_id,
            expected_version=1,
            payload={"kind": "pending-update"},
        )
    assert update_submission.job.id is not None
    update_job_id = update_submission.job.id

    async with memory_job_database() as db:
        cancellation = await manager.request_cancel(db, uid=uid, job_id=update_job_id)
    assert cancellation.accepted
    assert cancellation.changed
    cancelled_update = await _get_job(memory_job_database, uid=uid, job_id=update_job_id)
    assert cancelled_update.status == LongTermMemoryMutationStatus.CANCELLED
    assert cancelled_update.active_mutation_key is None
    async with memory_job_database() as db:
        record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
    assert record is not None
    assert record.pending_mutation_job_id is None

    async with memory_job_database() as db:
        cleanup_submission = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
            dedupe_key="pending-delete-cleanup",
            active_mutation_key=active_key,
            memory_id=memory_id,
            payload={"kind": "delete-cleanup"},
        )
    assert cleanup_submission.job.id is not None
    cleanup_job_id = cleanup_submission.job.id

    async with memory_job_database() as db:
        cleanup_cancellation = await manager.request_cancel(db, uid=uid, job_id=cleanup_job_id)
    assert not cleanup_cancellation.accepted
    assert not cleanup_cancellation.changed
    pending_cleanup = await _get_job(memory_job_database, uid=uid, job_id=cleanup_job_id)
    assert pending_cleanup.status == LongTermMemoryMutationStatus.PENDING
    assert pending_cleanup.active_mutation_key == active_key
    async with memory_job_database() as db:
        record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
    assert record is not None
    assert record.pending_mutation_job_id == cleanup_job_id


@pytest.mark.asyncio
async def test_try_claim_is_atomic_and_old_owner_cannot_renew_or_finish(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "claim-user"
    job_id = await _create_direct_job(
        memory_job_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        dedupe_key="claim-1",
    )

    owner_a, owner_b = "owner-a", "owner-b"
    claimed_a, claimed_b = await asyncio.gather(
        _claim(memory_job_database, uid=uid, job_id=job_id, owner=owner_a),
        _claim(memory_job_database, uid=uid, job_id=job_id, owner=owner_b),
    )
    assert (claimed_a is not None) != (claimed_b is not None)
    winner = owner_a if claimed_a is not None else owner_b
    loser = owner_b if claimed_a is not None else owner_a
    claimed = claimed_a if claimed_a is not None else claimed_b
    assert claimed is not None
    assert claimed.id is not None
    assert claimed.locked_by == winner
    assert claimed.attempt_count == 1

    async with memory_job_database() as db:
        assert not await memory_job_crud.renew_lease(
            db,
            uid=uid,
            job_id=job_id,
            owner=loser,
        )
        assert not await memory_job_crud.mark_succeeded(
            db,
            uid=uid,
            job_id=job_id,
            owner=loser,
            result={"finished": True},
        )
        assert await memory_job_crud.mark_succeeded(
            db,
            uid=uid,
            job_id=job_id,
            owner=winner,
            result={"finished": True},
        )

    finished = await _get_job(memory_job_database, uid=uid, job_id=job_id)
    assert finished.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert finished.attempt_count == 1


@pytest.mark.asyncio
async def test_recover_expired_job_can_be_reclaimed_but_old_owner_cannot_finish(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    uid = "recovery-user"
    job_id = await _create_direct_job(
        memory_job_database,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        dedupe_key="recovery-retry",
    )
    first_claim = await _claim(
        memory_job_database,
        uid=uid,
        job_id=job_id,
        owner="expired-owner",
        lease_seconds=1,
    )
    assert first_claim is not None

    async with memory_job_database() as db:
        now = await get_database_time(db)
        await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).values(lock_until=now - timedelta(seconds=10)))
        await db.commit()
        recovery = await memory_job_crud.recover_expired(db, delay_seconds=0)
    assert recovery.retried == 1
    assert recovery.failed == 0

    second_claim = await _claim(
        memory_job_database,
        uid=uid,
        job_id=job_id,
        owner="new-owner",
    )
    assert second_claim is not None
    assert second_claim.attempt_count == 2
    async with memory_job_database() as db:
        assert not await memory_job_crud.mark_failed(
            db,
            uid=uid,
            job_id=job_id,
            owner="expired-owner",
            error="stale owner",
        )
        assert await memory_job_crud.mark_succeeded(
            db,
            uid=uid,
            job_id=job_id,
            owner="new-owner",
            result={"recovered": True},
        )


@pytest.mark.asyncio
async def test_recover_expired_max_attempts_fails_and_clears_target(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "recovery-target-user"
    memory_id = await _create_memory_record(memory_job_database, uid=uid, memory_key="recover-target")
    active_key = f"{uid}:memory:{memory_id}"
    async with memory_job_database() as db:
        submission = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.UPDATE,
            dedupe_key="recovery-fail",
            active_mutation_key=active_key,
            memory_id=memory_id,
            expected_version=1,
            max_attempts=1,
            payload={"kind": "recover"},
        )
    assert submission.job.id is not None
    job_id = submission.job.id

    claimed = await _claim(
        memory_job_database,
        uid=uid,
        job_id=job_id,
        owner="expired-owner",
        enabled_operations=[LongTermMemoryMutationOperation.UPDATE],
        lease_seconds=1,
    )
    assert claimed is not None
    async with memory_job_database() as db:
        now = await get_database_time(db)
        await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).values(lock_until=now - timedelta(seconds=10)))
        await db.commit()
        max_attempts_error = t(ERR_MEMORY_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED)
        recovery = await memory_job_crud.recover_expired(
            db,
            max_attempts_error=max_attempts_error,
        )
    assert recovery.failed == 1
    assert recovery.retried == 0

    failed = await _get_job(memory_job_database, uid=uid, job_id=job_id)
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    assert failed.error == max_attempts_error
    assert failed.active_mutation_key is None
    async with memory_job_database() as db:
        record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
    assert record is not None
    assert record.pending_mutation_job_id is None


@pytest.mark.asyncio
async def test_recover_expired_create_preserves_reservation_until_max_attempts_and_dedupe_is_idempotent(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "recovery-create-user"
    payload = {"kind": "recovery-create"}
    active_key = f"{uid}:create:recovery"

    async with memory_job_database() as db:
        submission = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.CREATE,
            dedupe_key="recovery-create",
            active_mutation_key=active_key,
            max_attempts=2,
            payload=payload,
        )
    assert submission.created
    assert submission.job.id is not None
    job_id = submission.job.id

    async with memory_job_database() as db:
        duplicate = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.CREATE,
            dedupe_key="recovery-create",
            active_mutation_key=active_key,
            max_attempts=2,
            payload=payload,
        )
        assert not duplicate.created
        assert duplicate.job.id == job_id
        assert await memory_job_crud.count_pending_create(db, uid=uid) == 1

    first_claim = await _claim(
        memory_job_database,
        uid=uid,
        job_id=job_id,
        owner="expired-create-owner",
        enabled_operations=[LongTermMemoryMutationOperation.CREATE],
        lease_seconds=1,
    )
    assert first_claim is not None
    async with memory_job_database() as db:
        now = await get_database_time(db)
        await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).values(lock_until=now - timedelta(seconds=10)))
        await db.commit()
        recovery = await memory_job_crud.recover_expired(db, delay_seconds=0)
    assert recovery.retried == 1
    assert recovery.failed == 0
    async with memory_job_database() as db:
        assert await memory_job_crud.count_pending_create(db, uid=uid) == 1

    second_claim = await _claim(
        memory_job_database,
        uid=uid,
        job_id=job_id,
        owner="expired-create-owner-2",
        enabled_operations=[LongTermMemoryMutationOperation.CREATE],
        lease_seconds=1,
    )
    assert second_claim is not None
    assert second_claim.attempt_count == 2
    async with memory_job_database() as db:
        now = await get_database_time(db)
        await db.execute(update(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == uid, LongTermMemoryMutationJob.id == job_id).values(lock_until=now - timedelta(seconds=10)))
        await db.commit()
        max_attempts_error = t(ERR_MEMORY_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED)
        recovery = await memory_job_crud.recover_expired(
            db,
            max_attempts_error=max_attempts_error,
        )
    assert recovery.failed == 1
    assert recovery.retried == 0
    failed = await _get_job(memory_job_database, uid=uid, job_id=job_id)
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    assert failed.active_mutation_key is None
    async with memory_job_database() as db:
        assert await memory_job_crud.count_pending_create(db, uid=uid) == 0


@pytest.mark.asyncio
async def test_mark_failed_replacement_clears_candidate_pending_from_payload_not_new_memory_id(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "replacement-mark-failed-user"
    candidate_id = await _create_memory_record(
        memory_job_database,
        uid=uid,
        memory_key="replacement-candidate",
    )
    replacement_memory_id = candidate_id + 10_000
    payload = {"candidate": {"memory_id": candidate_id}}

    async with memory_job_database() as db:
        submission = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            dedupe_key="replacement-mark-failed",
            payload=payload,
            active_mutation_key="replacement-mark-failed-key",
            memory_id=replacement_memory_id,
            max_attempts=1,
        )
        assert submission.job.id is not None
        assert await memory_record_crud.reserve_pending_mutation(
            db,
            uid=uid,
            memory_id=candidate_id,
            job_id=submission.job.id,
            expected_version=1,
        )

    claimed = await _claim(
        memory_job_database,
        uid=uid,
        job_id=submission.job.id,
        owner="replacement-failure-owner",
        enabled_operations=[LongTermMemoryMutationOperation.CREATE_WITH_EVICTION],
    )
    assert claimed is not None
    async with memory_job_database() as db:
        assert await memory_job_crud.mark_failed(
            db,
            uid=uid,
            job_id=submission.job.id,
            owner="replacement-failure-owner",
            error="replacement failure",
        )

    failed = await _get_job(memory_job_database, uid=uid, job_id=submission.job.id)
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    assert failed.active_mutation_key is None
    async with memory_job_database() as db:
        candidate = await memory_record_crud.get_by_id(db, uid=uid, memory_id=candidate_id)
        replacement = await memory_record_crud.get_by_id(db, uid=uid, memory_id=replacement_memory_id)
    assert candidate is not None
    assert candidate.pending_mutation_job_id is None
    assert replacement is None


@pytest.mark.asyncio
async def test_recover_expired_replacement_max_attempts_clears_candidate_pending_from_payload(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "replacement-recovery-user"
    candidate_id = await _create_memory_record(
        memory_job_database,
        uid=uid,
        memory_key="recovery-candidate",
    )
    replacement_memory_id = candidate_id + 20_000
    payload = {"candidate": {"memory_id": candidate_id}}

    async with memory_job_database() as db:
        submission = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.CREATE_WITH_EVICTION,
            dedupe_key="replacement-recovery",
            payload=payload,
            active_mutation_key="replacement-recovery-key",
            memory_id=replacement_memory_id,
            max_attempts=1,
        )
        assert submission.job.id is not None
        assert await memory_record_crud.reserve_pending_mutation(
            db,
            uid=uid,
            memory_id=candidate_id,
            job_id=submission.job.id,
            expected_version=1,
        )

    claimed = await _claim(
        memory_job_database,
        uid=uid,
        job_id=submission.job.id,
        owner="replacement-recovery-owner",
        enabled_operations=[LongTermMemoryMutationOperation.CREATE_WITH_EVICTION],
        lease_seconds=1,
    )
    assert claimed is not None
    async with memory_job_database() as db:
        now = await get_database_time(db)
        await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == submission.job.id,
            )
            .values(lock_until=now - timedelta(seconds=10))
        )
        await db.commit()
        recovery = await memory_job_crud.recover_expired(
            db,
            max_attempts_error="replacement max attempts",
        )

    assert recovery.failed == 1
    assert recovery.retried == 0
    failed = await _get_job(memory_job_database, uid=uid, job_id=submission.job.id)
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    assert failed.error == "replacement max attempts"
    async with memory_job_database() as db:
        candidate = await memory_record_crud.get_by_id(db, uid=uid, memory_id=candidate_id)
        replacement = await memory_record_crud.get_by_id(db, uid=uid, memory_id=replacement_memory_id)
    assert candidate is not None
    assert candidate.pending_mutation_job_id is None
    assert replacement is None


@pytest.mark.asyncio
async def test_shutdown_release_at_max_attempts_fails_and_rejects_old_owner(
    memory_job_database: async_sessionmaker[AsyncSession],
) -> None:
    manager = MemoryJobManager()
    uid = "shutdown-max-attempts-user"
    memory_id = await _create_memory_record(memory_job_database, uid=uid, memory_key="shutdown-target")
    active_key = f"{uid}:memory:{memory_id}"
    owner = "shutdown-owner"

    async with memory_job_database() as db:
        submission = await manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.UPDATE,
            dedupe_key="shutdown-max-attempts",
            active_mutation_key=active_key,
            memory_id=memory_id,
            expected_version=1,
            max_attempts=1,
            payload={"kind": "shutdown-max-attempts"},
        )
    assert submission.job.id is not None
    job_id = submission.job.id

    claimed = await _claim(
        memory_job_database,
        uid=uid,
        job_id=job_id,
        owner=owner,
        enabled_operations=[LongTermMemoryMutationOperation.UPDATE],
    )
    assert claimed is not None
    async with memory_job_database() as db:
        max_attempts_error = t(ERR_MEMORY_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED)
        assert await memory_job_crud.release_claim_for_shutdown(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            max_attempts_error=max_attempts_error,
        )

    failed = await _get_job(memory_job_database, uid=uid, job_id=job_id)
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    assert failed.error == max_attempts_error
    assert failed.active_mutation_key is None
    async with memory_job_database() as db:
        record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=memory_id)
    assert record is not None
    assert record.pending_mutation_job_id is None

    async with memory_job_database() as db:
        assert not await memory_job_crud.mark_succeeded(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            result={"late": True},
        )
    still_failed = await _get_job(memory_job_database, uid=uid, job_id=job_id)
    assert still_failed.status == LongTermMemoryMutationStatus.FAILED


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


@pytest.mark.parametrize(
    ("attempt_count", "expected"),
    [
        (0, 1),
        (1, 1),
        (2, 2),
        (3, 4),
        (9, 256),
        (10, 300),
        (20, 300),
    ],
)
def test_retry_delay_seconds_has_minimum_growth_and_cap(attempt_count: int, expected: int) -> None:
    assert retry_delay_seconds(attempt_count) == expected


def test_retry_delay_seconds_rejects_bool_and_negative_attempts() -> None:
    with pytest.raises(TypeError):
        retry_delay_seconds(True)
    with pytest.raises(TypeError):
        retry_delay_seconds(False)
    with pytest.raises(ValueError):
        retry_delay_seconds(-1)
