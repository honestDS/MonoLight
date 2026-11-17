from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import ERR_MEMORY_MAINTENANCE_STATE_CONFLICT
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud, memory_store_crud
from app.core.i18n import t
from app.core.memory import build_memory_vector_item_id
from app.core.memory.errors import MemoryConflictError
from app.core.memory_jobs.manager import MemoryJobTargetBusyError
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecordIndexStatus,
)
from tests.integration.memory import test_memory_organization_merge_worker as worker_tests
from tests.integration.memory.test_memory_organization_merge_worker import (
    ACTIVE_EMBEDDING_REVISION,
    COLLECTION_NAME,
    _consumer,
    _FakeVectorBackend,
    _get_all_deltas,
    _get_cleanup_jobs,
    _get_record,
    _get_revisions,
    _prepare_merge,
    _run_child,
    _staged_vector_id_prefix,
)

memory_organization_handler = worker_tests.memory_organization_handler
memory_session_factory = worker_tests.memory_session_factory
vector_backend = worker_tests.vector_backend


@pytest.mark.asyncio
async def test_organization_merge_active_embedding_change_after_vector_write_fails_and_cleans_orphan(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-config-change-worker"
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="config changed organization content",
        target_memory_key="config-changed-organization-key",
        max_attempts=1,
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in (1, 2, 3)}
    new_vector_prefix = _staged_vector_id_prefix(1, 2, child_id)
    staged_vector_id: str | None = None
    old_vector_items = {build_memory_vector_item_id(memory_id, 1): dict(vector_backend.collections[COLLECTION_NAME]["items"][build_memory_vector_item_id(memory_id, 1)]) for memory_id in (1, 2, 3)}
    changed = False

    async def change_active_store(_collection_name: str, item_ids: list[str]) -> None:
        nonlocal changed, staged_vector_id
        if changed or len(item_ids) != 1 or not item_ids[0].startswith(new_vector_prefix):
            return
        changed = True
        staged_vector_id = item_ids[0]
        async with memory_session_factory() as db:
            updated = await memory_store_crud.update_by_uid(
                db,
                uid=uid,
                active_embedding_signature="organization-embedding-signature-changed",
                active_embedding_revision=ACTIVE_EMBEDDING_REVISION + 1,
            )
            assert updated is not None

    vector_backend.upsert_hook = change_active_store
    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.active_mutation_key is None
        assert changed is True
        assert staged_vector_id is not None
        assert any(staged_vector_id in call["ids"] for call in vector_backend.upsert_calls)
        collection = vector_backend.collections[COLLECTION_NAME]
        assert staged_vector_id not in collection["items"]
        assert all(build_memory_vector_item_id(memory_id, 1) in collection["items"] for memory_id in (1, 2, 3))
        assert all(collection["items"][item_id] == item for item_id, item in old_vector_items.items())
        for memory_id in (1, 2, 3):
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.index_status == LongTermMemoryRecordIndexStatus.READY
            assert record.pending_mutation_job_id is None
            assert (record.content, record.memory_key, record.content_hash, record.vector_item_id) == (
                before[memory_id].content,
                before[memory_id].memory_key,
                before[memory_id].content_hash,
                before[memory_id].vector_item_id,
            )
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=(1, 2, 3))
            assert [record.id for record in recallable] == [1, 2, 3]
            assert await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP) == 0
            store = await memory_store_crud.get_by_uid(db, uid=uid)
        assert store is not None
        assert store.active_embedding_revision == ACTIVE_EMBEDDING_REVISION + 1
        assert store.active_embedding_signature == "organization-embedding-signature-changed"
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_active_migration_after_vector_write_fails_before_publish(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-active-migration-after-vector-write-worker"
    _parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="migration during publish content",
        target_memory_key="migration-during-publish-key",
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in source_ids}
    new_vector_prefix = _staged_vector_id_prefix(1, 2, child_id)
    staged_vector_id: str | None = None
    changed = False

    async def start_migration(_collection_name: str, item_ids: list[str]) -> None:
        nonlocal changed, staged_vector_id
        if changed or len(item_ids) != 1 or not item_ids[0].startswith(new_vector_prefix):
            return
        changed = True
        staged_vector_id = item_ids[0]
        async with memory_session_factory() as db:
            updated = await memory_store_crud.update_by_uid(
                db,
                uid=uid,
                migration_job_id=9104,
                migration_status=LongTermMemoryMigrationStatus.BUILDING,
                commit=False,
            )
            assert updated is not None
            await db.commit()

    vector_backend.upsert_hook = start_migration
    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.attempt_count == 1
        assert failed.error == t(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        assert failed.active_mutation_key is None
        assert changed is True
        assert staged_vector_id is not None
        assert any(staged_vector_id in call["ids"] for call in vector_backend.upsert_calls)
        assert (COLLECTION_NAME, [staged_vector_id]) in vector_backend.delete_calls
        collection = vector_backend.collections[COLLECTION_NAME]
        assert staged_vector_id not in collection["items"]
        assert all(build_memory_vector_item_id(memory_id, 1) in collection["items"] for memory_id in source_ids)
        for memory_id in source_ids:
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.pending_mutation_job_id is None
            assert record.index_status == LongTermMemoryRecordIndexStatus.READY
            assert (record.content, record.memory_key, record.content_hash, record.vector_item_id) == (
                before[memory_id].content,
                before[memory_id].memory_key,
                before[memory_id].content_hash,
                before[memory_id].vector_item_id,
            )
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=source_ids)
            assert await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP) == 0
        assert [record.id for record in recallable] == list(source_ids)
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
    finally:
        await consumer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("reindex_phase", ["before_execution", "after_vector_write"])
