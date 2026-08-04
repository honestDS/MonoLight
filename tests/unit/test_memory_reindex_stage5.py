from typing import Any
from unittest.mock import patch

import chromadb
import pytest
from sqlalchemy import update

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
    from app.core.crud.memory import memory_store_crud
    from app.core.crud.memory_job import memory_job_crud
    from app.core.memory import submit_memory_reindex
    from app.core.memory_jobs import reindex_handler
    from app.core.memory_jobs.executor import MemoryJobExecutor, MemoryJobLeaseLostError
    from app.models.memory import (
        LongTermMemoryIndexStatus,
        LongTermMemoryMutationJob,
        LongTermMemoryMutationOperation,
        LongTermMemoryMutationStatus,
        LongTermMemoryOldCollectionCleanupStatus,
    )


pytest_plugins = ("tests.unit.memory_stage5_fixture",)


@pytest.mark.asyncio
async def test_submit_memory_reindex_creates_pending_job_and_is_idempotent(
    memory_session_factory,
) -> None:
    uid = "stage5-submit-user"
    old_collection = "stage5-submit-old"
    old_channel_id = 3
    old_model_id = "stage5-submit-model"
    old_dimensions = 4
    old_signature = "stage5-submit-signature"
    old_embedding_revision = 2
    old_index_revision = 6
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=old_channel_id,
        model_id=old_model_id,
        dimensions=old_dimensions,
        signature=old_signature,
        active_revision=old_embedding_revision,
        index_revision=old_index_revision,
        collection_name=old_collection,
    )

    async with memory_session_factory() as db:
        first = await submit_memory_reindex(
            db,
            uid=uid,
            dedupe_key="stage5-submit-dedupe",
        )

    assert first.created
    assert first.job.id is not None
    assert first.job.operation == LongTermMemoryMutationOperation.REINDEX
    assert first.job.status == LongTermMemoryMutationStatus.PENDING
    assert first.job.payload["target"]["index_revision"] == old_index_revision + 1
    assert first.job.payload["progress"]["snapshot_initialized"] is False

    async with memory_session_factory() as db:
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert store is not None
    assert store.index_status == LongTermMemoryIndexStatus.REINDEXING
    assert store.active_embedding_channel_id == old_channel_id
    assert store.active_embedding_model_id == old_model_id
    assert store.active_embedding_dimensions == old_dimensions
    assert store.active_embedding_signature == old_signature
    assert store.active_embedding_revision == old_embedding_revision
    assert store.active_collection_name == old_collection
    assert store.index_revision == old_index_revision

    async with memory_session_factory() as db:
        second = await submit_memory_reindex(
            db,
            uid=uid,
            dedupe_key="stage5-submit-dedupe",
        )

    assert not second.created
    assert second.job.id == first.job.id


@pytest.mark.asyncio
async def test_memory_reindex_builds_switches_and_cleans_old_collection(
    memory_session_factory,
    vector_backend: Stage5VectorBackend,
) -> None:
    uid = "stage5-complete-user"
    old_collection = "stage5-complete-old"
    channel_id = 4
    model_id = "stage5-complete-model"
    dimensions = 3
    signature = "stage5-complete-signature"
    embedding_revision = 5
    index_revision = 8
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=channel_id,
        model_id=model_id,
        dimensions=dimensions,
        signature=signature,
        active_revision=embedding_revision,
        index_revision=index_revision,
        collection_name=old_collection,
    )
    first_record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="first",
        version=2,
    )
    second_record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="second",
        version=4,
    )
    vector_backend.runtime_configs[(channel_id, model_id)] = runtime_config(
        channel_id=channel_id,
        model_id=model_id,
        dimensions=dimensions,
    )
    await vector_backend.get_or_create_collection(old_collection, metadata={"legacy": True})

    async with memory_session_factory() as db:
        submission = await submit_memory_reindex(
            db,
            uid=uid,
            dedupe_key="stage5-complete-dedupe",
        )
    assert submission.job.id is not None
    target_collection = submission.job.payload["target"]["collection"]

    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        job_id=submission.job.id,
    )
    assert claimed is not None
    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.REINDEX: reindex_handler.handle_reindex},
        session_factory=memory_session_factory,
    )
    result = await executor.execute_claimed(claimed, "stage5-worker")

    assert result.finalized
    assert result.result["finalized"] is True
    async with memory_session_factory() as db:
        finished_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=submission.job.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert finished_job is not None
    assert finished_job.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert store is not None
    assert store.active_embedding_channel_id == channel_id
    assert store.active_embedding_model_id == model_id
    assert store.active_embedding_dimensions == dimensions
    assert store.active_embedding_signature == signature
    assert store.active_embedding_revision == embedding_revision
    assert store.active_collection_name == target_collection
    assert store.index_revision == index_revision + 1
    assert store.index_status == LongTermMemoryIndexStatus.READY
    assert store.old_collection_name == old_collection
    assert store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.SUCCEEDED
    assert old_collection not in vector_backend.collections

    target = vector_backend.collections.get(target_collection)
    assert target is not None
    items = target["items"]
    assert len(items) == 2
    expected_records = []
    expected_records.append(first_record)
    expected_records.append(second_record)
    checked_count = 0
    for record in expected_records:
        assert record.vector_item_id is not None
        item = items.get(record.vector_item_id)
        assert item is not None
        metadata = item["metadata"]
        assert metadata["uid"] == uid
        assert metadata["embedding_revision"] == embedding_revision
        assert metadata["version"] == record.version
        checked_count += 1
    assert checked_count == 2


