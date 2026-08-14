from datetime import datetime, timedelta
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.memory_stage5_test_support import Stage5VectorBackend, claim_job, configure_store


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass


with patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient):
    from app.core.crud.memory import memory_embedding_revision_crud, memory_store_crud
    from app.core.crud.memory_job import memory_job_crud
    from app.core.crud.memory_maintenance import memory_maintenance_store_crud
    from app.core.memory_jobs.consumer import MemoryJobConsumer
    from app.core.memory_jobs.executor import MemoryJobExecutor
    from app.core.memory_jobs.maintenance_lifecycle import finalize_maintenance_terminal_state
    from app.core.memory_jobs.manager import memory_job_manager
    from app.models.memory import (
        LongTermMemoryEmbeddingRevisionStatus,
        LongTermMemoryIndexStatus,
        LongTermMemoryMigrationStatus,
        LongTermMemoryMutationJob,
        LongTermMemoryMutationOperation,
        LongTermMemoryMutationStatus,
        LongTermMemoryOldCollectionCleanupStatus,
    )
    from app.providers.database.time import get_database_time


pytest_plugins = ("tests.unit.memory_stage5_fixture",)


def _migration_configs(
    *,
    source_collection: str,
    target_collection: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = {
        "channel_id": 1,
        "model_id": "stage5-source-model",
        "dimensions": 3,
        "signature": "stage5-source-signature",
        "collection": source_collection,
        "revision": 1,
    }
    target = {
        "channel_id": 2,
        "model_id": "stage5-target-model",
        "dimensions": 4,
        "signature": "stage5-target-signature",
        "collection": target_collection,
        "revision": 2,
    }
    return source, target


def _reindex_payload(
    *,
    source_collection: str,
    target_collection: str,
    index_revision: int,
) -> dict[str, Any]:
    return {
        "from": {
            "channel_id": 1,
            "model_id": "stage5-reindex-model",
            "dimensions": 3,
            "signature": "stage5-reindex-signature",
            "embedding_revision": 1,
            "collection": source_collection,
            "index_revision": index_revision,
        },
        "target": {
            "collection": target_collection,
            "index_revision": index_revision + 1,
        },
        "progress": {
            "phase": "building",
            "snapshot_initialized": True,
            "snapshot_boundary": 0,
            "cursor": 0,
            "total_count": 0,
            "success_count": 0,
            "failure_count": 0,
        },
    }


async def _prepare_embedding_migration(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    source: dict[str, Any],
    target: dict[str, Any],
    dedupe_key: str,
    max_attempts: int = 3,
    revision_status: LongTermMemoryEmbeddingRevisionStatus = LongTermMemoryEmbeddingRevisionStatus.CONFIRMED,
) -> LongTermMemoryMutationJob:
    payload = {"from": dict(source), "target": dict(target)}
    async with session_factory() as db:
        started_at: datetime = await get_database_time(db)
        submission = await memory_job_manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            dedupe_key=dedupe_key,
            payload=payload,
            max_attempts=max_attempts,
            commit=False,
        )
        job = submission.job
        assert job.id is not None
        store = await memory_store_crud.start_embedding_migration(
            db,
            uid=uid,
            job_id=job.id,
            expected_active_revision=source["revision"],
            target_embedding_channel_id=target["channel_id"],
            target_embedding_model_id=target["model_id"],
            target_embedding_dimensions=target["dimensions"],
            target_embedding_signature=target["signature"],
            target_collection_name=target["collection"],
            migration_started_at=started_at,
            commit=False,
        )
        assert store is not None
        revision = await memory_embedding_revision_crud.create(
            db,
            uid=uid,
            revision=target["revision"],
            from_channel_id=source["channel_id"],
            from_model_id=source["model_id"],
            from_dimensions=source["dimensions"],
            from_signature=source["signature"],
            from_collection=source["collection"],
            to_channel_id=target["channel_id"],
            to_model_id=target["model_id"],
            to_dimensions=target["dimensions"],
            to_signature=target["signature"],
            to_collection=target["collection"],
            job_id=job.id,
            status=revision_status,
            confirmed_at=started_at,
            started_at=started_at if revision_status == LongTermMemoryEmbeddingRevisionStatus.RUNNING else None,
            commit=False,
        )
        assert revision is not None
        await db.commit()
        return job


