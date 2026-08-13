from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from tests.unit.memory_stage5_test_support import (
    MEMORY_TABLES,
    claim_job,
    configure_store,
    create_recallable_record,
)


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass


with patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient):
    from app.core.constants import (
        ERR_MEMORY_JOB_TARGET_STATE_CONFLICT,
        ERR_MEMORY_MUTATION_PENDING,
        ERR_MEMORY_RECORD_NOT_FOUND,
    )
    from app.core.crud.memory import (
        memory_record_crud,
        memory_revision_crud,
    )
    from app.core.crud.memory_job import memory_job_crud
    from app.core.memory import (
        MemoryConflictError,
        MemoryNotFoundError,
        build_memory_content_hash,
        get_memory,
        get_memory_settings,
        list_jobs,
        list_memories,
        list_memory_history,
        memory_service,
        pin_memory,
        retry_job,
        unpin_memory,
    )
    from app.core.memory_jobs.manager import MemoryJobValidationError, memory_job_manager
    from app.models.memory import (
        LongTermMemoryMutationJob,
        LongTermMemoryMutationOperation,
        LongTermMemoryMutationStatus,
        LongTermMemoryRecord,
        LongTermMemorySource,
        LongTermMemoryType,
    )


pytest_plugins = ("tests.unit.memory_stage5_fixture",)


@pytest_asyncio.fixture
async def memory_session_factory() -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
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


async def _create_raw_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    operation: LongTermMemoryMutationOperation,
    dedupe_key: str,
    status: LongTermMemoryMutationStatus = LongTermMemoryMutationStatus.PENDING,
    memory_id: int | None = None,
    expected_version: int | None = None,
    payload: dict[str, Any] | None = None,
) -> LongTermMemoryMutationJob:
    async with session_factory() as db:
        job, created = await memory_job_crud.create(
            db,
            uid=uid,
            operation=operation,
            dedupe_key=dedupe_key,
            status=status,
            memory_id=memory_id,
            expected_version=expected_version,
            payload=payload or {},
        )
        assert created
        return job


async def _fail_claimed_job(
    session_factory: async_sessionmaker[AsyncSession],
    claimed: LongTermMemoryMutationJob,
    *,
    owner: str,
    error: str,
    result: dict[str, Any] | None = None,
) -> None:
    assert claimed.id is not None
    async with session_factory() as db:
        changed = await memory_job_crud.mark_failed(
            db,
            uid=claimed.uid,
            job_id=claimed.id,
            owner=owner,
            error=error,
            result=result,
            commit=False,
        )
        assert changed
        await db.commit()


async def _create_failed_update_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    memory_key: str,
) -> tuple[LongTermMemoryRecord, LongTermMemoryMutationJob]:
    record = await create_recallable_record(
        session_factory,
        uid=uid,
        memory_key=memory_key,
        content=f"before-{memory_key}",
        version=1,
    )
    async with session_factory() as db:
        result = await memory_service.update(
            db,
            uid=uid,
            dedupe_key=f"update-{memory_key}",
            memory_id=record.id,
            expected_version=1,
            content=f"after-{memory_key}",
            memory_key=f"{memory_key}-updated",
            memory_type=LongTermMemoryType.FACT,
            change_evidence="stage7 retry",
            source=LongTermMemorySource.USER_API,
            source_id="stage7-source",
        )
    assert result.job is not None
    assert result.job.id is not None
    owner = f"stage7-failed-{memory_key}"
    claimed = await claim_job(
        session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.UPDATE,
        job_id=result.job.id,
        owner=owner,
    )
    assert claimed is not None
    await _fail_claimed_job(
        session_factory,
        claimed,
        owner=owner,
        error="publication failed",
    )
    async with session_factory() as db:
        failed = await memory_job_crud.get_by_id(db, uid=uid, job_id=result.job.id)
    assert failed is not None
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    return record, failed


