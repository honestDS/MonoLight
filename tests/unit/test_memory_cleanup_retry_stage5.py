from datetime import timedelta
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
from sqlalchemy import update

from tests.unit.memory_stage5_test_support import Stage5VectorBackend, claim_job, configure_store


class ImportSafePersistentClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass


with patch.object(chromadb, "PersistentClient", ImportSafePersistentClient):
    from app.core.crud.memory import memory_embedding_revision_crud, memory_store_crud
    from app.core.crud.memory_job import memory_job_crud
    from app.core.memory import submit_memory_cleanup_retry
    from app.core.memory_jobs import reindex_handler
    from app.core.memory_jobs.consumer import MemoryJobConsumer
    from app.core.memory_jobs.executor import MemoryJobExecutor
    from app.core.memory_jobs.maintenance_lifecycle import finalize_maintenance_terminal_state
    from app.core.memory_jobs.manager import memory_job_manager
    from app.core.memory_jobs.migration_handler import handle_embedding_migration
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


async def _create_migration_job(
    session_factory: Any,
    *,
    uid: str,
    source: dict[str, Any],
    target: dict[str, Any],
    dedupe_key: str,
) -> Any:
    payload = {"from": dict(source), "target": dict(target)}
    async with session_factory() as db:
        started_at = await get_database_time(db)
        submission = await memory_job_manager.submit(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
            dedupe_key=dedupe_key,
            payload=payload,
            commit=False,
        )
        job = submission.job
        assert submission.created
        assert job.id is not None
        started = await memory_store_crud.start_embedding_migration(
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
        assert started is not None
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
            status=LongTermMemoryEmbeddingRevisionStatus.CONFIRMED,
            confirmed_at=started_at,
            commit=False,
        )
        assert revision is not None
        await db.commit()
        return job


@pytest.mark.asyncio
async def test_reindex_cleanup_retry_after_switch(
    memory_session_factory: Any,
    vector_backend: Stage5VectorBackend,
) -> None:
    uid = "stage5-cleanup-reindex"
    source_collection = "stage5-cleanup-reindex-source"
    target_collection = "stage5-cleanup-reindex-target"
    embedding_revision = 4
    target_index_revision = 8
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=11,
        model_id="stage5-reindex-model",
        dimensions=3,
        signature="stage5-reindex-signature",
        active_revision=embedding_revision,
        index_revision=target_index_revision,
        collection_name=target_collection,
    )

    payload = {
        "from": {
            "channel_id": 11,
            "model_id": "stage5-reindex-model",
            "dimensions": 3,
            "signature": "stage5-reindex-signature",
            "embedding_revision": embedding_revision,
            "collection": source_collection,
            "index_revision": target_index_revision - 1,
        },
        "target": {
            "collection": target_collection,
            "index_revision": target_index_revision,
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
    original_job = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        dedupe_key="stage5-cleanup-reindex-original",
        owner="stage5-cleanup-reindex-owner",
        payload=payload,
    )
    assert original_job is not None
    assert original_job.id is not None
    assert original_job.locked_by is not None
    original_job_id = original_job.id
    async with memory_session_factory() as db:
        changed = await memory_job_crud.mark_failed(
            db,
            uid=uid,
            job_id=original_job_id,
            owner=original_job.locked_by,
            error="old collection cleanup failed",
            commit=False,
        )
        assert changed
        await finalize_maintenance_terminal_state(
            db,
            job=original_job,
            status=LongTermMemoryMutationStatus.FAILED,
            error="old collection cleanup failed",
        )
        updated_store = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            old_collection_name=source_collection,
            old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.FAILED,
            old_collection_cleanup_job_id=original_job_id,
            commit=False,
        )
        assert updated_store is not None
        await db.commit()

    await vector_backend.get_or_create_collection(source_collection, metadata={"legacy": True})
    async with memory_session_factory() as db:
        first = await submit_memory_cleanup_retry(
            db,
            uid=uid,
            dedupe_key="stage5-cleanup-reindex-retry",
        )
    assert first.created
    assert first.job.id is not None
    assert first.job.id != original_job_id
    assert first.job.operation == LongTermMemoryMutationOperation.REINDEX
    assert first.job.status == LongTermMemoryMutationStatus.PENDING
    retry_job_id = first.job.id

    async with memory_session_factory() as db:
        store_after_submit = await memory_store_crud.get_by_uid(db, uid=uid)
    assert store_after_submit is not None
    assert store_after_submit.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.PENDING
    assert store_after_submit.old_collection_cleanup_job_id == retry_job_id

    async with memory_session_factory() as db:
        second = await submit_memory_cleanup_retry(
            db,
            uid=uid,
            dedupe_key="stage5-cleanup-reindex-retry",
        )
    assert not second.created
    assert second.job.id == retry_job_id

    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        job_id=retry_job_id,
        owner="stage5-cleanup-reindex-worker",
    )
    assert claimed is not None
    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.REINDEX: reindex_handler.handle_reindex},
        session_factory=memory_session_factory,
    )
    result = await executor.execute_claimed(claimed, "stage5-cleanup-reindex-worker")

    assert result.finalized
    assert result.result["finalized"] is True
    async with memory_session_factory() as db:
        finished_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=retry_job_id)
        original_finished_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=original_job_id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert finished_job is not None
    assert finished_job.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert original_finished_job is not None
    assert original_finished_job.status == LongTermMemoryMutationStatus.FAILED
    assert store is not None
    assert store.active_embedding_channel_id == 11
    assert store.active_embedding_model_id == "stage5-reindex-model"
    assert store.active_embedding_dimensions == 3
    assert store.active_embedding_signature == "stage5-reindex-signature"
    assert store.active_embedding_revision == embedding_revision
    assert store.active_collection_name == target_collection
    assert store.index_revision == target_index_revision
    assert store.index_status == LongTermMemoryIndexStatus.READY
    assert store.old_collection_name == source_collection
    assert store.old_collection_cleanup_job_id == retry_job_id
    assert store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.SUCCEEDED
    assert source_collection not in vector_backend.collections