async def _mark_failed_and_finalize(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    claimed: LongTermMemoryMutationJob,
    owner: str,
    error: str,
) -> None:
    assert claimed.id is not None
    async with session_factory() as db:
        changed = await memory_job_crud.mark_failed(
            db,
            uid=claimed.uid,
            job_id=claimed.id,
            owner=owner,
            error=error,
            commit=False,
        )
        assert changed
        await finalize_maintenance_terminal_state(
            db,
            job=claimed,
            status=LongTermMemoryMutationStatus.FAILED,
            error=error,
        )
        await db.commit()


@pytest.mark.asyncio
async def test_corrupt_reindex_payload_does_not_rollback_generic_terminal_state(
    memory_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    uid = "stage5-lifecycle-corrupt-reindex"
    source_collection = "stage5-corrupt-source"
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=1,
        model_id="stage5-reindex-model",
        dimensions=3,
        signature="stage5-reindex-signature",
        active_revision=1,
        index_revision=4,
        collection_name=source_collection,
    )

    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        owner="stage5-corrupt-worker",
        payload={"corrupt": True},
    )
    assert claimed is not None
    assert claimed.locked_by is not None
    await _mark_failed_and_finalize(
        memory_session_factory,
        claimed=claimed,
        owner=claimed.locked_by,
        error="corrupt reindex payload",
    )

    async with memory_session_factory() as db:
        job = await memory_job_crud.get_by_id(db, uid=uid, job_id=claimed.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert job is not None
    assert job.status == LongTermMemoryMutationStatus.FAILED
    assert store is not None
    assert store.index_status == LongTermMemoryIndexStatus.READY
    assert store.active_collection_name == source_collection
    assert store.active_embedding_revision == 1
    assert store.index_revision == 4


@pytest.mark.asyncio
async def test_failed_reindex_finalization_marks_progress_and_store_failed(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: Stage5VectorBackend,
) -> None:
    uid = "stage5-lifecycle-failed-reindex"
    source_collection = "stage5-failed-source"
    target_collection = "stage5-failed-target"
    index_revision = 6
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=1,
        model_id="stage5-reindex-model",
        dimensions=3,
        signature="stage5-reindex-signature",
        active_revision=1,
        index_revision=index_revision,
        collection_name=source_collection,
    )
    payload = _reindex_payload(
        source_collection=source_collection,
        target_collection=target_collection,
        index_revision=index_revision,
    )
    await vector_backend.get_or_create_collection(target_collection)
    async with memory_session_factory() as db:
        started = await memory_maintenance_store_crud.start_reindex(
            db,
            uid=uid,
            expected_active_revision=1,
            expected_active_collection_name=source_collection,
            expected_index_revision=index_revision,
            commit=True,
        )
    assert started is not None

    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        owner="stage5-failed-worker",
        payload=payload,
    )
    assert claimed is not None
    assert claimed.locked_by is not None
    await _mark_failed_and_finalize(
        memory_session_factory,
        claimed=claimed,
        owner=claimed.locked_by,
        error="reindex failed",
    )

    async with memory_session_factory() as db:
        job = await memory_job_crud.get_by_id(db, uid=uid, job_id=claimed.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert job is not None
    assert job.status == LongTermMemoryMutationStatus.FAILED
    assert job.payload["progress"]["failure_count"] >= 1
    assert store is not None
    assert store.index_status == LongTermMemoryIndexStatus.FAILED
    assert store.active_collection_name == source_collection
    assert store.active_embedding_revision == 1
    assert store.index_revision == index_revision
    assert store.active_collection_name != target_collection
    assert target_collection not in vector_backend.collections
    assert target_collection in vector_backend.deleted_collections


@pytest.mark.asyncio
async def test_preparing_migration_pending_job_can_be_cancelled(
    memory_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    uid = "stage5-lifecycle-cancel-preparing"
    source_collection = "stage5-cancel-source"
    target_collection = "stage5-cancel-target"
    source, target = _migration_configs(
        source_collection=source_collection,
        target_collection=target_collection,
    )
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=1,
        collection_name=source["collection"],
    )
    job = await _prepare_embedding_migration(
        memory_session_factory,
        uid=uid,
        source=source,
        target=target,
        dedupe_key="stage5-cancel-preparing",
    )
    assert job.id is not None

    async with memory_session_factory() as db:
        cancellation = await memory_job_manager.request_cancel(db, uid=uid, job_id=job.id)
    assert cancellation.accepted
    assert cancellation.changed

    async with memory_session_factory() as db:
        cancelled_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
        revision = await memory_embedding_revision_crud.get_by_revision(
            db,
            uid=uid,
            revision=target["revision"],
        )
    assert cancelled_job is not None
    assert cancelled_job.status == LongTermMemoryMutationStatus.CANCELLED
    assert store is not None
    assert store.migration_status == LongTermMemoryMigrationStatus.CANCELLED
    assert store.active_embedding_channel_id == source["channel_id"]
    assert store.active_embedding_model_id == source["model_id"]
    assert store.active_embedding_dimensions == source["dimensions"]
    assert store.active_embedding_signature == source["signature"]
    assert store.active_embedding_revision == source["revision"]
    assert store.active_collection_name == source_collection
    assert store.active_collection_name != target_collection
    assert revision is not None
    assert revision.status == LongTermMemoryEmbeddingRevisionStatus.CANCELLED


@pytest.mark.asyncio
async def test_switching_migration_rejects_cancellation(
    memory_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    uid = "stage5-lifecycle-cancel-switching"
    source_collection = "stage5-switching-source"
    target_collection = "stage5-switching-target"
    source, target = _migration_configs(
        source_collection=source_collection,
        target_collection=target_collection,
    )
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=1,
        collection_name=source["collection"],
    )
    job = await _prepare_embedding_migration(
        memory_session_factory,
        uid=uid,
        source=source,
        target=target,
        dedupe_key="stage5-cancel-switching",
    )
    assert job.id is not None
    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        job_id=job.id,
        owner="stage5-switching-worker",
    )
    assert claimed is not None

    async with memory_session_factory() as db:
        updated = await memory_maintenance_store_crud.update_embedding_migration_progress(
            db,
            uid=uid,
            migration_job_id=job.id,
            migration_status=LongTermMemoryMigrationStatus.SWITCHING,
            commit=True,
        )
    assert updated is not None

    async with memory_session_factory() as db:
        cancellation = await memory_job_manager.request_cancel(db, uid=uid, job_id=job.id)
    assert not cancellation.accepted
    assert not cancellation.changed
    assert cancellation.error

    async with memory_session_factory() as db:
        current_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert current_job is not None
    assert current_job.status == LongTermMemoryMutationStatus.RUNNING
    assert current_job.cancel_requested_at is None
    assert store is not None
    assert store.migration_status == LongTermMemoryMigrationStatus.SWITCHING
    assert store.active_embedding_revision == source["revision"]
    assert store.active_collection_name == source_collection
    assert store.active_collection_name != target_collection