async def test_organization_merge_reindex_boundary_fails_without_publication(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    reindex_phase: str,
) -> None:
    uid = f"organization-merge-reindex-{reindex_phase}-worker"
    _parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="reindex boundary organization content",
        target_memory_key="reindex-boundary-organization-key",
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in source_ids}
    old_vector_items = {build_memory_vector_item_id(memory_id, 1): dict(vector_backend.collections[COLLECTION_NAME]["items"][build_memory_vector_item_id(memory_id, 1)]) for memory_id in source_ids}
    new_vector_prefix = _staged_vector_id_prefix(1, 2, child_id)
    staged_vector_id: str | None = None
    switched = False

    if reindex_phase == "before_execution":
        async with memory_session_factory() as db:
            updated = await memory_store_crud.update_by_uid(
                db,
                uid=uid,
                index_status=LongTermMemoryIndexStatus.REINDEXING,
            )
            assert updated is not None
    else:

        async def switch_to_reindexing(_collection_name: str, item_ids: list[str]) -> None:
            nonlocal switched, staged_vector_id
            if switched or len(item_ids) != 1 or not item_ids[0].startswith(new_vector_prefix):
                return
            switched = True
            staged_vector_id = item_ids[0]
            async with memory_session_factory() as db:
                updated = await memory_store_crud.update_by_uid(
                    db,
                    uid=uid,
                    index_status=LongTermMemoryIndexStatus.REINDEXING,
                    commit=False,
                )
                assert updated is not None
                await db.commit()

        vector_backend.upsert_hook = switch_to_reindexing

    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.attempt_count == 1
        assert failed.error == t(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        assert failed.active_mutation_key is None
        if reindex_phase == "before_execution":
            assert vector_backend.upsert_calls == []
        else:
            assert switched is True
            assert staged_vector_id is not None
            assert any(call["ids"] == [staged_vector_id] for call in vector_backend.upsert_calls)
            assert (COLLECTION_NAME, [staged_vector_id]) in vector_backend.delete_calls

        collection = vector_backend.collections[COLLECTION_NAME]
        if staged_vector_id is not None:
            assert staged_vector_id not in collection["items"]
        for item_id, item in old_vector_items.items():
            assert collection["items"][item_id] == item
        for memory_id in source_ids:
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.deleted_at is None
            assert record.pending_mutation_job_id is None
            assert record.version == before[memory_id].version == 1
            assert record.memory_key == before[memory_id].memory_key
            assert record.content == before[memory_id].content
            assert record.content_hash == before[memory_id].content_hash
            assert record.vector_item_id == before[memory_id].vector_item_id
            assert record.index_status == LongTermMemoryRecordIndexStatus.READY
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=source_ids)
            cleanup_count = await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP)
        assert [record.id for record in recallable] == list(source_ids)
        assert cleanup_count == 0
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_active_migration_before_execution_fails_and_releases_sources(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
) -> None:
    uid = "organization-merge-active-migration-before-execution-worker"
    migration_job_id = 9101
    _parent_id, child_id, source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="migration delta organization content",
        target_memory_key="migration-delta-organization-key",
    )
    async with memory_session_factory() as db:
        updated = await memory_store_crud.update_by_uid(
            db,
            uid=uid,
            migration_job_id=migration_job_id,
            migration_status=LongTermMemoryMigrationStatus.BUILDING,
            commit=False,
        )
        assert updated is not None
        await db.commit()

    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.attempt_count == 1
        assert failed.error == t(ERR_MEMORY_MAINTENANCE_STATE_CONFLICT)
        assert failed.active_mutation_key is None
        assert vector_backend.upsert_calls == []
        assert not any(item_id.startswith(build_memory_vector_item_id(1, 2)) for item_id in vector_backend.collections[COLLECTION_NAME]["items"])
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
        assert await _get_cleanup_jobs(memory_session_factory, uid=uid, merge_job_id=child_id) == []
        for memory_id in source_ids:
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None
            assert record.pending_mutation_job_id is None
            assert record.is_active is True
            assert record.index_status == LongTermMemoryRecordIndexStatus.READY
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=source_ids)
        assert [record.id for record in recallable] == list(source_ids)
        async with memory_session_factory() as db:
            store = await memory_store_crud.get_by_uid(db, uid=uid)
        assert store is not None
        assert store.migration_job_id == migration_job_id
        assert store.migration_status == LongTermMemoryMigrationStatus.BUILDING
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_second_delta_conflict_rolls_back_publication_and_releases_sources(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-merge-delta-rollback-worker"
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="delta conflict organization content",
        target_memory_key="delta-conflict-organization-key",
        max_attempts=1,
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in (1, 2, 3)}
    original_append = memory_organization_handler.append_memory_embedding_delta
    append_calls = 0

    async def raise_on_second_delta(*args: Any, **kwargs: Any) -> Any:
        nonlocal append_calls
        append_calls += 1
        if append_calls == 2:
            raise MemoryConflictError("second migration delta conflict")
        return await original_append(*args, **kwargs)

    monkeypatch.setattr(memory_organization_handler, "append_memory_embedding_delta", raise_on_second_delta)
    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert append_calls == 2
        assert failed.active_mutation_key is None
        assert not any(item_id.startswith(build_memory_vector_item_id(1, 2)) for item_id in vector_backend.collections[COLLECTION_NAME]["items"])
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
        assert await _get_cleanup_jobs(memory_session_factory, uid=uid, merge_job_id=child_id) == []
        for memory_id in (1, 2, 3):
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.deleted_at is None
            assert record.memory_key == before[memory_id].memory_key
            assert record.content_hash == before[memory_id].content_hash
            assert record.pending_mutation_job_id is None
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            store = await memory_store_crud.get_by_uid(db, uid=uid)
        assert store is not None
        assert store.migration_delta_high_watermark == 0
    finally:
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_merge_cleanup_creation_conflict_rolls_back_all_database_publication(
    memory_session_factory: async_sessionmaker[AsyncSession],
    vector_backend: _FakeVectorBackend,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-merge-cleanup-conflict-worker"
    _parent_id, child_id, _source_ids = await _prepare_merge(
        memory_session_factory,
        vector_backend,
        uid=uid,
        memory_ids=(1, 2, 3),
        primary_memory_id=1,
        action="merge",
        pinned_ids=frozenset({1}),
        target_content="cleanup conflict organization content",
        target_memory_key="cleanup-conflict-organization-key",
        max_attempts=1,
    )
    before = {memory_id: await _get_record(memory_session_factory, uid=uid, memory_id=memory_id) for memory_id in (1, 2, 3)}

    async def raise_cleanup_conflict(*_args: Any, **_kwargs: Any) -> Any:
        raise MemoryJobTargetBusyError("existing cleanup business conflict")

    monkeypatch.setattr(memory_organization_handler.memory_job_manager, "create_organization_cleanup_job", raise_cleanup_conflict)
    consumer = _consumer(memory_session_factory)
    try:
        failed = await _run_child(
            consumer,
            memory_session_factory,
            uid=uid,
            child_id=child_id,
            status=LongTermMemoryMutationStatus.FAILED,
        )
        assert failed.active_mutation_key is None
        assert not any(item_id.startswith(build_memory_vector_item_id(1, 2)) for item_id in vector_backend.collections[COLLECTION_NAME]["items"])
        assert await _get_cleanup_jobs(memory_session_factory, uid=uid, merge_job_id=child_id) == []
        assert await _get_all_deltas(memory_session_factory, uid=uid) == []
        for memory_id in (1, 2, 3):
            record = await _get_record(memory_session_factory, uid=uid, memory_id=memory_id)
            assert record is not None and before[memory_id] is not None
            assert record.is_active is True
            assert record.deleted_at is None
            assert record.memory_key == before[memory_id].memory_key
            assert record.content_hash == before[memory_id].content_hash
            assert record.pending_mutation_job_id is None
            assert [revision.version for revision in await _get_revisions(memory_session_factory, uid=uid, memory_id=memory_id)] == [1]
        async with memory_session_factory() as db:
            recallable = await memory_record_crud.list_recallable_by_ids(db, uid=uid, memory_ids=(1, 2, 3))
            assert [record.id for record in recallable] == [1, 2, 3]
            assert await memory_job_crud.count(db, uid=uid, operation=LongTermMemoryMutationOperation.DELETE_CLEANUP) == 0
    finally:
        await consumer.stop()
