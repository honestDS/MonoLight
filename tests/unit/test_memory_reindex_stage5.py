from datetime import timedelta
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
    from app.core.constants import ERR_MEMORY_MAINTENANCE_STATE_CONFLICT
    from app.core.crud.memory import memory_store_crud
    from app.core.crud.memory_job import memory_job_crud
    from app.core.crud.memory_maintenance import memory_maintenance_job_crud
    from app.core.memory import submit_memory_reindex
    from app.core.memory.errors import MemoryConflictError
    from app.core.memory_jobs import reindex_handler
    from app.core.memory_jobs.executor import (
        MemoryJobExecutionContext,
        MemoryJobExecutor,
        MemoryJobLeaseLostError,
        MemoryJobRetryableError,
    )
    from app.core.memory_jobs.maintenance_lifecycle import finalize_maintenance_terminal_state
    from app.core.memory_jobs.maintenance_state import ValidationSnapshot, record_snapshot
    from app.models.memory import (
        LongTermMemoryIndexStatus,
        LongTermMemoryMutationJob,
        LongTermMemoryMutationOperation,
        LongTermMemoryMutationStatus,
        LongTermMemoryOldCollectionCleanupStatus,
        LongTermMemorySource,
    )
    from app.providers.database.time import get_database_time


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
@pytest.mark.parametrize(
    ("operation", "payload"),
    [
        (LongTermMemoryMutationOperation.ORGANIZE, {}),
        (LongTermMemoryMutationOperation.ORGANIZE_MERGE, {}),
        (
            LongTermMemoryMutationOperation.DELETE_CLEANUP,
            {"source": LongTermMemorySource.AUTO_ORGANIZE.value},
        ),
    ],
)
async def test_submit_memory_reindex_rejects_active_organization_chain(
    memory_session_factory,
    operation: LongTermMemoryMutationOperation,
    payload: dict[str, Any],
) -> None:
    uid = f"stage5-reindex-organization-block-{operation.value}"
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=31,
        model_id="stage5-organization-block-model",
        dimensions=3,
        signature="stage5-organization-block-signature",
        active_revision=2,
        index_revision=4,
        collection_name=f"stage5-organization-block-{operation.value}",
    )

    async with memory_session_factory() as db:
        active_job, created = await memory_job_crud.create(
            db,
            uid=uid,
            operation=operation,
            dedupe_key=f"stage5-active-organization-{operation.value}",
            payload=payload,
            commit=True,
        )
    assert created
    assert active_job.status == LongTermMemoryMutationStatus.PENDING

    async with memory_session_factory() as db:
        with pytest.raises(MemoryConflictError) as exc_info:
            await submit_memory_reindex(
                db,
                uid=uid,
                dedupe_key=f"stage5-blocked-reindex-{operation.value}",
            )
    assert exc_info.value.message == ERR_MEMORY_MAINTENANCE_STATE_CONFLICT

    async with memory_session_factory() as db:
        store = await memory_store_crud.get_by_uid(db, uid=uid)
        blocked_job = await memory_job_crud.get_by_dedupe_key(
            db,
            uid=uid,
            dedupe_key=f"stage5-blocked-reindex-{operation.value}",
        )
    assert store is not None
    assert store.index_status == LongTermMemoryIndexStatus.READY
    assert blocked_job is None