@pytest.mark.asyncio
async def test_list_memories_filters_pages_and_isolates_uid(memory_session_factory) -> None:
    uid = "stage7-memory-owner"
    await configure_store(memory_session_factory, uid=uid)
    await configure_store(memory_session_factory, uid="stage7-memory-other")
    low = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="needle-low",
        content="needle low",
        memory_type=LongTermMemoryType.FACT,
    )
    await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="needle-todo",
        content="needle todo",
        memory_type=LongTermMemoryType.TODO,
    )
    await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="needle-high",
        content="needle high",
        version=3,
        memory_type=LongTermMemoryType.FACT,
    )
    await create_recallable_record(
        memory_session_factory,
        uid="stage7-memory-other",
        memory_key="needle-foreign",
        content="needle foreign",
        version=4,
        memory_type=LongTermMemoryType.FACT,
    )

    async with memory_session_factory() as db:
        result = await list_memories(
            db,
            uid=uid,
            skip=1,
            limit=1,
            keyword="needle",
            memory_type=LongTermMemoryType.FACT,
            sort_by="version",
            sort_order="desc",
        )

    assert result["total"] == 2
    assert result["skip"] == 1
    assert result["limit"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["id"] == low.id
    assert result["items"][0]["memory_key"] == "needle-low"


@pytest.mark.asyncio
async def test_memory_detail_cross_uid_is_not_found(memory_session_factory) -> None:
    uid = "stage7-detail-owner"
    await configure_store(memory_session_factory, uid=uid)
    record = await create_recallable_record(memory_session_factory, uid=uid, memory_key="private")

    async with memory_session_factory() as db:
        with pytest.raises(MemoryNotFoundError) as exc_info:
            await get_memory(db, uid="stage7-detail-other", memory_id=record.id)

    assert exc_info.value.message == ERR_MEMORY_RECORD_NOT_FOUND


@pytest.mark.asyncio
async def test_pin_management_entry_is_idempotent_persistent_and_uid_scoped(memory_session_factory) -> None:
    owner = "stage7-pin-owner"
    other = "stage7-pin-other"
    record = await create_recallable_record(
        memory_session_factory,
        uid=owner,
        memory_key="managed-pin",
    )

    async with memory_session_factory() as db:
        pinned = await pin_memory(db, uid=owner, memory_id=record.id)
        pinned_again = await pin_memory(db, uid=owner, memory_id=record.id)
        unpinned = await unpin_memory(db, uid=owner, memory_id=record.id)

        with pytest.raises(MemoryNotFoundError):
            await pin_memory(db, uid=other, memory_id=record.id)
        with pytest.raises(MemoryNotFoundError):
            await unpin_memory(db, uid=other, memory_id=record.id)

    assert pinned["id"] == record.id
    assert pinned["pinned"] is True
    assert pinned_again["id"] == record.id
    assert pinned_again["pinned"] is True
    assert unpinned["id"] == record.id
    assert unpinned["pinned"] is False

    async with memory_session_factory() as db:
        persisted = await memory_record_crud.get_by_id(db, uid=owner, memory_id=record.id)

    assert persisted is not None
    assert persisted.pinned is False


@pytest.mark.asyncio
async def test_pin_management_entry_rejects_records_reserved_by_mutation_jobs(memory_session_factory) -> None:
    uid = "stage7-pin-pending-owner"
    update_record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="pending-update-pin",
    )
    organization_record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="pending-organization-unpin",
    )

    async with memory_session_factory() as db:
        pinned = await pin_memory(db, uid=uid, memory_id=organization_record.id)
    assert pinned["pinned"] is True

    update_job = await _create_raw_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.UPDATE,
        dedupe_key="pending-update-pin-job",
        memory_id=update_record.id,
        expected_version=update_record.version,
    )
    organization_job = await _create_raw_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
        dedupe_key="pending-organization-unpin-job",
        memory_id=organization_record.id,
        expected_version=organization_record.version,
    )
    assert update_job.id is not None
    assert organization_job.id is not None

    async with memory_session_factory() as db:
        assert await memory_record_crud.reserve_pending_mutation(
            db,
            uid=uid,
            memory_id=update_record.id,
            job_id=update_job.id,
            expected_version=update_record.version,
        )
        assert await memory_record_crud.reserve_pending_mutation(
            db,
            uid=uid,
            memory_id=organization_record.id,
            job_id=organization_job.id,
            expected_version=organization_record.version,
        )

    async with memory_session_factory() as db:
        with pytest.raises(MemoryConflictError) as pin_exc:
            await pin_memory(db, uid=uid, memory_id=update_record.id)
        assert pin_exc.value.message == ERR_MEMORY_MUTATION_PENDING

        with pytest.raises(MemoryConflictError) as unpin_exc:
            await unpin_memory(db, uid=uid, memory_id=organization_record.id)
        assert unpin_exc.value.message == ERR_MEMORY_MUTATION_PENDING

    async with memory_session_factory() as db:
        persisted_update = await memory_record_crud.get_by_id(db, uid=uid, memory_id=update_record.id)
        persisted_organization = await memory_record_crud.get_by_id(db, uid=uid, memory_id=organization_record.id)

    assert persisted_update is not None
    assert persisted_update.pinned is False
    assert persisted_update.pending_mutation_job_id == update_job.id
    assert persisted_organization is not None
    assert persisted_organization.pinned is True
    assert persisted_organization.pending_mutation_job_id == organization_job.id


