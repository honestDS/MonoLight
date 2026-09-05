from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import chromadb
import pytest
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from tests.unit.memory_stage5_test_support import (
    Stage5VectorBackend,
    claim_job,
    configure_store,
    create_recallable_record,
    runtime_config,
)


class _ImportSafePersistentClient:
    def __init__(self, **_kwargs: Any) -> None:
        pass


with patch.object(chromadb, "PersistentClient", _ImportSafePersistentClient):
    from app.core.crud.memory.job import memory_job_crud
    from app.core.crud.memory.maintenance import memory_maintenance_store_crud
    from app.core.crud.memory.store import (
        memory_embedding_delta_crud,
        memory_embedding_revision_crud,
        memory_record_crud,
        memory_store_crud,
    )
    from app.core.memory_jobs import maintenance_vector, migration_handler
    from app.core.memory_jobs.executor import MemoryJobExecutionContext, MemoryJobExecutor, MemoryJobLeaseLostError, MemoryJobRetryableError
    from app.core.memory_jobs.maintenance_lifecycle import finalize_maintenance_terminal_state
    from app.core.memory_jobs.maintenance_state import ValidationSnapshot, record_snapshot
    from app.core.memory_jobs.manager import memory_job_manager
    from app.core.memory_jobs.migration_handler import handle_embedding_migration
    from app.models.memory import (
        LongTermMemoryEmbeddingDeltaAction,
        LongTermMemoryEmbeddingDeltaStatus,
        LongTermMemoryEmbeddingRevisionStatus,
        LongTermMemoryMigrationStatus,
        LongTermMemoryMutationJob,
        LongTermMemoryMutationOperation,
        LongTermMemoryMutationStatus,
        LongTermMemoryOldCollectionCleanupStatus,
        LongTermMemoryRecord,
    )
    from app.models.profile import Profile, ProfileConfig
    from app.providers.database.time import get_database_time


pytest_plugins = ("tests.unit.memory_stage5_fixture",)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "action",
    [
        LongTermMemoryEmbeddingDeltaAction.DELETE,
        LongTermMemoryEmbeddingDeltaAction.SUPPRESS,
    ],
)
async def test_migration_removal_delta_does_not_require_current_record(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: Stage5VectorBackend,
    action: LongTermMemoryEmbeddingDeltaAction,
) -> None:
    uid = f"stage5-removal-delta-{action.value}"
    collection_name = f"stage5-removal-target-{action.value}"
    record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key=f"removal-{action.value}",
        content=f"removal content {action.value}",
        vector_item_id=f"removal-vector-{action.value}",
    )
    assert record.id is not None
    await vector_backend.get_or_create_collection(collection_name)
    await vector_backend.upsert(
        collection_name,
        [record.vector_item_id],
        [record.content],
        [[0.1, 0.2, 0.3]],
        [{"memory_id": record.id, "version": record.version}],
    )
    async with memory_session_factory() as db:
        deleted = await memory_record_crud.delete(
            db,
            uid=uid,
            memory_id=record.id,
        )
    assert deleted is not None

    context = SimpleNamespace(
        session_factory=memory_session_factory,
        job=SimpleNamespace(uid=uid),
    )
    delta = SimpleNamespace(memory_id=record.id, action=action)
    await migration_handler._apply_migration_delta(
        context,
        delta,
        {"collection": collection_name},
    )

    assert vector_backend.collections[collection_name]["items"] == {}