@pytest.mark.asyncio
async def test_submit_memory_reindex_does_not_block_on_unrelated_delete_cleanup(
    memory_session_factory,
) -> None:
    uid = "stage5-reindex-unrelated-delete-cleanup"
    await configure_store(
        memory_session_factory,
        uid=uid,
        channel_id=32,
        model_id="stage5-unrelated-cleanup-model",
        dimensions=3,
        signature="stage5-unrelated-cleanup-signature",
        active_revision=2,
        index_revision=4,
        collection_name="stage5-unrelated-cleanup",
    )

    async with memory_session_factory() as db:
        cleanup_job, created = await memory_job_crud.create(
            db,
            uid=uid,
            operation=LongTermMemoryMutationOperation.DELETE_CLEANUP,
            dedupe_key="stage5-unrelated-delete-cleanup-job",
            payload={"source": LongTermMemorySource.USER_API.value},
            commit=True,
        )
    assert created
    assert cleanup_job.status == LongTermMemoryMutationStatus.PENDING

    async with memory_session_factory() as db:
        submission = await submit_memory_reindex(
            db,
            uid=uid,
            dedupe_key="stage5-reindex-with-unrelated-cleanup",
        )
    assert submission.created
    assert submission.job.operation == LongTermMemoryMutationOperation.REINDEX


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
async def test_memory_reindex_rejects_corrupted_nonfirst_target_embedding(
    memory_session_factory,
    vector_backend: Stage5VectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "stage5-corrupted-nonfirst-vector-user"
    old_collection = "stage5-corrupted-nonfirst-vector-old"
    channel_id = 4
    model_id = "stage5-corrupted-nonfirst-vector-model"
    dimensions = 3
    signature = "stage5-corrupted-nonfirst-vector-signature"
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
    await create_recallable_record(
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
            dedupe_key="stage5-corrupted-nonfirst-vector-dedupe",
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

    original_reconcile_collection = reindex_handler.reconcile_collection

    async def corrupt_second_embedding(
        context: Any,
        *,
        records: list[Any],
        config: dict[str, Any],
        purpose: str,
    ) -> Any:
        assert len(records) >= 2
        target = vector_backend.collections.get(config["collection"])
        assert target is not None
        target_item = target["items"].get(second_record.vector_item_id)
        assert target_item is not None
        assert target_item["document"] == second_record.content
        assert target_item["metadata"]["uid"] == uid
        assert target_item["metadata"]["embedding_revision"] == embedding_revision
        assert target_item["metadata"]["version"] == second_record.version
        target_item["embedding"] = [999.0] * dimensions
        return await original_reconcile_collection(
            context,
            records=records,
            config=config,
            purpose=purpose,
        )

    monkeypatch.setattr(reindex_handler, "reconcile_collection", corrupt_second_embedding)
    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.REINDEX: reindex_handler.handle_reindex},
        session_factory=memory_session_factory,
    )
    with pytest.raises(MemoryJobRetryableError):
        await executor.execute_claimed(claimed, "stage5-worker")

    target = vector_backend.collections.get(target_collection)
    assert target is not None
    target_item = target["items"].get(second_record.vector_item_id)
    assert target_item is not None
    assert target_item["embedding"] == [999.0] * dimensions
    assert target_item["document"] == second_record.content
    assert target_item["metadata"]["uid"] == uid
    assert target_item["metadata"]["embedding_revision"] == embedding_revision
    assert target_item["metadata"]["version"] == second_record.version

    async with memory_session_factory() as db:
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert store is not None
    assert store.active_collection_name == old_collection
    assert store.index_revision == index_revision