@pytest.mark.asyncio
async def test_list_jobs_filters_and_isolates_uid(memory_session_factory) -> None:
    owner = "stage7-job-owner"
    other = "stage7-job-other"
    owner_failed = await _create_raw_job(
        memory_session_factory,
        uid=owner,
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key="owner-failed",
        status=LongTermMemoryMutationStatus.FAILED,
    )
    await _create_raw_job(
        memory_session_factory,
        uid=owner,
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key="owner-pending",
        status=LongTermMemoryMutationStatus.PENDING,
    )
    await _create_raw_job(
        memory_session_factory,
        uid=other,
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key="other-failed",
        status=LongTermMemoryMutationStatus.FAILED,
    )

    async with memory_session_factory() as db:
        result = await list_jobs(
            db,
            uid=owner,
            skip=0,
            limit=1,
            status=LongTermMemoryMutationStatus.FAILED,
            operation=LongTermMemoryMutationOperation.CREATE,
        )

    assert result["total"] == 1
    assert [item["id"] for item in result["items"]] == [owner_failed.id]


@pytest.mark.asyncio
async def test_memory_settings_exposes_earliest_unfinished_job_and_isolates_uid(memory_session_factory) -> None:
    owner = "stage7-settings-owner"
    other = "stage7-settings-other"
    await configure_store(memory_session_factory, uid=owner)
    first = await _create_raw_job(
        memory_session_factory,
        uid=owner,
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key="settings-create",
        payload={"progress": {"success_count": 2, "total_count": 5}},
    )
    await _create_raw_job(
        memory_session_factory,
        uid=owner,
        operation=LongTermMemoryMutationOperation.UPDATE,
        dedupe_key="settings-update",
    )
    foreign = await _create_raw_job(
        memory_session_factory,
        uid=other,
        operation=LongTermMemoryMutationOperation.CREATE,
        dedupe_key="foreign-create",
    )

    async with memory_session_factory() as db:
        result = await get_memory_settings(db, uid=owner)

    current_job = result["current_job"]
    assert current_job is not None
    assert current_job["id"] == first.id
    assert current_job["operation"] == LongTermMemoryMutationOperation.CREATE.value
    assert current_job["status"] == LongTermMemoryMutationStatus.PENDING.value
    assert current_job["payload"]["progress"] == {"success_count": 2, "total_count": 5}
    assert current_job["id"] != foreign.id


@pytest.mark.asyncio
async def test_memory_history_page_reports_real_total(memory_session_factory) -> None:
    uid = "stage7-history-owner"
    await configure_store(memory_session_factory, uid=uid)
    record = await create_recallable_record(memory_session_factory, uid=uid, memory_key="history")
    async with memory_session_factory() as db:
        for version in (1, 2, 3):
            await memory_revision_crud.create(
                db,
                uid=uid,
                memory_id=record.id,
                version=version,
                memory_key="history",
                memory_type=LongTermMemoryType.FACT,
                content=f"history-{version}",
                content_hash=f"history-hash-{version}",
                source=LongTermMemorySource.AUTO_EXTRACT,
                commit=False,
            )
        deleted = await memory_record_crud.delete(
            db,
            uid=uid,
            memory_id=record.id,
            commit=False,
        )
        assert deleted is not None
        await db.commit()

    async with memory_session_factory() as db:
        result = await list_memory_history(db, uid=uid, memory_id=record.id, skip=1, limit=1)

    assert result["total"] == 3
    assert result["skip"] == 1
    assert result["limit"] == 1
    assert len(result["items"]) == 1
    assert result["items"][0]["version"] == 2