async def _prepare_embedding_migration(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    dedupe_key: str,
    source: dict[str, Any],
    target: dict[str, Any],
) -> Any:
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
        await memory_embedding_revision_crud.create(
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
        await db.commit()
        return job


async def _save_revision_history(
    session_factory: async_sessionmaker[AsyncSession],
    *,
    uid: str,
    source: dict[str, Any],
    failed_target: dict[str, Any],
) -> None:
    async with session_factory() as db:
        now = await get_database_time(db)
        await memory_embedding_revision_crud.create(
            db,
            uid=uid,
            revision=1,
            to_channel_id=source["channel_id"],
            to_model_id=source["model_id"],
            to_dimensions=source["dimensions"],
            to_signature=source["signature"],
            to_collection=source["collection"],
            status=LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED,
            confirmed_at=now,
            finished_at=now,
            commit=False,
        )
        await memory_embedding_revision_crud.create(
            db,
            uid=uid,
            revision=2,
            from_channel_id=source["channel_id"],
            from_model_id=source["model_id"],
            from_dimensions=source["dimensions"],
            from_signature=source["signature"],
            from_collection=source["collection"],
            to_channel_id=failed_target["channel_id"],
            to_model_id=failed_target["model_id"],
            to_dimensions=failed_target["dimensions"],
            to_signature=failed_target["signature"],
            to_collection=failed_target["collection"],
            status=LongTermMemoryEmbeddingRevisionStatus.FAILED,
            error="previous migration failed",
            finished_at=now,
            commit=False,
        )
        await db.commit()


@pytest.mark.asyncio
async def test_embedding_migration_switches_and_applies_snapshot_delta(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: Stage5VectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "stage5-migration-delta-user"
    old_collection = "stage5-migration-delta-old"
    target_collection = "stage5-migration-delta-target"
    source = {
        "channel_id": 1,
        "model_id": "memory-model-v1",
        "dimensions": 3,
        "signature": "stage5-source-signature",
        "collection": old_collection,
        "revision": 1,
    }
    failed_target = {
        "channel_id": 8,
        "model_id": "memory-model-failed",
        "dimensions": 4,
        "signature": "stage5-failed-signature",
        "collection": "stage5-migration-failed-target",
        "revision": 2,
    }
    target = {
        "channel_id": 9,
        "model_id": "memory-model-v3",
        "dimensions": 4,
        "signature": "stage5-target-signature",
        "collection": target_collection,
        "revision": 3,
    }
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=4,
        collection_name=old_collection,
    )
    await _save_revision_history(
        memory_session_factory,
        uid=uid,
        source=source,
        failed_target=failed_target,
    )
    record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="delta-record",
        content="content-v1",
        version=1,
        vector_item_id="vector-v1",
        content_hash="hash-v1",
    )
    assert record.id is not None
    await vector_backend.get_or_create_collection(old_collection, metadata={"legacy": True})
    vector_backend.runtime_configs[(target["channel_id"], target["model_id"])] = runtime_config(
        channel_id=target["channel_id"],
        model_id=target["model_id"],
        dimensions=target["dimensions"],
    )
    profile_a = Profile(
        uid=uid,
        name="stage5-migration-profile-a",
        configs=ProfileConfig.model_validate(
            {
                "memory": {
                    "enabled": True,
                    "embedding_channel_id": source["channel_id"],
                    "embedding_model_id": source["model_id"],
                    "top_k": 3,
                    "candidate_k": 7,
                    "result_max_chars": 2000,
                }
            }
        ).model_dump(),
    )
    profile_b = Profile(
        uid=uid,
        name="stage5-migration-profile-b",
        configs=ProfileConfig.model_validate(
            {
                "memory": {
                    "enabled": False,
                    "embedding_channel_id": source["channel_id"],
                    "embedding_model_id": source["model_id"],
                    "top_k": 8,
                    "candidate_k": 12,
                    "result_max_chars": 4000,
                }
            }
        ).model_dump(),
    )
    async with memory_session_factory() as db:
        db.add(profile_a)
        db.add(profile_b)
        await db.commit()
    job = await _prepare_embedding_migration(
        memory_session_factory,
        uid=uid,
        dedupe_key="stage5-migration-delta",
        source=source,
        target=target,
    )
    assert job.id is not None

    original_embed = maintenance_vector.embed_texts_with_config
    updated_record = False

    async def embed_with_delta(
        config: Any,
        texts: Any,
        dimensions: int | None = None,
        **kwargs: Any,
    ) -> list[list[float]]:
        nonlocal updated_record
        if not updated_record and config.model_id == target["model_id"]:
            updated_record = True
            async with memory_session_factory() as db:
                result = await db.execute(
                    update(LongTermMemoryRecord)
                    .where(LongTermMemoryRecord.uid == uid, LongTermMemoryRecord.id == record.id)
                    .values(
                        content="content-v2",
                        content_hash="hash-v2",
                        version=2,
                        indexed_version=2,
                        vector_item_id="vector-v2",
                    )
                )
                assert result.rowcount == 1
                sequence = await memory_store_crud.reserve_migration_delta_sequence(
                    db,
                    uid=uid,
                    migration_job_id=job.id,
                    expected_high_watermark=0,
                    commit=False,
                )
                assert sequence == 1
                await memory_embedding_delta_crud.create(
                    db,
                    uid=uid,
                    migration_job_id=job.id,
                    sequence=sequence,
                    memory_id=record.id,
                    memory_version=2,
                    action=LongTermMemoryEmbeddingDeltaAction.UPSERT,
                    snapshot={
                        "content": "content-v2",
                        "content_hash": "hash-v2",
                        "version": 2,
                        "indexed_version": 2,
                        "vector_item_id": "vector-v2",
                    },
                    status=LongTermMemoryEmbeddingDeltaStatus.PENDING,
                    commit=False,
                )
                await db.commit()
        return await original_embed(config, texts, dimensions=dimensions, **kwargs)

    monkeypatch.setattr(maintenance_vector, "embed_texts_with_config", embed_with_delta)
    managed_follow_calls: list[dict[str, Any]] = []

    async def record_managed_follow(_db: AsyncSession, **kwargs: Any) -> list[Any]:
        managed_follow_calls.append(kwargs)
        return []

    monkeypatch.setattr(
        migration_handler,
        "submit_managed_knowledge_base_migrations_for_memory_revision",
        record_managed_follow,
    )
    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        job_id=job.id,
        owner="stage5-migration-delta-worker",
    )
    assert claimed is not None
    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.EMBEDDING_MIGRATION: handle_embedding_migration},
        session_factory=memory_session_factory,
    )
    result = await executor.execute_claimed(claimed, "stage5-migration-delta-worker")

    assert result.finalized
    assert result.result["finalized"] is True
    assert managed_follow_calls == [
        {
            "uid": uid,
            "target_channel_id": target["channel_id"],
            "target_model_id": target["model_id"],
            "target_dimensions": target["dimensions"],
            "target_signature": target["signature"],
            "memory_revision": target["revision"],
            "commit": False,
        }
    ]
    async with memory_session_factory() as db:
        finished_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
        current_record = await memory_record_crud.get_by_id(db, uid=uid, memory_id=record.id)
        revision_one = await memory_embedding_revision_crud.get_by_revision(db, uid=uid, revision=1)
        revision_two = await memory_embedding_revision_crud.get_by_revision(db, uid=uid, revision=2)
        revision_three = await memory_embedding_revision_crud.get_by_revision(db, uid=uid, revision=3)
        deltas = await memory_embedding_delta_crud.list_by_migration_job(
            db,
            uid=uid,
            migration_job_id=job.id,
        )
        profiles = list((await db.execute(select(Profile).where(Profile.uid == uid).order_by(Profile.id))).scalars().all())
    assert finished_job is not None
    assert finished_job.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert store is not None
    assert store.active_embedding_channel_id == target["channel_id"]
    assert store.active_embedding_model_id == target["model_id"]
    assert store.active_embedding_dimensions == target["dimensions"]
    assert store.active_embedding_signature == target["signature"]
    assert store.active_embedding_revision == target["revision"]
    assert store.active_collection_name == target_collection
    assert store.index_revision == 5
    assert store.migration_status == LongTermMemoryMigrationStatus.SUCCEEDED
    assert store.target_embedding_channel_id is None
    assert store.target_embedding_model_id is None
    assert store.target_embedding_dimensions is None
    assert store.target_embedding_signature is None
    assert store.target_collection_name is None
    assert store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.SUCCEEDED
    assert store.old_collection_name == old_collection
    assert old_collection not in vector_backend.collections
    assert revision_one is not None
    assert revision_one.status == LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED
    assert revision_two is not None
    assert revision_two.status == LongTermMemoryEmbeddingRevisionStatus.FAILED
    assert revision_three is not None
    assert revision_three.status == LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED
    assert store.migration_delta_high_watermark == 1
    assert store.migration_delta_applied_watermark == 1
    assert len(deltas) == 1
    assert deltas[0].sequence == 1
    assert deltas[0].status == LongTermMemoryEmbeddingDeltaStatus.APPLIED
    assert current_record is not None
    assert current_record.content == "content-v2"
    assert current_record.version == 2
    assert current_record.indexed_version == 2
    assert current_record.vector_item_id == "vector-v2"
    assert len(profiles) == 2
    profile_a_memory = ProfileConfig.model_validate(profiles[0].configs).memory
    profile_b_memory = ProfileConfig.model_validate(profiles[1].configs).memory
    assert profile_a_memory.embedding_channel_id == target["channel_id"]
    assert profile_a_memory.embedding_model_id == target["model_id"]
    assert profile_a_memory.enabled is True
    assert profile_a_memory.top_k == 3
    assert profile_a_memory.candidate_k == 7
    assert profile_a_memory.result_max_chars == 2000
    assert profile_b_memory.embedding_channel_id == target["channel_id"]
    assert profile_b_memory.embedding_model_id == target["model_id"]
    assert profile_b_memory.enabled is False
    assert profile_b_memory.top_k == 8
    assert profile_b_memory.candidate_k == 12
    assert profile_b_memory.result_max_chars == 4000

    target_items = vector_backend.collections[target_collection]["items"]
    item_ids: list[str] = []
    for item_id in target_items:
        item_ids.append(item_id)
    assert len(item_ids) == 1
    assert item_ids[0] == "vector-v2"
    target_item = target_items["vector-v2"]
    assert target_item["document"] == "content-v2"
    assert target_item["metadata"]["version"] == 2
    assert target_item["metadata"]["embedding_revision"] == 3
    target_upserts: list[dict[str, Any]] = []
    for call in vector_backend.upsert_calls:
        if call["collection_name"] == target_collection:
            target_upserts.append(call)
    assert len(target_upserts) >= 2
    assert target_upserts[0]["ids"][0] == "vector-v1"
    assert target_upserts[0]["documents"][0] == "content-v1"
    assert target_upserts[0]["metadatas"][0]["version"] == 1
    assert target_upserts[1]["ids"][0] == "vector-v2"
    assert target_upserts[1]["documents"][0] == "content-v2"
    assert target_upserts[1]["metadatas"][0]["version"] == 2


