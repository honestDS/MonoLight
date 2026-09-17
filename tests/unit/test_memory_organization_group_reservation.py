from __future__ import annotations

from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.memory.job import memory_job_crud
from app.core.crud.memory.store import memory_record_crud
from app.core.memory import build_memory_active_mutation_key, build_memory_organization_active_mutation_key
from app.core.memory.organization import (
    MemoryOrganizationValidatedItem,
    MemoryOrganizationValidatedSource,
    MemoryOrganizationValidatedTarget,
)
from app.core.memory_jobs.manager import MemoryJobManager, MemoryJobTargetBusyError
from app.core.utils.time import get_local_time
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryStore,
    LongTermMemoryType,
)
from app.providers.database.time import get_database_time


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    LongTermMemoryStore.__table__,
                    LongTermMemoryMutationJob.__table__,
                    LongTermMemoryRecord.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


async def _parent(db: AsyncSession, *, uid: str) -> LongTermMemoryMutationJob:
    db.add(
        LongTermMemoryStore(
            uid=uid,
            active_embedding_channel_id=1,
            active_embedding_model_id="memory-model-v1",
            active_embedding_dimensions=3,
            active_embedding_signature="organization-embedding-signature",
            active_embedding_revision=3,
            active_collection_name="organization-merge-collection",
            index_revision=8,
            index_status=LongTermMemoryIndexStatus.READY,
        )
    )
    await db.flush()
    parent, created = await memory_job_crud.create(
        db,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        dedupe_key=f"parent-{uid}",
        active_mutation_key=build_memory_organization_active_mutation_key(uid),
        payload={},
        available_at=await get_database_time(db),
        commit=False,
    )
    assert created and parent.id is not None
    claimed = await memory_job_crud.try_claim(
        db,
        uid=uid,
        job_id=parent.id,
        owner="organization-worker",
        commit=False,
    )
    assert claimed is not None
    await db.commit()
    return claimed


async def _record(
    db: AsyncSession,
    *,
    uid: str,
    memory_id: int,
    version: int = 1,
    **overrides: object,
) -> LongTermMemoryRecord:
    values: dict[str, object] = {
        "id": memory_id,
        "memory_key": f"source-{memory_id}",
        "content": f"source content {memory_id}",
        "content_token_count": 3,
        "content_hash": f"source-hash-{memory_id}",
        "version": version,
        "indexed_version": version,
        "vector_item_id": f"source-vector-{memory_id}",
        "is_active": True,
        "suppress_recall": False,
        "index_status": LongTermMemoryRecordIndexStatus.READY,
    }
    values.update(overrides)
    return await memory_record_crud.create(db, uid=uid, commit=False, **values)


def _item(
    source_versions: dict[int, int],
    *,
    action: str = "merge",
    primary_memory_id: int | None = None,
    memory_key: str = "organized-key",
    content_hash: str | None = None,
) -> MemoryOrganizationValidatedItem:
    sources = tuple(
        MemoryOrganizationValidatedSource(
            memory_id=memory_id,
            expected_version=version,
            pinned=False,
        )
        for memory_id, version in source_versions.items()
    )
    target = MemoryOrganizationValidatedTarget(
        content=f"organized content {memory_key}",
        memory_key=memory_key,
        memory_type=LongTermMemoryType.FACT,
        content_token_count=3,
        content_hash=content_hash or f"organized-hash-{memory_key}",
    )
    return MemoryOrganizationValidatedItem(
        action=action,
        sources=sources,
        primary_memory_id=primary_memory_id if action == "merge" else None,
        target=target,
    )


async def _submit_child(
    db: AsyncSession,
    parent: LongTermMemoryMutationJob,
    item: MemoryOrganizationValidatedItem,
    *,
    group_index: int,
) -> LongTermMemoryMutationJob | None:
    return await MemoryJobManager().create_organization_merge_child(
        db,
        parent_job=parent,
        item=item,
        group_index=group_index,
        snapshot_digest="snapshot-digest",
        active_embedding_revision=3,
        index_revision=8,
        policy_version=5,
    )