@pytest.mark.asyncio
async def test_reindex_cleanup_retry_after_expired_switch_failure(
    memory_session_factory: Any,
) -> None:
    uid = "stage5-cleanup-reindex-expired"
    source_collection = "stage5-cleanup-reindex-expired-source"
    target_collection = "stage5-cleanup-reindex-expired-target"
    embedding_revision = 4
    target_index_revision = 8
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=11,
        model_id="stage5-reindex-model",
        dimensions=3,
        signature="stage5-reindex-signature",
        active_revision=embedding_revision,
        index_revision=target_index_revision,
        collection_name=target_collection,
    )
    payload = {
        "from": {
            "channel_id": 11,
            "model_id": "stage5-reindex-model",
            "dimensions": 3,
            "signature": "stage5-reindex-signature",
            "embedding_revision": embedding_revision,
            "collection": source_collection,
            "index_revision": target_index_revision - 1,
        },
        "target": {
            "collection": target_collection,
            "index_revision": target_index_revision,
        },
        "progress": {
            "phase": "switching",
            "snapshot_initialized": True,
            "snapshot_boundary": 0,
            "cursor": 0,
            "total_count": 0,
            "success_count": 0,
            "failure_count": 0,
        },
    }
    original_job = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        dedupe_key="stage5-cleanup-reindex-expired-original",
        owner="stage5-cleanup-reindex-expired-owner",
        payload=payload,
        max_attempts=1,
    )
    assert original_job is not None
    assert original_job.id is not None
    original_job_id = original_job.id

    async with memory_session_factory() as db:
        updated_store = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            old_collection_name=source_collection,
            old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.RUNNING,
            old_collection_cleanup_job_id=original_job_id,
            commit=False,
        )
        assert updated_store is not None
        now = await get_database_time(db)
        result = await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == original_job_id,
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
        failed_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=original_job_id)
        failed_store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert failed_job is not None
    assert failed_job.status == LongTermMemoryMutationStatus.FAILED
    assert failed_store is not None
    assert failed_store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.FAILED
    assert failed_store.active_collection_name == target_collection
    assert failed_store.index_revision == target_index_revision
    assert failed_store.index_status == LongTermMemoryIndexStatus.READY

    async with memory_session_factory() as db:
        submission = await submit_memory_cleanup_retry(
            db,
            uid=uid,
            dedupe_key="stage5-cleanup-reindex-expired-retry",
        )
    assert submission.created
    assert submission.job.id is not None
    assert submission.job.id != original_job_id
    assert submission.job.status == LongTermMemoryMutationStatus.PENDING

    async with memory_session_factory() as db:
        retried_store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert retried_store is not None
    assert retried_store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.PENDING
    assert retried_store.old_collection_cleanup_job_id == submission.job.id