@pytest.mark.asyncio
async def test_embedding_migration_sample_query_failure_does_not_switch(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: Stage5VectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "stage5-migration-validation-user"
    old_collection = "stage5-migration-validation-old"
    target_collection = "stage5-migration-validation-target"
    source = {
        "channel_id": 3,
        "model_id": "memory-model-old",
        "dimensions": 3,
        "signature": "stage5-validation-source",
        "collection": old_collection,
        "revision": 1,
    }
    target = {
        "channel_id": 4,
        "model_id": "memory-model-new",
        "dimensions": 4,
        "signature": "stage5-validation-target",
        "collection": target_collection,
        "revision": 2,
    }
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=7,
        collection_name=old_collection,
    )
    await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="validation-record",
        content="validation-content",
        vector_item_id="validation-vector-v1",
    )
    async with memory_session_factory() as db:
        now = await get_database_time(db)
        await memory_embedding_revision_crud.create(
            db,
            uid=uid,
            revision=1,
            to_channel_id=source["channel_id"],
            to_model_id=source["model_id"],
            to_dimensions=source["dimensions"],
            to_signature=source["signature"],
            to_collection=source["collection"],
            status=LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED,
            confirmed_at=now,
            finished_at=now,
        )
    await vector_backend.get_or_create_collection(old_collection, metadata={"legacy": True})
    vector_backend.runtime_configs[(target["channel_id"], target["model_id"])] = runtime_config(
        channel_id=target["channel_id"],
        model_id=target["model_id"],
        dimensions=target["dimensions"],
    )
    job = await _prepare_embedding_migration(
        memory_session_factory,
        uid=uid,
        dedupe_key="stage5-migration-validation",
        source=source,
        target=target,
    )
    assert job.id is not None

    async def empty_sample_query(*_args: Any, **_kwargs: Any) -> list[Any]:
        return []

    monkeypatch.setattr(maintenance_vector, "hybrid_query_collection", empty_sample_query)
    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        job_id=job.id,
        owner="stage5-migration-validation-worker",
    )
    assert claimed is not None
    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.EMBEDDING_MIGRATION: handle_embedding_migration},
        session_factory=memory_session_factory,
    )
    with pytest.raises(MemoryJobRetryableError):
        await executor.execute_claimed(claimed, "stage5-migration-validation-worker")

    async with memory_session_factory() as db:
        store = await memory_store_crud.get_by_uid(db, uid=uid)
        revision = await memory_embedding_revision_crud.get_by_revision(db, uid=uid, revision=2)
    assert store is not None
    assert store.active_embedding_channel_id == source["channel_id"]
    assert store.active_embedding_model_id == source["model_id"]
    assert store.active_embedding_dimensions == source["dimensions"]
    assert store.active_embedding_signature == source["signature"]
    assert store.active_embedding_revision == source["revision"]
    assert store.active_collection_name == old_collection
    assert store.index_revision == 7
    assert store.migration_status == LongTermMemoryMigrationStatus.VALIDATING
    assert store.active_collection_name != target_collection
    assert target_collection in vector_backend.collections
    assert revision is not None
    assert revision.status == LongTermMemoryEmbeddingRevisionStatus.RUNNING