@pytest.mark.asyncio
async def test_expired_max_attempt_migration_is_failed_and_not_switched(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: Stage5VectorBackend,
) -> None:
    uid = "stage5-lifecycle-expired-migration"
    source_collection = "stage5-expired-source"
    target_collection = "stage5-expired-target"
    source, target = _migration_configs(
        source_collection=source_collection,
        target_collection=target_collection,
    )
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=1,
        collection_name=source["collection"],
    )
    job = await _prepare_embedding_migration(
        memory_session_factory,
        uid=uid,
        source=source,
        target=target,
        dedupe_key="stage5-expired-migration",
        max_attempts=1,
        revision_status=LongTermMemoryEmbeddingRevisionStatus.RUNNING,
    )
    assert job.id is not None
    await vector_backend.get_or_create_collection(target_collection)
    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        job_id=job.id,
        owner="stage5-expired-worker",
    )
    assert claimed is not None

    async with memory_session_factory() as db:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job.id,
            )
            .values(lock_until=now - timedelta(seconds=1))
        )
        assert result.rowcount == 1
        await db.commit()

    consumer = MemoryJobConsumer(
        MemoryJobExecutor(session_factory=memory_session_factory),
        session_factory=memory_session_factory,
    )
    await consumer._recover_expired()

    async with memory_session_factory() as db:
        failed_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
        revision = await memory_embedding_revision_crud.get_by_revision(
            db,
            uid=uid,
            revision=target["revision"],
        )
    assert failed_job is not None
    assert failed_job.status == LongTermMemoryMutationStatus.FAILED
    assert store is not None
    assert store.migration_status == LongTermMemoryMigrationStatus.FAILED
    assert store.migration_failure_count >= 1
    assert store.active_embedding_channel_id == source["channel_id"]
    assert store.active_embedding_model_id == source["model_id"]
    assert store.active_embedding_dimensions == source["dimensions"]
    assert store.active_embedding_signature == source["signature"]
    assert store.active_embedding_revision == source["revision"]
    assert store.active_collection_name == source_collection
    assert store.active_collection_name != target_collection
    assert target_collection not in vector_backend.collections
    assert target_collection in vector_backend.deleted_collections
    assert revision is not None
    assert revision.status == LongTermMemoryEmbeddingRevisionStatus.FAILED