@pytest.mark.asyncio
async def test_migration_cleanup_retry_after_cancellation(
    memory_session_factory: Any,
    vector_backend: Stage5VectorBackend,
) -> None:
    uid = "stage5-cleanup-migration"
    source_collection = "stage5-cleanup-migration-source"
    target_collection = "stage5-cleanup-migration-target"
    source = {
        "channel_id": 21,
        "model_id": "stage5-migration-source-model",
        "dimensions": 3,
        "signature": "stage5-migration-source-signature",
        "collection": source_collection,
        "revision": 5,
    }
    target = {
        "channel_id": 22,
        "model_id": "stage5-migration-target-model",
        "dimensions": 4,
        "signature": "stage5-migration-target-signature",
        "collection": target_collection,
        "revision": 6,
    }
    original_index_revision = 12
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=original_index_revision,
        collection_name=source_collection,
    )
    original_job = await _create_migration_job(
        memory_session_factory,
        uid=uid,
        source=source,
        target=target,
        dedupe_key="stage5-cleanup-migration-original",
    )
    assert original_job.id is not None
    original_job_id = original_job.id
    async with memory_session_factory() as db:
        cancellation = await memory_job_manager.request_cancel(db, uid=uid, job_id=original_job_id)
    assert cancellation.accepted
    assert cancellation.changed
    assert cancellation.job is not None
    assert cancellation.job.status == LongTermMemoryMutationStatus.CANCELLED

    async with memory_session_factory() as db:
        await finalize_maintenance_terminal_state(
            db,
            job=original_job,
            status=LongTermMemoryMutationStatus.CANCELLED,
            error="migration cancelled",
        )
        updated_store = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            migration_status=LongTermMemoryMigrationStatus.CANCELLED,
            migration_job_id=original_job_id,
            old_collection_name=target_collection,
            old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.FAILED,
            old_collection_cleanup_job_id=original_job_id,
            commit=False,
        )
        assert updated_store is not None
        await db.commit()

    await vector_backend.get_or_create_collection(target_collection, metadata={"cancelled": True})
    async with memory_session_factory() as db:
        submission = await submit_memory_cleanup_retry(
            db,
            uid=uid,
            dedupe_key="stage5-cleanup-migration-retry",
        )
    assert submission.created
    assert submission.job.id is not None
    assert submission.job.operation == LongTermMemoryMutationOperation.EMBEDDING_MIGRATION
    assert submission.job.status == LongTermMemoryMutationStatus.PENDING
    retry_job_id = submission.job.id

    async with memory_session_factory() as db:
        store_after_submit = await memory_store_crud.get_by_uid(db, uid=uid)
    assert store_after_submit is not None
    assert store_after_submit.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.PENDING
    assert store_after_submit.old_collection_cleanup_job_id == retry_job_id

    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        job_id=retry_job_id,
        owner="stage5-cleanup-migration-worker",
    )
    assert claimed is not None
    executor = MemoryJobExecutor(
        {
            LongTermMemoryMutationOperation.EMBEDDING_MIGRATION: handle_embedding_migration,
        },
        session_factory=memory_session_factory,
    )
    result = await executor.execute_claimed(claimed, "stage5-cleanup-migration-worker")

    assert result.finalized
    assert result.result["finalized"] is True
    async with memory_session_factory() as db:
        finished_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=retry_job_id)
        original_finished_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=original_job_id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert finished_job is not None
    assert finished_job.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert original_finished_job is not None
    assert original_finished_job.status == LongTermMemoryMutationStatus.CANCELLED
    assert store is not None
    assert store.active_embedding_channel_id == source["channel_id"]
    assert store.active_embedding_model_id == source["model_id"]
    assert store.active_embedding_dimensions == source["dimensions"]
    assert store.active_embedding_signature == source["signature"]
    assert store.active_embedding_revision == source["revision"]
    assert store.active_collection_name == source_collection
    assert store.index_revision == original_index_revision
    assert store.migration_status == LongTermMemoryMigrationStatus.CANCELLED
    assert store.migration_job_id == original_job_id
    assert store.old_collection_name == target_collection
    assert store.old_collection_cleanup_job_id == retry_job_id
    assert store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.SUCCEEDED
    assert target_collection not in vector_backend.collections
    deleted_count = 0
    for deleted_collection in vector_backend.deleted_collections:
        assert deleted_collection == target_collection
        deleted_count += 1
    assert deleted_count == 1