@pytest.mark.asyncio
async def test_update_and_merge_reserve_all_sources_in_stable_order(db_session: AsyncSession) -> None:
    uid = "group-reservation-success-user"
    parent = await _parent(db_session, uid=uid)
    await _record(db_session, uid=uid, memory_id=1)
    await _record(db_session, uid=uid, memory_id=2)
    await _record(db_session, uid=uid, memory_id=3)
    await db_session.commit()

    update_child = await _submit_child(
        db_session,
        parent,
        _item({1: 1}, action="update", memory_key="updated-key"),
        group_index=0,
    )
    merge_child = await _submit_child(
        db_session,
        parent,
        _item({3: 1, 2: 1}, primary_memory_id=2, memory_key="merged-key"),
        group_index=1,
    )
    assert update_child is not None and update_child.id is not None
    assert merge_child is not None and merge_child.id is not None

    records = await memory_record_crud.get_organization_group(
        db_session,
        uid=uid,
        memory_ids=[3, 1, 2],
    )
    assert [record.id for record in records] == [1, 2, 3]
    assert records[0].pending_mutation_job_id == update_child.id
    assert records[1].pending_mutation_job_id == merge_child.id
    assert records[2].pending_mutation_job_id == merge_child.id
    assert update_child.active_mutation_key == build_memory_active_mutation_key(uid, memory_id=1)
    assert merge_child.active_mutation_key == build_memory_active_mutation_key(uid, memory_id=2)
    assert [source["memory_id"] for source in merge_child.payload["sources"]] == [2, 3]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_case",
    [
        "inactive",
        "deleted",
        "index_not_ready",
        "indexed_version_stale",
        "vector_missing",
        "suppressed",
        "version_stale",
        "pending",
        "cross_uid",
        "missing",
    ],
)
async def test_invalid_source_group_rolls_back_without_partial_reservation_and_next_group_can_submit(
    db_session: AsyncSession,
    invalid_case: str,
) -> None:
    uid = f"group-reservation-invalid-{invalid_case}"
    parent = await _parent(db_session, uid=uid)
    await _record(db_session, uid=uid, memory_id=1)
    record_two = await _record(db_session, uid=uid, memory_id=2)
    await _record(db_session, uid=uid, memory_id=3)
    if invalid_case == "inactive":
        record_two.is_active = False
    elif invalid_case == "deleted":
        record_two.deleted_at = get_local_time()
    elif invalid_case == "index_not_ready":
        record_two.index_status = LongTermMemoryRecordIndexStatus.FAILED
    elif invalid_case == "indexed_version_stale":
        record_two.indexed_version = 0
    elif invalid_case == "vector_missing":
        record_two.vector_item_id = None
    elif invalid_case == "suppressed":
        record_two.suppress_recall = True
    elif invalid_case == "version_stale":
        record_two.version = 2
        record_two.indexed_version = 2
    elif invalid_case == "pending":
        blocker, created = await memory_job_crud.create(
            db_session,
            uid=uid,
            operation=LongTermMemoryMutationOperation.UPDATE,
            dedupe_key="source-blocker",
            active_mutation_key=build_memory_active_mutation_key(uid, memory_id=2),
            memory_id=2,
            expected_version=1,
            payload={"kind": "blocker"},
            commit=False,
        )
        assert created and blocker.id is not None
        assert await memory_record_crud.reserve_pending_mutation(
            db_session,
            uid=uid,
            memory_id=2,
            job_id=blocker.id,
            expected_version=1,
            commit=False,
        )
    elif invalid_case == "cross_uid":
        await _record(db_session, uid="different-user", memory_id=4)
    elif invalid_case == "missing":
        pass
    await db_session.commit()

    first_sources = {1: 1, 2: 1}
    if invalid_case == "version_stale":
        first_sources[2] = 1
    elif invalid_case == "cross_uid":
        first_sources = {1: 1, 4: 1}
    elif invalid_case == "missing":
        first_sources = {1: 1, 99: 1}
    first = await _submit_child(
        db_session,
        parent,
        _item(first_sources, primary_memory_id=1, memory_key="stale-group-key"),
        group_index=0,
    )
    second = await _submit_child(
        db_session,
        parent,
        _item({3: 1}, action="update", memory_key="valid-group-key"),
        group_index=1,
    )
    assert first is None
    assert second is not None and second.id is not None

    refreshed_one = await memory_record_crud.get_by_id(db_session, uid=uid, memory_id=1)
    refreshed_two = await memory_record_crud.get_by_id(db_session, uid=uid, memory_id=2)
    refreshed_three = await memory_record_crud.get_by_id(db_session, uid=uid, memory_id=3)
    assert refreshed_one is not None and refreshed_one.pending_mutation_job_id is None
    if invalid_case == "pending":
        assert refreshed_two is not None and refreshed_two.pending_mutation_job_id is not None
    else:
        assert refreshed_two is not None and refreshed_two.pending_mutation_job_id is None
    assert refreshed_three is not None and refreshed_three.pending_mutation_job_id == second.id
    children = await memory_job_crud.list_children_by_parent_job_id(
        db_session,
        uid=uid,
        parent_job_id=parent.id,
    )
    assert [child.id for child in children] == [second.id]


