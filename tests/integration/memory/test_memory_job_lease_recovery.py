import asyncio
from datetime import timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import (
    ERR_MEMORY_JOB_LEASE_MAX_ATTEMPTS_EXCEEDED,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud
from app.core.i18n import t
from app.core.memory_jobs.consumer import retry_delay_seconds
from app.core.memory_jobs.manager import MemoryJobManager
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
)
from app.providers.database.time import get_database_time
from tests.integration.memory import test_memory_job_worker as job_worker_tests

memory_job_database = job_worker_tests.memory_job_database
_create_direct_job = job_worker_tests._create_direct_job
_create_memory_record = job_worker_tests._create_memory_record
_get_job = job_worker_tests._get_job
_claim = job_worker_tests._claim


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