@pytest.mark.asyncio
async def test_embedding_migration_rejects_corrupted_non_sampled_target_vector(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: Stage5VectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "stage5-migration-vector-validation-user"
    old_collection = "stage5-migration-vector-validation-old"
    target_collection = "stage5-migration-vector-validation-target"
    source = {
        "channel_id": 5,
        "model_id": "memory-model-vector-source",
        "dimensions": 3,
        "signature": "stage5-vector-validation-source",
        "collection": old_collection,
        "revision": 1,
    }
    target = {
        "channel_id": 6,
        "model_id": "memory-model-vector-target",
        "dimensions": 4,
        "signature": "stage5-vector-validation-target",
        "collection": target_collection,
        "revision": 2,
    }
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=9,
        collection_name=old_collection,
    )
    first_record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="vector-validation-first",
        content="vector-validation-content-first",
        content_hash="vector-hash-first",
        vector_item_id="vector-validation-first",
    )
    second_record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="vector-validation-second",
        content="vector-validation-content-second",
        content_hash="vector-hash-second",
        vector_item_id="vector-validation-second",
    )
    assert first_record.id is not None
    assert second_record.id is not None
    async with memory_session_factory() as db:
        now = await get_database_time(db)
        await memory_embedding_revision_crud.create(
            db,
            uid=uid,
            revision=1,
            to_channel_id=source["channel_id"],
            to_model_id=source["model_id"],
            to_dimensions=source["dimensions"],
            to_signature=source["signature"],
            to_collection=source["collection"],
            status=LongTermMemoryEmbeddingRevisionStatus.SUCCEEDED,
            confirmed_at=now,
            finished_at=now,
        )
    await vector_backend.get_or_create_collection(old_collection, metadata={"legacy": True})
    vector_backend.runtime_configs[(target["channel_id"], target["model_id"])] = runtime_config(
        channel_id=target["channel_id"],
        model_id=target["model_id"],
        dimensions=target["dimensions"],
    )
    job = await _prepare_embedding_migration(
        memory_session_factory,
        uid=uid,
        dedupe_key="stage5-migration-vector-validation",
        source=source,
        target=target,
    )
    assert job.id is not None

    original_reconcile_collection = migration_handler.reconcile_collection
    corrupted_item_id: str | None = None
    corrupted_document: str | None = None
    corrupted_metadata: dict[str, Any] | None = None

    async def reconcile_with_corrupted_vector(
        context: Any,
        *,
        records: list[Any],
        config: dict[str, Any],
        purpose: str,
    ) -> Any:
        nonlocal corrupted_item_id, corrupted_document, corrupted_metadata
        if config["collection"] == target_collection:
            target_items = vector_backend.collections[target_collection]["items"]
            item_ids = list(target_items)
            assert len(item_ids) == 2
            corrupted_item_id = item_ids[1]
            corrupted_item = target_items[corrupted_item_id]
            expected_record = next(record for record in records if record.vector_item_id == corrupted_item_id)
            assert corrupted_item["document"] == expected_record.content
            corrupted_document = corrupted_item["document"]
            corrupted_metadata = dict(corrupted_item["metadata"])
            corrupted_item["embedding"] = [9.0] * target["dimensions"]
        return await original_reconcile_collection(
            context,
            records=records,
            config=config,
            purpose=purpose,
        )

    monkeypatch.setattr(migration_handler, "reconcile_collection", reconcile_with_corrupted_vector)
    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        job_id=job.id,
        owner="stage5-migration-vector-validation-worker",
    )
    assert claimed is not None
    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.EMBEDDING_MIGRATION: handle_embedding_migration},
        session_factory=memory_session_factory,
    )
    with pytest.raises(MemoryJobRetryableError):
        await executor.execute_claimed(claimed, "stage5-migration-vector-validation-worker")

    async with memory_session_factory() as db:
        store = await memory_store_crud.get_by_uid(db, uid=uid)
        revision = await memory_embedding_revision_crud.get_by_revision(db, uid=uid, revision=2)
    assert corrupted_item_id is not None
    assert corrupted_document is not None
    assert corrupted_metadata is not None
    target_item = vector_backend.collections[target_collection]["items"][corrupted_item_id]
    assert target_item["embedding"] == [9.0] * target["dimensions"]
    assert target_item["document"] == corrupted_document
    assert target_item["metadata"] == corrupted_metadata
    assert store is not None
    assert store.active_embedding_channel_id == source["channel_id"]
    assert store.active_embedding_model_id == source["model_id"]
    assert store.active_embedding_dimensions == source["dimensions"]
    assert store.active_embedding_signature == source["signature"]
    assert store.active_embedding_revision == source["revision"]
    assert store.active_collection_name == old_collection
    assert store.index_revision == 9
    assert store.migration_status == LongTermMemoryMigrationStatus.VALIDATING
    assert store.active_collection_name != target_collection
    assert target_collection in vector_backend.collections
    assert revision is not None
    assert revision.status == LongTermMemoryEmbeddingRevisionStatus.RUNNING


