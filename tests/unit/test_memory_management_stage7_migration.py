from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from tests.unit.memory_stage5_test_support import (
    MEMORY_TABLES,
    Stage5VectorBackend,
    claim_job,
    configure_store,
    create_recallable_record,
)


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass


with patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient):
    from app.core.constants import ERR_MEMORY_MIGRATION_NOT_FOUND
    from app.core.crud.memory import memory_embedding_revision_crud, memory_store_crud
    from app.core.crud.memory_job import memory_job_crud
    from app.core.memory import (
        MemoryNotFoundError,
        cancel_embedding_migration,
        get_embedding_migration,
        get_memory_settings,
        list_embedding_migrations,
        retry_embedding_migration,
    )
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


def _migration_configs(prefix: str) -> tuple[dict[str, Any], dict[str, Any]]:
    return (
        {
            "channel_id": 1,
            "model_id": f"{prefix}-source-model",
            "dimensions": 3,
            "signature": f"{prefix}-source-signature",
            "collection": f"{prefix}-source-collection",
            "revision": 1,
        },
        {
            "channel_id": 2,
            "model_id": f"{prefix}-target-model",
            "dimensions": 4,
            "signature": f"{prefix}-target-signature",
            "collection": f"{prefix}-target-collection",
            "revision": 2,
        },
    )


async def _create_migration_job(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    source: dict[str, Any],
    target: dict[str, Any],
    dedupe_key: str,
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
            commit=False,
        )
        job = submission.job
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


async def _fail_migration(
    session_factory: async_sessionmaker[AsyncSession],
    job: LongTermMemoryMutationJob,
) -> None:
    assert job.id is not None
    owner = "stage7-migration-failure-worker"
    claimed = await claim_job(
        session_factory,
        uid=job.uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        job_id=job.id,
        owner=owner,
    )
    assert claimed is not None
    async with session_factory() as db:
        changed = await memory_job_crud.mark_failed(
            db,
            uid=job.uid,
            job_id=job.id,
            owner=owner,
            error="migration failed",
            commit=False,
        )
        assert changed
        await finalize_maintenance_terminal_state(
            db,
            job=claimed,
            status=LongTermMemoryMutationStatus.FAILED,
            error="migration failed",
        )
        await db.commit()


@pytest.mark.asyncio
async def test_memory_settings_expose_active_target_progress_capacity_and_cleanup(
    memory_session_factory,
) -> None:
    uid = "stage7-settings-owner"
    source, target = _migration_configs("stage7-settings")
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=7,
        collection_name=source["collection"],
        max_active_records=9,
    )
    await create_recallable_record(memory_session_factory, uid=uid, memory_key="active")
    job = await _create_migration_job(
        memory_session_factory,
        uid=uid,
        source=source,
        target=target,
        dedupe_key="stage7-settings-migration",
    )
    assert job.id is not None
    async with memory_session_factory() as db:
        updated = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            migration_status=LongTermMemoryMigrationStatus.BUILDING,
            migration_snapshot_boundary=20,
            migration_cursor=4,
            migration_total_count=20,
            migration_success_count=3,
            migration_failure_count=1,
            migration_delta_high_watermark=8,
            migration_delta_applied_watermark=6,
            migration_error="migration warning",
            index_status=LongTermMemoryIndexStatus.READY,
            old_collection_name="stage7-settings-old",
            old_collection_cleanup_status=LongTermMemoryOldCollectionCleanupStatus.FAILED,
            old_collection_cleanup_job_id=77,
            old_collection_cleanup_error="cleanup failed",
        )
        assert updated is not None
        result = await get_memory_settings(db, uid=uid)

    assert result["configured"] is True
    assert result["active"] == {
        "channel_id": source["channel_id"],
        "model_id": source["model_id"],
        "dimensions": source["dimensions"],
        "signature": source["signature"],
        "revision": source["revision"],
        "collection": source["collection"],
    }
    assert result["target"] == {
        "channel_id": target["channel_id"],
        "model_id": target["model_id"],
        "dimensions": target["dimensions"],
        "signature": target["signature"],
        "revision": target["revision"],
        "collection": target["collection"],
    }
    assert result["migration"]["job_id"] == job.id
    assert result["migration"]["status"] == LongTermMemoryMigrationStatus.BUILDING.value
    assert result["migration"]["snapshot_boundary"] == 20
    assert result["migration"]["cursor"] == 4
    assert result["migration"]["total_count"] == 20
    assert result["migration"]["success_count"] == 3
    assert result["migration"]["failure_count"] == 1
    assert result["delta"] == {"high_watermark": 8, "applied_watermark": 6}
    assert result["index"] == {"revision": 7, "status": LongTermMemoryIndexStatus.READY.value}
    assert result["capacity"] == {"max_active_records": 9, "active_record_count": 1}
    assert result["old_collection_cleanup"]["name"] == "stage7-settings-old"
    assert result["old_collection_cleanup"]["status"] == LongTermMemoryOldCollectionCleanupStatus.FAILED.value
    assert result["old_collection_cleanup"]["job_id"] == 77
    assert result["old_collection_cleanup"]["error"] == "cleanup failed"
    assert result["migration_job"]["id"] == job.id