@pytest.mark.asyncio
async def test_memory_reindex_accepts_small_target_embedding_difference(
    memory_session_factory,
    vector_backend: Stage5VectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "stage5-small-vector-difference-user"
    old_collection = "stage5-small-vector-difference-old"
    channel_id = 4
    model_id = "stage5-small-vector-difference-model"
    dimensions = 3
    signature = "stage5-small-vector-difference-signature"
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
    await create_recallable_record(
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
            dedupe_key="stage5-small-vector-difference-dedupe",
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

    original_reconcile_collection = reindex_handler.reconcile_collection
    perturbed = False

    async def perturb_target_embedding(
        context: Any,
        *,
        records: list[Any],
        config: dict[str, Any],
        purpose: str,
    ) -> Any:
        nonlocal perturbed
        assert len(records) >= 2
        target = vector_backend.collections.get(config["collection"])
        assert target is not None
        target_item = target["items"].get(second_record.vector_item_id)
        assert target_item is not None
        target_item["embedding"][0] += 1e-7
        perturbed = True
        return await original_reconcile_collection(
            context,
            records=records,
            config=config,
            purpose=purpose,
        )

    monkeypatch.setattr(reindex_handler, "reconcile_collection", perturb_target_embedding)
    executor = MemoryJobExecutor(
        {LongTermMemoryMutationOperation.REINDEX: reindex_handler.handle_reindex},
        session_factory=memory_session_factory,
    )
    result = await executor.execute_claimed(claimed, "stage5-worker")

    assert perturbed
    assert result.finalized
    assert result.result["finalized"] is True
    async with memory_session_factory() as db:
        finished_job = await memory_job_crud.get_by_id(db, uid=uid, job_id=submission.job.id)
        store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert finished_job is not None
    assert finished_job.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert store is not None
    assert store.active_collection_name == target_collection
    assert store.index_revision == index_revision + 1
    assert store.index_status == LongTermMemoryIndexStatus.READY
    assert store.old_collection_name == old_collection
    assert store.old_collection_cleanup_status == LongTermMemoryOldCollectionCleanupStatus.SUCCEEDED
    assert old_collection not in vector_backend.collections


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


@pytest.mark.asyncio
async def test_memory_reindex_switch_is_fenced_after_expired_lease_recovery(
    memory_session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "stage5-reindex-switch-fence-user"
    old_collection = "stage5-reindex-switch-fence-old"
    channel_id = 6
    model_id = "stage5-reindex-switch-fence-model"
    dimensions = 3
    signature = "stage5-reindex-switch-fence-signature"
    embedding_revision = 4
    index_revision = 10
    owner = "stage5-reindex-switch-fence-worker"
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
        memory_key="switch-fence-record",
    )
    assert record.id is not None

    async with memory_session_factory() as db:
        submission = await submit_memory_reindex(
            db,
            uid=uid,
            dedupe_key="stage5-reindex-switch-fence",
        )
        assert submission.job.id is not None
        await db.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == submission.job.id,
            )
            .values(max_attempts=1)
        )
        await db.commit()

    claimed = await claim_job(
        memory_session_factory,
        uid=uid,
        operation=LongTermMemoryMutationOperation.REINDEX,
        job_id=submission.job.id,
        owner=owner,
    )
    assert claimed is not None
    assert claimed.id is not None
    async with memory_session_factory() as db:
        initial_claim = await memory_job_crud.get_active_claim(
            db,
            uid=uid,
            job_id=claimed.id,
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
            now = await get_database_time(recovery_db)
            await recovery_db.execute(
                update(LongTermMemoryMutationJob)
                .where(
                    LongTermMemoryMutationJob.uid == uid,
                    LongTermMemoryMutationJob.id == claimed.id,
                )
                .values(lock_until=now - timedelta(seconds=1))
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

    async def persist_payload_without_write(_db: Any, **_kwargs: Any) -> Any:
        return claimed

    monkeypatch.setattr(memory_store_crud, "lock_for_mutation", return_store_without_write)
    monkeypatch.setattr(memory_job_crud, "get_active_claim", get_claim_for_switch)
    monkeypatch.setattr(reindex_handler, "read_recallable_records", return_records_without_read)
    monkeypatch.setattr(memory_maintenance_job_crud, "update_running_payload", persist_payload_without_write)

    context = MemoryJobExecutionContext(
        job=claimed,
        worker_id=owner,
        session_factory=memory_session_factory,
    )
    validation = ValidationSnapshot(
        records=(record_snapshot(record, embedding_revision),),
        count=1,
        success_count=1,
    )
    with pytest.raises(MemoryJobLeaseLostError):
        await reindex_handler._switch_reindex(context, claimed.payload, validation)

    assert recovered
    async with memory_session_factory() as db:
        job = await memory_job_crud.get_by_id(db, uid=uid, job_id=claimed.id)
        current_store = await memory_store_crud.get_by_uid(db, uid=uid)
    assert job is not None
    assert job.status == LongTermMemoryMutationStatus.FAILED
    assert current_store is not None
    assert current_store.active_collection_name == old_collection
    assert current_store.index_revision == index_revision
    assert current_store.index_status == LongTermMemoryIndexStatus.FAILED