@pytest.mark.asyncio
async def test_expired_max_attempt_reindex_after_switch_fails_cleanup_only(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: Stage5VectorBackend,
) -> None:
    uid = "stage5-lifecycle-expired-switched-reindex"
    source_collection = "stage5-expired-switched-reindex-source"
    target_collection = "stage5-expired-switched-reindex-target"
    source_index_revision = 4
    target_index_revision = 5
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=1,
        model_id="stage5-reindex-model",
        dimensions=3,
        signature="stage5-reindex-signature",
        active_revision=1,
        index_revision=source_index_revision,
        collection_name=source_collection,
    )
    payload = _reindex_payload(
        source_collection=source_collection,
        target_collection=target_collection,
        index_revision=source_index_revision,
    )
    await vector_backend.get_or_create_collection(target_collection)
    async with memory_session_factory() as db:
        started = await memory_maintenance_store_crud.start_reindex(
            db,
            uid=uid,
            expected_active_revision=1,
            expected_active_collection_name=source_collection,
            expected_index_revision=source_index_revision,
            commit=True,
        )
    assert started is not None
    job = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        owner="stage5-expired-switched-reindex-worker",
        payload=payload,
        max_attempts=1,
    )
    assert job is not None
    assert job.id is not None

    async with memory_session_factory() as db:
        switched = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            active_collection_name=target_collection,
            index_revision=target_index_revision,
            index_status=LongTermMemoryIndexStatus.READY,
            old_collection_name=source_collection,
            old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.RUNNING,
            old_collection_cleanup_job_id=job.id,
            commit=True,
        )
    assert switched is not None

    async with memory_session_factory() as db:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job.id,
            )
            .values(lock_until=now - timedelta(seconds=1))
        )
        assert result.rowcount == 1
        await db.commit()

    consumer = MemoryJobConsumer(
        MemoryJobExecutor(session_factory=memory_session_factory),
        session_factory=memory_session_factory,
    )
    await consumer._recover_expired()

    async with memory_session_factory() as db:
        failed_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert failed_job is not None
    assert failed_job.status == LongTermMemoryMutationStatus.FAILED
    assert store is not None
    assert store.active_collection_name == target_collection
    assert store.active_embedding_revision == 1
    assert store.index_revision == target_index_revision
    assert store.index_status == LongTermMemoryIndexStatus.READY
    assert store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.FAILED
    assert store.old_collection_cleanup_error is not None
    assert store.old_collection_cleanup_error == failed_job.error
    assert store.old_collection_cleanup_at is None
    assert target_collection in vector_backend.collections
    assert target_collection not in vector_backend.deleted_collections