@pytest.mark.asyncio
async def test_failed_publication_retry_requeues_and_enforces_version_and_uid(
    memory_session_factory,
) -> None:
    uid = "stage7-publication-owner"
    await configure_store(memory_session_factory, uid=uid)
    record, failed = await _create_failed_update_job(
        memory_session_factory,
        uid=uid,
        memory_key="retry-success",
    )
    assert failed.id is not None

    async with memory_session_factory() as db:
        retried = await retry_job(db, uid=uid, job_id=failed.id)

    retry_view = retried["job"]
    assert retried["status"] == "accepted"
    assert retry_view["status"] == LongTermMemoryMutationStatus.PENDING.value
    assert retry_view["memory_id"] == record.id
    assert retry_view["expected_version"] == 1
    assert retry_view["payload"]["content"] == "after-retry-success"

    async with memory_session_factory() as db:
        retry_record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=record.id)
        persisted_retry_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=retry_view["id"])
    assert retry_record is not None
    assert retry_record.pending_mutation_job_id == retry_view["id"]
    assert persisted_retry_job is not None
    assert persisted_retry_job.uid == uid

    async with memory_session_factory() as db:
        with pytest.raises(MemoryNotFoundError):
            await retry_job(db, uid="stage7-publication-other", job_id=failed.id)

    stale_record, stale_failed = await _create_failed_update_job(
        memory_session_factory,
        uid=uid,
        memory_key="retry-stale",
    )
    assert stale_failed.id is not None
    async with memory_session_factory() as db:
        changed = await db.execute(update(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.id == stale_record.id).values(version=2))
        assert changed.rowcount == 1
        await db.commit()
    async with memory_session_factory() as db:
        with pytest.raises(MemoryConflictError):
            await retry_job(db, uid=uid, job_id=stale_failed.id)


@pytest.mark.asyncio
async def test_restore_jobs_remain_queryable_but_are_not_retryable(memory_session_factory) -> None:
    uid = "stage7-restore-compatibility"
    failed = await _create_raw_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.RESTORE,
        dedupe_key="legacy-restore",
        status=LongTermMemoryMutationStatus.FAILED,
        memory_id=42,
        expected_version=2,
        payload={"restored_from_version": 1},
    )
    assert failed.id is not None

    async with memory_session_factory() as db:
        jobs = await list_jobs(db, uid=uid, operation=LongTermMemoryMutationOperation.RESTORE)
        with pytest.raises(MemoryConflictError) as exc_info:
            await retry_job(db, uid=uid, job_id=failed.id)

    assert jobs["total"] == 1
    assert jobs["items"][0]["operation"] == LongTermMemoryMutationOperation.RESTORE.value
    assert exc_info.value.message == ERR_MEMORY_JOB_TARGET_STATE_CONFLICT


@pytest.mark.asyncio
async def test_new_restore_jobs_are_rejected_by_the_submission_manager(memory_session_factory) -> None:
    uid = "stage7-restore-submission-disabled"
    await configure_store(memory_session_factory, uid=uid)
    async with memory_session_factory() as db:
        with pytest.raises(MemoryJobValidationError):
            await memory_job_manager.submit(
                db,
                uid=uid,
                operation=LongTermMemoryMutationOperation.RESTORE,
                dedupe_key="new-restore",
                payload={"restored_from_version": 1},
                active_mutation_key="restore-target",
                memory_id=1,
                expected_version=1,
            )