@pytest.mark.asyncio
@pytest.mark.parametrize("conflict_kind", ["memory_key", "content_hash"])
async def test_group_external_identity_conflicts_stale_but_group_internal_reuse_is_allowed(
    db_session: AsyncSession,
    conflict_kind: str,
) -> None:
    uid = f"group-identity-conflict-{conflict_kind}"
    parent = await _parent(db_session, uid=uid)
    await _record(db_session, uid=uid, memory_id=1)
    await _record(db_session, uid=uid, memory_id=2)
    await _record(
        db_session,
        uid=uid,
        memory_id=10,
        memory_key="blocked-key",
        content_hash="blocked-hash",
    )
    await db_session.commit()

    if conflict_kind == "memory_key":
        blocked = _item(
            {1: 1, 2: 1},
            primary_memory_id=1,
            memory_key="blocked-key",
            content_hash="new-hash",
        )
    else:
        blocked = _item(
            {1: 1, 2: 1},
            primary_memory_id=1,
            memory_key="new-key",
            content_hash="blocked-hash",
        )
    assert await _submit_child(db_session, parent, blocked, group_index=0) is None

    internal = _item(
        {1: 1, 2: 1},
        primary_memory_id=1,
        memory_key="source-1",
        content_hash="source-hash-1",
    )
    child = await _submit_child(db_session, parent, internal, group_index=1)
    assert child is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation,payload_shape",
    [
        (LongTermMemoryMutationOperation.CREATE, "top_level"),
        (LongTermMemoryMutationOperation.UPDATE, "top_level"),
        (LongTermMemoryMutationOperation.RESTORE, "top_level"),
        (LongTermMemoryMutationOperation.CREATE_WITH_EVICTION, "publication"),
        (LongTermMemoryMutationOperation.ORGANIZE_MERGE, "target"),
    ],
)
@pytest.mark.parametrize(
    "status",
    [
        LongTermMemoryMutationStatus.PENDING,
        LongTermMemoryMutationStatus.RUNNING,
        LongTermMemoryMutationStatus.RETRY,
        LongTermMemoryMutationStatus.SUCCEEDED,
    ],
)
async def test_unfinished_target_identities_block_but_terminal_jobs_do_not(
    db_session: AsyncSession,
    operation: LongTermMemoryMutationOperation,
    payload_shape: str,
    status: LongTermMemoryMutationStatus,
) -> None:
    uid = f"group-job-identity-{operation.value}-{status.value}"
    parent = await _parent(db_session, uid=uid)
    await _record(db_session, uid=uid, memory_id=1)
    target = {"memory_key": "target-key", "content_hash": "target-hash"}
    if payload_shape == "top_level":
        payload: dict[str, object] = dict(target)
    elif payload_shape == "publication":
        payload = {"publication": dict(target)}
    else:
        payload = {"target": dict(target)}
    blocker, created = await memory_job_crud.create(
        db_session,
        uid=uid,
        operation=operation,
        dedupe_key=f"identity-blocker-{operation.value}-{status.value}",
        active_mutation_key=(
            None
            if status
            in {
                LongTermMemoryMutationStatus.SUCCEEDED,
                LongTermMemoryMutationStatus.FAILED,
                LongTermMemoryMutationStatus.CANCELLED,
            }
            else f"identity-blocker-key-{operation.value}-{status.value}"
        ),
        memory_id=99 if operation in {LongTermMemoryMutationOperation.UPDATE, LongTermMemoryMutationOperation.RESTORE} else None,
        expected_version=1 if operation in {LongTermMemoryMutationOperation.UPDATE, LongTermMemoryMutationOperation.RESTORE} else None,
        payload=payload,
        status=status,
        commit=False,
    )
    assert created and blocker.id is not None
    await db_session.commit()

    child = await _submit_child(
        db_session,
        parent,
        _item({1: 1}, action="update", memory_key="target-key", content_hash="target-hash"),
        group_index=0,
    )
    if status in {
        LongTermMemoryMutationStatus.PENDING,
        LongTermMemoryMutationStatus.RUNNING,
        LongTermMemoryMutationStatus.RETRY,
    }:
        assert child is None
    else:
        assert child is not None