@pytest.mark.asyncio
async def test_expired_max_attempt_migration_after_switch_fails_cleanup_only(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: Stage5VectorBackend,
) -> None:
    uid = "stage5-lifecycle-expired-switched-migration"
    source_collection = "stage5-expired-switched-migration-source"
    target_collection = "stage5-expired-switched-migration-target"
    source, target = _migration_configs(
        source_collection=source_collection,
        target_collection=target_collection,
    )
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=1,
        collection_name=source_collection,
    )
    job = await _prepare_embedding_migration(
        memory_session_factory,
        uid=uid,
        source=source,
        target=target,
        dedupe_key="stage5-expired-switched-migration",
        max_attempts=1,
        revision_status=LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED,
    )
    assert job.id is not None
    await vector_backend.get_or_create_collection(target_collection)
    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        job_id=job.id,
        owner="stage5-expired-switched-migration-worker",
    )
    assert claimed is not None

    async with memory_session_factory() as db:
        switched = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            active_embedding_channel_id=target["channel_id"],
            active_embedding_model_id=target["model_id"],
            active_embedding_dimensions=target["dimensions"],
            active_embedding_signature=target["signature"],
            active_embedding_revision=target["revision"],
            active_collection_name=target_collection,
            index_revision=2,
            index_status=LongTermMemoryIndexStatus.READY,
            target_embedding_channel_id=None,
            target_embedding_model_id=None,
            target_embedding_dimensions=None,
            target_embedding_signature=None,
            target_collection_name=None,
            migration_status=LongTermMemoryMigrationStatus.SUCCEEDED,
            old_collection_name=source_collection,
            old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.RUNNING,
            old_collection_cleanup_job_id=job.id,
            commit=True,
        )
    assert switched is not None

    async with memory_session_factory() as db:
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job.id,
            )
            .values(lock_until=now - timedelta(seconds=1))
        )
        assert result.rowcount == 1
        await db.commit()

    consumer = MemoryJobConsumer(
        MemoryJobExecutor(session_factory=memory_session_factory),
        session_factory=memory_session_factory,
    )
    await consumer._recover_expired()

    async with memory_session_factory() as db:
        failed_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
        revision = await memory_embedding_revision_crud.get_by_revision(
            db,
            uid=uid,
            revision=target["revision"],
        )
    assert failed_job is not None
    assert failed_job.status == LongTermMemoryMutationStatus.FAILED
    assert store is not None
    assert store.migration_status == LongTermMemoryMigrationStatus.SUCCEEDED
    assert store.active_embedding_channel_id == target["channel_id"]
    assert store.active_embedding_model_id == target["model_id"]
    assert store.active_embedding_dimensions == target["dimensions"]
    assert store.active_embedding_signature == target["signature"]
    assert store.active_embedding_revision == target["revision"]
    assert store.active_collection_name == target_collection
    assert store.index_revision == 2
    assert store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.FAILED
    assert store.old_collection_cleanup_error is not None
    assert store.old_collection_cleanup_error == failed_job.error
    assert store.old_collection_cleanup_at is None
    assert revision is not None
    assert revision.status == LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED
    assert target_collection in vector_backend.collections
    assert target_collection not in vector_backend.deleted_collections