@pytest.mark.asyncio
async def test_embedding_migration_switch_is_fenced_after_expired_lease_recovery(
    memory_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "stage5-migration-switch-fence-user"
    old_collection = "stage5-migration-switch-fence-old"
    target_collection = "stage5-migration-switch-fence-target"
    owner = "stage5-migration-switch-fence-worker"
    source = {
        "channel_id": 11,
        "model_id": "stage5-migration-switch-fence-source",
        "dimensions": 3,
        "signature": "stage5-migration-switch-fence-source-signature",
        "collection": old_collection,
        "revision": 1,
    }
    target = {
        "channel_id": 12,
        "model_id": "stage5-migration-switch-fence-target",
        "dimensions": 4,
        "signature": "stage5-migration-switch-fence-target-signature",
        "collection": target_collection,
        "revision": 2,
    }
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=source["channel_id"],
        model_id=source["model_id"],
        dimensions=source["dimensions"],
        signature=source["signature"],
        active_revision=source["revision"],
        index_revision=13,
        collection_name=old_collection,
    )
    record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="switch-fence-record",
    )
    assert record.id is not None
    job = await _prepare_embedding_migration(
        memory_session_factory,
        uid=uid,
        dedupe_key="stage5-migration-switch-fence",
        source=source,
        target=target,
    )
    assert job.id is not None
    async with memory_session_factory() as db:
        now = await get_database_time(db)
        updated_store = await memory_maintenance_store_crud.update_embedding_migration_progress(
            db,
            uid=uid,
            migration_job_id=job.id,
            migration_status=LongTermMemoryMigrationStatus.VALIDATING,
            migration_snapshot_boundary=record.id,
            migration_total_count=1,
            migration_cursor=record.id,
            migration_success_count=1,
            migration_failure_count=0,
            migration_delta_high_watermark=0,
            migration_delta_applied_watermark=0,
            commit=False,
        )
        assert updated_store is not None
        updated_revision = await memory_embedding_revision_crud.update_by_revision(
            db,
            uid=uid,
            revision=target["revision"],
            status=LongTermMemoryEmbeddingRevisionStatus.RUNNING,
            started_at=now,
            commit=False,
        )
        assert updated_revision is not None
        await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == job.id,
            )
            .values(max_attempts=1)
        )
        await db.commit()

    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.EMBEDDING_MIGRATION,
        job_id=job.id,
        owner=owner,
    )
    assert claimed is not None
    async with memory_session_factory() as db:
        initial_claim = await memory_job_crud.get_active_claim(
            db,
            uid=uid,
            job_id=job.id,
            owner=owner,
        )
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert initial_claim is not None
    assert store is not None

    original_get_active_claim = memory_job_crud.get_active_claim
    initial_claim_check = False
    recovered = False

    async def get_claim_for_switch(_db: Any, **kwargs: Any) -> Any:
        nonlocal initial_claim_check
        if not initial_claim_check:
            initial_claim_check = True
            return initial_claim
        return await original_get_active_claim(_db, **kwargs)

    async def recover_expired_claim() -> None:
        nonlocal recovered
        if recovered:
            return
        recovered = True
        async with memory_session_factory() as recovery_db:
            recovery_now = await get_database_time(recovery_db)
            await recovery_db.execute(
                update(LongTermMemoryMutationJob)
                .where(
                    LongTermMemoryMutationJob.uid == uid,
                    LongTermMemoryMutationJob.id == job.id,
                )
                .values(lock_until=recovery_now - timedelta(seconds=1))
            )
            recovery = await memory_job_crud.recover_expired(
                recovery_db,
                max_attempts_error="stage5 lease expired",
                commit=False,
            )
            for terminal in recovery.terminal_jobs:
                await finalize_maintenance_terminal_state(
                    recovery_db,
                    job=terminal.job,
                    status=terminal.status,
                    error=terminal.error,
                )
            await recovery_db.commit()

    async def return_store_without_write(_db: Any, *, uid: str, commit: bool = True) -> Any:
        await recover_expired_claim()
        return store

    async def return_records_without_read(_db: Any, **_kwargs: Any) -> list[Any]:
        return [record]

    monkeypatch.setattr(memory_job_crud, "get_active_claim", get_claim_for_switch)
    monkeypatch.setattr(memory_store_crud, "lock_for_mutation", return_store_without_write)
    monkeypatch.setattr(migration_handler, "read_recallable_records", return_records_without_read)

    context = MemoryJobExecutionContext(
        job=claimed,
        worker_id=owner,
        session_factory=memory_session_factory,
    )
    validation = ValidationSnapshot(
        records=(record_snapshot(record, target["revision"]),),
        count=1,
        success_count=1,
    )
    with pytest.raises(MemoryJobLeaseLostError):
        await migration_handler._switch_migration(context, claimed.payload, validation)

    assert recovered
    async with memory_session_factory() as db:
        recovered_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job.id)
        current_store = await memory_store_crud.get_by_uid(db, uid=uid)
        recovered_revision = await memory_embedding_revision_crud.get_by_revision(
            db,
            uid=uid,
            revision=target["revision"],
        )
    assert recovered_job is not None
    assert recovered_job.status == LongTermMemoryMutationStatus.FAILED
    assert current_store is not None
    assert current_store.active_embedding_channel_id == source["channel_id"]
    assert current_store.active_embedding_model_id == source["model_id"]
    assert current_store.active_embedding_revision == source["revision"]
    assert current_store.active_collection_name == old_collection
    assert current_store.index_revision == 13
    assert current_store.migration_status == LongTermMemoryMigrationStatus.FAILED
    assert recovered_revision is not None
    assert recovered_revision.status == LongTermMemoryEmbeddingRevisionStatus.FAILED