@pytest.mark.asyncio
async def test_delete_cleanup_retry_only_requeues_failed_real_tombstone(memory_session_factory) -> None:
    uid = "stage7-cleanup-owner"
    await configure_store(memory_session_factory, uid=uid)
    record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="cleanup-retry",
        content="cleanup retry content",
        content_hash=build_memory_content_hash("cleanup retry content"),
    )

    async with memory_session_factory() as db:
        deleted = await memory_service.delete(
            db,
            uid=uid,
            dedupe_key="stage7-cleanup-delete",
            memory_id=record.id,
            expected_version=record.version,
            source=LongTermMemorySource.USER_API,
            max_attempts=1,
        )
    assert deleted.job is not None and deleted.job.id is not None
    failed_job_id = deleted.job.id
    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
        job_id=failed_job_id,
        owner="stage7-cleanup-failed-worker",
    )
    assert claimed is not None
    failure_result = {
        "phase": "delete_cleanup",
        "memory_id": record.id,
        "record_snapshot": claimed.payload["record_snapshot"],
    }
    await _fail_claimed_job(
        memory_session_factory,
        claimed,
        owner="stage7-cleanup-failed-worker",
        error="vector delete failed",
        result=failure_result,
    )

    async with memory_session_factory() as db:
        failed = await memory_job_crud.get_by_id(db, uid=uid, job_id=failed_job_id)
        tombstone = await memory_record_crud.get_by_id(db, uid=uid, memory_id=record.id)
    assert failed is not None
    assert failed.status == LongTermMemoryMutationStatus.FAILED
    assert failed.payload["record_snapshot"] == claimed.payload["record_snapshot"]
    assert failed.result == failure_result
    assert tombstone is not None
    assert tombstone.is_active is False
    assert tombstone.deleted_at is not None

    async with memory_session_factory() as db:
        retried = await retry_job(db, uid=uid, job_id=failed_job_id)

    retry_view = retried["job"]
    assert retried["status"] == "accepted"
    assert retry_view["id"] != failed_job_id
    assert retry_view["operation"] == LongTermMemoryMutationOperation.DELETE_CLEANUP.value
    assert retry_view["payload"]["record_snapshot"] == failed.payload["record_snapshot"]
    async with memory_session_factory() as db:
        retry_job_record = await memory_job_crud.get_by_id(db, uid=uid, job_id=retry_view["id"])
        retry_tombstone = await memory_record_crud.get_by_id(db, uid=uid, memory_id=record.id)
    assert retry_job_record is not None
    assert retry_job_record.uid == uid
    assert retry_job_record.status == LongTermMemoryMutationStatus.PENDING
    assert retry_tombstone is not None
    assert retry_tombstone.pending_mutation_job_id == retry_view["id"]

    async with memory_session_factory() as db:
        with pytest.raises(MemoryNotFoundError):
            await retry_job(db, uid="stage7-cleanup-other", job_id=failed_job_id)

    pending_record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="cleanup-pending",
        content="cleanup pending content",
        content_hash=build_memory_content_hash("cleanup pending content"),
    )
    async with memory_session_factory() as db:
        pending = await memory_service.delete(
            db,
            uid=uid,
            dedupe_key="stage7-cleanup-pending",
            memory_id=pending_record.id,
            expected_version=pending_record.version,
            max_attempts=1,
        )
        assert pending.job is not None and pending.job.id is not None
        with pytest.raises(MemoryConflictError) as exc_info:
            await retry_job(db, uid=uid, job_id=pending.job.id)
    assert exc_info.value.message == ERR_MEMORY_JOB_TARGET_STATE_CONFLICT

    cancelled_record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="cleanup-cancelled",
        content="cleanup cancelled content",
        content_hash=build_memory_content_hash("cleanup cancelled content"),
    )
    async with memory_session_factory() as db:
        cancelled = await memory_service.delete(
            db,
            uid=uid,
            dedupe_key="stage7-cleanup-cancelled",
            memory_id=cancelled_record.id,
            expected_version=cancelled_record.version,
            max_attempts=1,
        )
        assert cancelled.job is not None and cancelled.job.id is not None
        claimed_cancelled = await memory_job_crud.try_claim(
            db,
            uid=uid,
            job_id=cancelled.job.id,
            owner="stage7-cleanup-cancelled-worker",
            lease_seconds=30,
            commit=False,
        )
        assert claimed_cancelled is not None
        assert await memory_job_crud.mark_cancelled(
            db,
            uid=uid,
            job_id=cancelled.job.id,
            owner="stage7-cleanup-cancelled-worker",
            commit=False,
        )
        await db.commit()
        with pytest.raises(MemoryConflictError) as exc_info:
            await retry_job(db, uid=uid, job_id=cancelled.job.id)
    assert exc_info.value.message == ERR_MEMORY_JOB_TARGET_STATE_CONFLICT