@pytest.mark.asyncio
async def test_group_pending_blocks_update_delete_and_other_merge(db_session: AsyncSession) -> None:
    uid = "group-pending-competition-user"
    parent = await _parent(db_session, uid=uid)
    assert parent.id is not None
    parent_id = parent.id
    await _record(db_session, uid=uid, memory_id=1)
    await _record(db_session, uid=uid, memory_id=2)
    await _record(db_session, uid=uid, memory_id=3)
    await db_session.commit()

    child = await _submit_child(
        db_session,
        parent,
        _item({1: 1, 2: 1}, primary_memory_id=1, memory_key="first-merge"),
        group_index=0,
    )
    assert child is not None and child.id is not None
    await db_session.commit()
    manager = MemoryJobManager()
    for operation, memory_id in (
        (LongTermMemoryMutationOperation.UPDATE, 1),
        (LongTermMemoryMutationOperation.DELETE_CLEANUP, 2),
    ):
        with pytest.raises(MemoryJobTargetBusyError):
            await manager.submit(
                db_session,
                uid=uid,
                operation=operation,
                dedupe_key=f"competing-{operation.value}",
                active_mutation_key=build_memory_active_mutation_key(uid, memory_id=memory_id),
                memory_id=memory_id,
                expected_version=1,
                payload={"kind": operation.value},
            )

    parent = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=parent_id)
    assert parent is not None
    competing_merge = await _submit_child(
        db_session,
        parent,
        _item({1: 1, 3: 1}, primary_memory_id=3, memory_key="second-merge"),
        group_index=1,
    )
    assert competing_merge is None


@pytest.mark.asyncio
@pytest.mark.parametrize("initial_status", [LongTermMemoryMutationStatus.PENDING, LongTermMemoryMutationStatus.RETRY])
async def test_cancelled_merge_child_releases_all_sources_without_affecting_parent_or_other_child(
    db_session: AsyncSession,
    initial_status: LongTermMemoryMutationStatus,
) -> None:
    uid = f"group-cancel-{initial_status.value}-user"
    parent = await _parent(db_session, uid=uid)
    await _record(db_session, uid=uid, memory_id=1)
    await _record(db_session, uid=uid, memory_id=2)
    await _record(db_session, uid=uid, memory_id=3)
    await db_session.commit()

    first = await _submit_child(
        db_session,
        parent,
        _item({1: 1, 2: 1}, primary_memory_id=1, memory_key="cancelled-merge"),
        group_index=0,
    )
    other = await _submit_child(
        db_session,
        parent,
        _item({3: 1}, action="update", memory_key="other-update"),
        group_index=1,
    )
    assert first is not None and first.id is not None
    assert other is not None and other.id is not None
    if initial_status == LongTermMemoryMutationStatus.RETRY:
        await memory_job_crud.update_status(
            db_session,
            uid=uid,
            job_id=first.id,
            status=LongTermMemoryMutationStatus.RETRY,
            commit=True,
        )

    cancellation = await MemoryJobManager().request_cancel(db_session, uid=uid, job_id=first.id)
    assert cancellation.accepted and cancellation.changed
    cancelled = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=first.id)
    current_parent = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=parent.id)
    current_other = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=other.id)
    assert cancelled is not None and cancelled.status == LongTermMemoryMutationStatus.CANCELLED
    assert current_parent is not None and current_parent.status == LongTermMemoryMutationStatus.RUNNING
    assert current_other is not None and current_other.status == LongTermMemoryMutationStatus.PENDING
    for memory_id in (1, 2):
        record = await memory_record_crud.get_by_id(db_session, uid=uid, memory_id=memory_id)
        assert record is not None and record.pending_mutation_job_id is None
    other_record = await memory_record_crud.get_by_id(db_session, uid=uid, memory_id=3)
    assert other_record is not None and other_record.pending_mutation_job_id == other.id