@pytest.mark.asyncio
async def test_memory_reindex_does_not_persist_progress_after_lease_loss(
    memory_session_factory,
    vector_backend: Stage5VectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "stage5-lease-user"
    old_collection = "stage5-lease-old"
    target_collection = "stage5-lease-target"
    channel_id = 5
    model_id = "stage5-lease-model"
    dimensions = 3
    signature = "stage5-lease-signature"
    embedding_revision = 3
    index_revision = 9
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=channel_id,
        model_id=model_id,
        dimensions=dimensions,
        signature=signature,
        active_revision=embedding_revision,
        index_revision=index_revision,
        collection_name=old_collection,
    )
    record = await create_recallable_record(
        memory_session_factory,
        uid=uid,
        memory_key="lease-record",
    )
    assert record.id is not None
    vector_backend.runtime_configs[(channel_id, model_id)] = runtime_config(
        channel_id=channel_id,
        model_id=model_id,
        dimensions=dimensions,
    )

    async with memory_session_factory() as db:
        store = await memory_store_crud.get_by_uid(db, uid=uid)
        assert store is not None
        await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            index_status=LongTermMemoryIndexStatus.REINDEXING,
        )

    payload: dict[str, Any] = {
        "from": {
            "channel_id": channel_id,
            "model_id": model_id,
            "dimensions": dimensions,
            "signature": signature,
            "embedding_revision": embedding_revision,
            "collection": old_collection,
            "index_revision": index_revision,
        },
        "target": {
            "collection": target_collection,
            "index_revision": index_revision + 1,
        },
        "progress": {
            "phase": "building",
            "snapshot_initialized": True,
            "snapshot_boundary": record.id,
            "cursor": 0,
            "total_count": 1,
            "success_count": 0,
            "failure_count": 0,
        },
    }
    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        dedupe_key="stage5-lease-dedupe",
        owner="stage5-lease-worker",
        payload=payload,
    )
    assert claimed is not None
    assert claimed.id is not None
    job_id = claimed.id
    original_upsert_records = reindex_handler.upsert_records

    async def upsert_then_lose_lease(
        context: Any,
        collection_name: str,
        records: list[Any],
        config: dict[str, Any],
    ) -> None:
        await original_upsert_records(context, collection_name, records, config)
        async with memory_session_factory() as db:
            await db.execute(
                update(LongTermMemoryMutationJob)
                .where(
                    LongTermMemoryMutationJob.uid == uid,
                    LongTermMemoryMutationJob.id == job_id,
                )
                .values(
                    status=LongTermMemoryMutationStatus.RETRY,
                    locked_by=None,
                    lock_until=None,
                )
            )
            await db.commit()

    monkeypatch.setattr(reindex_handler, "upsert_records", upsert_then_lose_lease)
    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.REINDEX: reindex_handler.handle_reindex},
        session_factory=memory_session_factory,
    )
    with pytest.raises(MemoryJobLeaseLostError):
        await executor.execute_claimed(claimed, "stage5-lease-worker")

    async with memory_session_factory() as db:
        job = await memory_job_crud.get_by_id(db, uid=uid, job_id=job_id)
    assert job is not None
    assert job.status == LongTermMemoryMutationStatus.RETRY
    assert job.payload["progress"]["cursor"] == 0
    assert job.payload["progress"]["success_count"] == 0
