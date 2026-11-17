import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import (
    ERR_MEMORY_JOB_OPERATION_INVALID,
    ERR_MEMORY_JOB_PAYLOAD_INVALID,
)
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud
from app.core.i18n import t
from app.core.memory_jobs.manager import (
    MemoryJobManager,
    MemoryJobTargetBusyError,
    MemoryJobValidationError,
)
from app.models.memory import (
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
)
from tests.integration.memory import test_memory_job_worker as job_worker_tests

memory_job_database = job_worker_tests.memory_job_database
_claim = job_worker_tests._claim
_create_memory_record = job_worker_tests._create_memory_record
_get_job = job_worker_tests._get_job


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