@pytest.mark.asyncio
async def test_migration_list_and_detail_isolate_uid(memory_session_factory) -> None:
    owner = "stage7-migration-owner"
    other = "stage7-migration-other"
    owner_source, owner_target = _migration_configs("stage7-owner")
    other_source, other_target = _migration_configs("stage7-other")
    await configure_store(
        memory_session_factory,
        uid=owner,
        channel_id=owner_source["channel_id"],
        model_id=owner_source["model_id"],
        dimensions=owner_source["dimensions"],
        signature=owner_source["signature"],
        collection_name=owner_source["collection"],
    )
    await configure_store(
        memory_session_factory,
        uid=other,
        channel_id=other_source["channel_id"],
        model_id=other_source["model_id"],
        dimensions=other_source["dimensions"],
        signature=other_source["signature"],
        collection_name=other_source["collection"],
    )
    owner_job = await _create_migration_job(
        memory_session_factory,
        uid=owner,
        source=owner_source,
        target=owner_target,
        dedupe_key="stage7-owner-migration",
    )
    await _create_migration_job(
        memory_session_factory,
        uid=other,
        source=other_source,
        target=other_target,
        dedupe_key="stage7-other-migration",
    )
    assert owner_job.id is not None

    async with memory_session_factory() as db:
        result = await list_embedding_migrations(db, uid=owner, skip=0, limit=10)
        with pytest.raises(MemoryNotFoundError) as exc_info:
            await get_embedding_migration(db, uid=other, migration_id=owner_job.id)

    assert result["total"] == 1
    assert result["items"][0]["job_id"] == owner_job.id
    assert result["items"][0]["from"]["model_id"] == owner_source["model_id"]
    assert exc_info.value.message == ERR_MEMORY_MIGRATION_NOT_FOUND


@pytest.mark.asyncio
async def test_failed_migration_retry_creates_new_job_revision_target_and_preserves_active(
    memory_session_factory,
    vector_backend: Stage5VectorBackend,
) -> None:
    uid = "stage7-migration-retry-owner"
    source, target = _migration_configs("stage7-retry")
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=6,
        collection_name=source["collection"],
    )
    old_job = await _create_migration_job(
        memory_session_factory,
        uid=uid,
        source=source,
        target=target,
        dedupe_key="stage7-migration-retry-original",
    )
    await _fail_migration(memory_session_factory, old_job)
    assert old_job.id is not None

    async with memory_session_factory() as db:
        retried = await retry_embedding_migration(db, uid=uid, migration_id=old_job.id)

    assert retried["created"] is True
    new_job_id = retried["job"]["id"]
    assert new_job_id != old_job.id
    async with memory_session_factory() as db:
        old_finished = await memory_job_crud.get_by_id(db, uid=uid, job_id=old_job.id)
        new_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=new_job_id)
        old_revision = await memory_embedding_revision_crud.get_by_job_id(db, uid=uid, job_id=old_job.id)
        new_revision = await memory_embedding_revision_crud.get_by_job_id(db, uid=uid, job_id=new_job_id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert old_finished is not None
    assert old_finished.status == LongTermMemoryMutationStatus.FAILED
    assert new_job is not None
    assert new_job.status == LongTermMemoryMutationStatus.PENDING
    assert old_revision is not None
    assert old_revision.status == LongTermMemoryEmbeddingRevisionStatus.FAILED
    assert new_revision is not None
    assert new_revision.revision == old_revision.revision + 1
    assert new_revision.job_id == new_job_id
    assert new_revision.status == LongTermMemoryEmbeddingRevisionStatus.CONFIRMED
    assert new_job.payload["target"]["revision"] == new_revision.revision
    assert new_job.payload["target"]["collection"] != target["collection"]
    assert store is not None
    assert store.active_embedding_channel_id == source["channel_id"]
    assert store.active_embedding_model_id == source["model_id"]
    assert store.active_embedding_dimensions == source["dimensions"]
    assert store.active_embedding_signature == source["signature"]
    assert store.active_embedding_revision == source["revision"]
    assert store.active_collection_name == source["collection"]
    assert store.migration_job_id == new_job_id
    assert store.migration_status == LongTermMemoryMigrationStatus.PREPARING
    assert store.index_revision == 6


@pytest.mark.asyncio
async def test_switching_migration_cancellation_is_rejected(
    memory_session_factory,
    vector_backend: Stage5VectorBackend,
) -> None:
    uid = "stage7-migration-switching"
    source, target = _migration_configs("stage7-switching")
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        collection_name=source["collection"],
    )
    job = await _create_migration_job(
        memory_session_factory,
        uid=uid,
        source=source,
        target=target,
        dedupe_key="stage7-switching-migration",
    )
    assert job.id is not None
    async with memory_session_factory() as db:
        updated = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            migration_status=LongTermMemoryMigrationStatus.SWITCHING,
        )
        assert updated is not None
        result = await cancel_embedding_migration(db, uid=uid, migration_id=job.id)
        persisted = await memory_job_crud.get_by_id(db, uid=uid, job_id=job.id)

    assert result["accepted"] is False
    assert result["changed"] is False
    assert result["error"] is not None
    assert persisted is not None
    assert persisted.status == LongTermMemoryMutationStatus.PENDING
