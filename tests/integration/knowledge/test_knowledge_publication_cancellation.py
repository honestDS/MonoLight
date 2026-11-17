from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import select

from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import organization_lock_token_for_job
from app.core.knowledge.errors import ManagedKnowledgeConflictError
from app.core.knowledge.organization import create_knowledge_organization_snapshot
from app.core.knowledge.organization_publication import (
    KnowledgeOrganizationPlanStaleError,
    publish_knowledge_organization_plan,
)
from app.core.knowledge.organization_types import KnowledgeOrganizationPlan
from app.core.knowledge_jobs.executor import (
    KnowledgeJobCancelledError,
    KnowledgeJobExecutionContext,
)
from app.core.knowledge_jobs.handlers import handle_organization_mutation
from app.models.knowledge_base import (
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationSnapshotItem,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeRevisionOperation,
)
from app.providers.database.time import get_database_time
from tests.integration.knowledge.knowledge_publication_support import (
    _add_item,
    _complete_stage,
    _create_managed_container,
    _create_running_organization_job,
    _mark_organization_job_terminal,
    _request_organization_cancel,
    _seed_items,
)
from tests.integration.knowledge.knowledge_publication_support import _session_factory as _session_factory


@pytest.mark.asyncio
async def test_parent_cancellation_rolls_back_committed_unpublished_mutation(session_factory):
    parent_owner = "knowledge_organization_publication-cancel-rollback-parent"
    mutation_owner = "knowledge_organization_publication-cancel-rollback-mutation"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="cancel-rollback")
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=2)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="cancel-rollback",
            owner=parent_owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        plan = KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "keep",
                        "source": {"knowledge_id": items[0].id, "expected_version": 1},
                        "summary": "keep source",
                    },
                    {
                        "action": "update",
                        "source": {"knowledge_id": items[1].id, "expected_version": 1},
                        "target": {
                            "knowledge_key": "updated-topic",
                            "content": "updated organization content",
                        },
                        "summary": "update source",
                    },
                ]
            }
        )
        final_stage = await _complete_stage(db, snapshot=snapshot, plan=plan, stage_key="stage-15-cancel-rollback")
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id
        target_id = items[1].id

    publication = await publish_knowledge_organization_plan(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        organization_job_id=parent_id,
        owner=parent_owner,
        snapshot_id=snapshot_id,
        final_stage_id=final_stage_id,
        plan=plan,
    )
    assert len(publication.mutation_job_ids) == 1
    mutation_job_id = publication.mutation_job_ids[0]

    async with session_factory() as db:
        published_mutation = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=mutation_job_id)
        assert published_mutation is not None
        published_mutation.available_at = await get_database_time(db)
        mutation_job = await knowledge_job_crud.try_claim(
            db,
            uid="user-1",
            job_id=mutation_job_id,
            owner=mutation_owner,
            lease_seconds=60,
            enabled_operations=(KnowledgeJobOperation.ORGANIZE_MUTATION,),
            commit=False,
        )
        assert mutation_job is not None
        await db.commit()

    mutation_context = KnowledgeJobExecutionContext(
        job=mutation_job,
        worker_id=mutation_owner,
        session_factory=session_factory,
    )
    execution = await handle_organization_mutation(mutation_context)
    managed_update_job_id = execution.result["mutation_job_ids"][0]

    async with session_factory() as db:
        prepared_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        prepared_mutation = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=mutation_job_id)
        prepared_update = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=managed_update_job_id)
        prepared_target = await db.get(ManagedKnowledgeItem, target_id)
        prepared_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id).order_by(ManagedKnowledgeItem.id))).scalars())

    assert prepared_parent is not None and prepared_parent.status == KnowledgeJobStatus.RUNNING
    assert prepared_mutation is not None and prepared_mutation.status == KnowledgeJobStatus.RUNNING
    assert prepared_update is not None and prepared_update.status == KnowledgeJobStatus.PENDING
    assert prepared_target is not None
    assert prepared_target.content == "updated organization content"
    assert prepared_target.version == 2
    assert prepared_target.indexed_version == 1
    assert prepared_target.is_recallable is False
    assert prepared_target.vector_item_ids == ["vector-topic-1"]
    assert prepared_target.pending_job_id == managed_update_job_id
    assert [item.organization_lock_token for item in prepared_items] == [organization_lock_token_for_job(parent_id)] * 2

    async with session_factory() as db:
        cancellation = await _request_organization_cancel(db, uid="user-1", job_id=parent_id)
        assert cancellation.accepted is True
        assert cancellation.changed is True
        requested_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        requested_mutation = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=mutation_job_id)
        cancelled_update = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=managed_update_job_id)
        restored_target = await db.get(ManagedKnowledgeItem, target_id)
        revisions = list(
            (
                await db.execute(
                    select(ManagedKnowledgeRevision)
                    .where(
                        ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id,
                        ManagedKnowledgeRevision.knowledge_id == target_id,
                    )
                    .order_by(ManagedKnowledgeRevision.version)
                )
            ).scalars()
        )

    assert requested_parent is not None
    assert requested_parent.status == KnowledgeJobStatus.RUNNING
    assert requested_parent.cancel_requested_at is not None
    assert requested_mutation is not None
    assert requested_mutation.status == KnowledgeJobStatus.RUNNING
    assert requested_mutation.cancel_requested_at is not None
    assert cancelled_update is not None and cancelled_update.status == KnowledgeJobStatus.CANCELLED
    assert restored_target is not None
    assert restored_target.content == "organization content 1"
    assert restored_target.version == 1
    assert restored_target.indexed_version == 1
    assert restored_target.is_recallable is True
    assert restored_target.vector_item_ids == ["vector-topic-1"]
    assert restored_target.pending_job_id is None
    assert restored_target.organization_lock_token == organization_lock_token_for_job(parent_id)
    assert [(revision.version, revision.operation) for revision in revisions] == [
        (1, ManagedKnowledgeRevisionOperation.CREATE),
    ]

    async with session_factory() as db:
        assert await _mark_organization_job_terminal(
            db,
            uid="user-1",
            job_id=mutation_job_id,
            owner=mutation_owner,
            status=KnowledgeJobStatus.CANCELLED,
        )

    async with session_factory() as db:
        assert await _mark_organization_job_terminal(
            db,
            uid="user-1",
            job_id=parent_id,
            owner=parent_owner,
            status=KnowledgeJobStatus.CANCELLED,
        )

    async with session_factory() as db:
        final_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        final_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        final_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id).order_by(ManagedKnowledgeItem.id))).scalars())
        final_revisions = list(
            (
                await db.execute(
                    select(ManagedKnowledgeRevision)
                    .where(
                        ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id,
                        ManagedKnowledgeRevision.knowledge_id == target_id,
                    )
                    .order_by(ManagedKnowledgeRevision.version)
                )
            ).scalars()
        )

    assert final_parent is not None and final_parent.status == KnowledgeJobStatus.CANCELLED
    assert {child.id for child in final_children} == {mutation_job_id, managed_update_job_id}
    assert {child.status for child in final_children} == {KnowledgeJobStatus.CANCELLED}
    assert all(item.organization_lock_token is None for item in final_items)
    final_target = next(item for item in final_items if item.id == target_id)
    assert final_target.content == "organization content 1"
    assert final_target.version == 1
    assert final_target.indexed_version == 1
    assert final_target.is_recallable is True
    assert final_target.vector_item_ids == ["vector-topic-1"]
    assert final_target.pending_job_id is None
    assert [(revision.version, revision.operation) for revision in final_revisions] == [
        (1, ManagedKnowledgeRevisionOperation.CREATE),
    ]


@pytest.mark.asyncio
async def test_parent_cancellation_rolls_back_committed_unpublished_merge_mutation(session_factory):
    parent_owner = "knowledge_organization_publication-cancel-merge-parent"
    mutation_owner = "knowledge_organization_publication-cancel-merge-mutation"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="cancel-merge")
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=2)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="cancel-merge",
            owner=parent_owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        plan = KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "merge",
                        "sources": [
                            {"knowledge_id": items[0].id, "expected_version": 1},
                            {"knowledge_id": items[1].id, "expected_version": 1},
                        ],
                        "primary_knowledge_id": items[0].id,
                        "target": {
                            "knowledge_key": "merged-topic",
                            "content": "merged organization content",
                        },
                        "summary": "merge sources",
                    }
                ]
            }
        )
        final_stage = await _complete_stage(db, snapshot=snapshot, plan=plan, stage_key="stage-15-cancel-merge")
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id
        primary_id = items[0].id
        secondary_id = items[1].id
        primary_content = items[0].content
        secondary_content = items[1].content
        primary_vector_item_ids = list(items[0].vector_item_ids)
        secondary_vector_item_ids = list(items[1].vector_item_ids)

    publication = await publish_knowledge_organization_plan(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        organization_job_id=parent_id,
        owner=parent_owner,
        snapshot_id=snapshot_id,
        final_stage_id=final_stage_id,
        plan=plan,
    )
    assert len(publication.mutation_job_ids) == 1
    mutation_job_id = publication.mutation_job_ids[0]

    async with session_factory() as db:
        published_mutation = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=mutation_job_id)
        assert published_mutation is not None
        published_mutation.available_at = await get_database_time(db)
        mutation_job = await knowledge_job_crud.try_claim(
            db,
            uid="user-1",
            job_id=mutation_job_id,
            owner=mutation_owner,
            lease_seconds=60,
            enabled_operations=(KnowledgeJobOperation.ORGANIZE_MUTATION,),
            commit=False,
        )
        assert mutation_job is not None
        await db.commit()

    mutation_context = KnowledgeJobExecutionContext(
        job=mutation_job,
        worker_id=mutation_owner,
        session_factory=session_factory,
    )
    execution = await handle_organization_mutation(mutation_context)
    actual_mutation_job_ids = execution.result["mutation_job_ids"]
    assert len(actual_mutation_job_ids) == 2

    async with session_factory() as db:
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        actual_children = [child for child in children if child.id in actual_mutation_job_ids]
        actual_children_by_operation = {child.operation: child for child in actual_children}
        prepared_primary = await db.get(ManagedKnowledgeItem, primary_id)
        prepared_secondary = await db.get(ManagedKnowledgeItem, secondary_id)

    assert len(actual_children) == 2
    assert set(actual_children_by_operation) == {
        KnowledgeJobOperation.MANAGED_UPDATE,
        KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
    }
    managed_update = actual_children_by_operation[KnowledgeJobOperation.MANAGED_UPDATE]
    managed_delete = actual_children_by_operation[KnowledgeJobOperation.MANAGED_DELETE_CLEANUP]
    assert managed_update.status == KnowledgeJobStatus.PENDING
    assert managed_delete.status == KnowledgeJobStatus.PENDING
    assert prepared_primary is not None
    assert prepared_primary.knowledge_key == "merged-topic"
    assert prepared_primary.content == "merged organization content"
    assert prepared_primary.version == 2
    assert prepared_primary.indexed_version == 1
    assert prepared_primary.is_recallable is False
    assert prepared_primary.deleted_at is None
    assert prepared_primary.vector_item_ids == primary_vector_item_ids
    assert prepared_primary.pending_job_id == managed_update.id
    assert prepared_secondary is not None
    assert prepared_secondary.content == secondary_content
    assert prepared_secondary.version == 2
    assert prepared_secondary.indexed_version == 1
    assert prepared_secondary.is_recallable is False
    assert prepared_secondary.deleted_at is not None
    assert prepared_secondary.vector_item_ids == secondary_vector_item_ids
    assert prepared_secondary.pending_job_id == managed_delete.id
    assert prepared_primary.organization_lock_token == organization_lock_token_for_job(parent_id)
    assert prepared_secondary.organization_lock_token == organization_lock_token_for_job(parent_id)

    async with session_factory() as db:
        cancellation = await _request_organization_cancel(db, uid="user-1", job_id=parent_id)
        assert cancellation.accepted is True
        assert cancellation.changed is True
        requested_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        requested_mutation = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=mutation_job_id)
        cancelled_update = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=managed_update.id)
        cancelled_delete = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=managed_delete.id)
        restored_primary = await db.get(ManagedKnowledgeItem, primary_id)
        restored_secondary = await db.get(ManagedKnowledgeItem, secondary_id)

    assert requested_parent is not None
    assert requested_parent.status == KnowledgeJobStatus.RUNNING
    assert requested_parent.cancel_requested_at is not None
    assert requested_mutation is not None
    assert requested_mutation.status == KnowledgeJobStatus.RUNNING
    assert requested_mutation.cancel_requested_at is not None
    assert cancelled_update is not None and cancelled_update.status == KnowledgeJobStatus.CANCELLED
    assert cancelled_delete is not None and cancelled_delete.status == KnowledgeJobStatus.SUCCEEDED
    assert cancelled_delete.active_change_key is None
    assert cancelled_delete.cancel_requested_at is None
    assert cancelled_delete.result is not None
    assert cancelled_delete.result.get("cleanup_skipped") == "organization_rolled_back"
    assert restored_primary is not None
    assert restored_primary.knowledge_key == "topic-0"
    assert restored_primary.content == primary_content
    assert restored_primary.version == 1
    assert restored_primary.indexed_version == 1
    assert restored_primary.is_recallable is True
    assert restored_primary.deleted_at is None
    assert restored_primary.vector_item_ids == primary_vector_item_ids
    assert restored_primary.pending_job_id is None
    assert restored_secondary is not None
    assert restored_secondary.knowledge_key == "topic-1"
    assert restored_secondary.content == secondary_content
    assert restored_secondary.version == 1
    assert restored_secondary.indexed_version == 1
    assert restored_secondary.is_recallable is True
    assert restored_secondary.deleted_at is None
    assert restored_secondary.vector_item_ids == secondary_vector_item_ids
    assert restored_secondary.pending_job_id is None
    assert restored_primary.organization_lock_token == organization_lock_token_for_job(parent_id)
    assert restored_secondary.organization_lock_token == organization_lock_token_for_job(parent_id)

    async with session_factory() as db:
        assert await _mark_organization_job_terminal(
            db,
            uid="user-1",
            job_id=mutation_job_id,
            owner=mutation_owner,
            status=KnowledgeJobStatus.CANCELLED,
        )

    async with session_factory() as db:
        assert await _mark_organization_job_terminal(
            db,
            uid="user-1",
            job_id=parent_id,
            owner=parent_owner,
            status=KnowledgeJobStatus.CANCELLED,
        )

    async with session_factory() as db:
        final_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        final_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        final_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id).order_by(ManagedKnowledgeItem.id))).scalars())

    assert final_parent is not None and final_parent.status == KnowledgeJobStatus.CANCELLED
    assert {child.id for child in final_children} == {mutation_job_id, *actual_mutation_job_ids}
    final_status_by_id = {child.id: child.status for child in final_children}
    assert final_status_by_id[mutation_job_id] == KnowledgeJobStatus.CANCELLED
    assert final_status_by_id[managed_update.id] == KnowledgeJobStatus.CANCELLED
    assert final_status_by_id[managed_delete.id] == KnowledgeJobStatus.SUCCEEDED
    assert all(item.organization_lock_token is None for item in final_items)


@pytest.mark.asyncio
async def test_parent_cancellation_before_mutation_submission_is_fenced(session_factory, monkeypatch):
    parent_owner = "knowledge_organization_publication-cancel-fence-parent"
    mutation_owner = "knowledge_organization_publication-cancel-fence-mutation"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="cancel-fence")
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=2)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="cancel-fence",
            owner=parent_owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        plan = KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "keep",
                        "source": {"knowledge_id": items[0].id, "expected_version": 1},
                        "summary": "keep source",
                    },
                    {
                        "action": "update",
                        "source": {"knowledge_id": items[1].id, "expected_version": 1},
                        "target": {
                            "knowledge_key": "updated-topic",
                            "content": "updated organization content",
                        },
                        "summary": "update source",
                    },
                ]
            }
        )
        final_stage = await _complete_stage(db, snapshot=snapshot, plan=plan, stage_key="stage-15-cancel-fence")
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id
        target_id = items[1].id

    publication = await publish_knowledge_organization_plan(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        organization_job_id=parent_id,
        owner=parent_owner,
        snapshot_id=snapshot_id,
        final_stage_id=final_stage_id,
        plan=plan,
    )
    assert len(publication.mutation_job_ids) == 1
    mutation_job_id = publication.mutation_job_ids[0]

    async with session_factory() as db:
        published_mutation = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=mutation_job_id)
        assert published_mutation is not None
        published_mutation.available_at = await get_database_time(db)
        mutation_job = await knowledge_job_crud.try_claim(
            db,
            uid="user-1",
            job_id=mutation_job_id,
            owner=mutation_owner,
            lease_seconds=60,
            enabled_operations=(KnowledgeJobOperation.ORGANIZE_MUTATION,),
            commit=False,
        )
        assert mutation_job is not None
        await db.commit()

    parent_lock_entered = asyncio.Event()
    release_parent_lock = asyncio.Event()
    parent_lock_seen = False
    original_lock_by_id = knowledge_job_crud.lock_by_id

    async def lock_by_id_with_parent_barrier(db, *, uid: str, job_id: int):
        nonlocal parent_lock_seen
        if job_id == parent_id and not parent_lock_seen:
            parent_lock_seen = True
            parent_lock_entered.set()
            await release_parent_lock.wait()
        return await original_lock_by_id(db, uid=uid, job_id=job_id)

    monkeypatch.setattr(knowledge_job_crud, "lock_by_id", lock_by_id_with_parent_barrier)
    mutation_context = KnowledgeJobExecutionContext(
        job=mutation_job,
        worker_id=mutation_owner,
        session_factory=session_factory,
    )
    execution_task = asyncio.create_task(handle_organization_mutation(mutation_context))
    try:
        await asyncio.wait_for(parent_lock_entered.wait(), timeout=1)
        async with session_factory() as db:
            cancellation = await _request_organization_cancel(db, uid="user-1", job_id=parent_id)
        assert cancellation.accepted is True
        assert cancellation.changed is True
        release_parent_lock.set()
        with pytest.raises(KnowledgeJobCancelledError):
            await asyncio.wait_for(execution_task, timeout=1)
    finally:
        release_parent_lock.set()
        if not execution_task.done():
            execution_task.cancel()
        await asyncio.gather(execution_task, return_exceptions=True)

    async with session_factory() as db:
        current_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        current_mutation = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=mutation_job_id)
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        current_target = await db.get(ManagedKnowledgeItem, target_id)
        revisions = list(
            (
                await db.execute(
                    select(ManagedKnowledgeRevision)
                    .where(
                        ManagedKnowledgeRevision.knowledge_base_id == knowledge_base_id,
                        ManagedKnowledgeRevision.knowledge_id == target_id,
                    )
                    .order_by(ManagedKnowledgeRevision.version)
                )
            ).scalars()
        )

    assert current_parent is not None
    assert current_parent.status == KnowledgeJobStatus.RUNNING
    assert current_parent.cancel_requested_at is not None
    assert current_mutation is not None
    assert current_mutation.status == KnowledgeJobStatus.RUNNING
    assert current_mutation.cancel_requested_at is not None
    assert [child.operation for child in children] == [KnowledgeJobOperation.ORGANIZE_MUTATION]
    assert all(
        child.operation
        not in {
            KnowledgeJobOperation.MANAGED_UPDATE,
            KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
        }
        for child in children
    )
    assert current_target is not None
    assert current_target.content == "organization content 1"
    assert current_target.version == 1
    assert current_target.indexed_version == 1
    assert current_target.is_recallable is True
    assert current_target.vector_item_ids == ["vector-topic-1"]
    assert current_target.pending_job_id is None
    assert current_target.organization_lock_token == organization_lock_token_for_job(parent_id)
    assert [(revision.version, revision.operation) for revision in revisions] == [
        (1, ManagedKnowledgeRevisionOperation.CREATE),
    ]


@pytest.mark.asyncio
async def test_publication_cancel_barrier_rejects_plan_without_mutation_children(
    session_factory,
    monkeypatch,
):
    owner = "knowledge_organization_publication-publication-cancel-barrier"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="publication-cancel-barrier")
        item = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="publication-cancel-topic",
            content="publication cancel content",
        )
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="publication-cancel-barrier",
            owner=owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        plan = KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "update",
                        "source": {"knowledge_id": item.id, "expected_version": 1},
                        "target": {
                            "knowledge_key": "publication-cancelled-topic",
                            "content": "publication cancellation content",
                        },
                        "summary": "cancel publication",
                    }
                ]
            }
        )
        final_stage = await _complete_stage(
            db,
            snapshot=snapshot,
            plan=plan,
            stage_key="stage-15-publication-cancel-barrier",
        )
        locked_item = await db.get(ManagedKnowledgeItem, item.id)
        assert locked_item is not None
        await db.refresh(locked_item)
        assert locked_item.organization_lock_token == organization_lock_token_for_job(parent.id)
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    parent_lock_entered = asyncio.Event()
    release_parent_lock = asyncio.Event()
    parent_lock_seen = False
    original_lock_by_id = knowledge_job_crud.lock_by_id

    async def lock_by_id_with_parent_barrier(db, *, uid: str, job_id: int):
        nonlocal parent_lock_seen
        if job_id == parent_id and not parent_lock_seen:
            parent_lock_seen = True
            parent_lock_entered.set()
            await release_parent_lock.wait()
        return await original_lock_by_id(db, uid=uid, job_id=job_id)

    monkeypatch.setattr(knowledge_job_crud, "lock_by_id", lock_by_id_with_parent_barrier)
    publication_task = asyncio.create_task(
        publish_knowledge_organization_plan(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            organization_job_id=parent_id,
            owner=owner,
            snapshot_id=snapshot_id,
            final_stage_id=final_stage_id,
            plan=plan,
        )
    )
    try:
        await asyncio.wait_for(parent_lock_entered.wait(), timeout=1)
        async with session_factory() as db:
            cancellation = await _request_organization_cancel(db, uid="user-1", job_id=parent_id)
        assert cancellation.accepted is True
        assert cancellation.changed is True
        release_parent_lock.set()
        with pytest.raises(KnowledgeOrganizationPlanStaleError):
            await asyncio.wait_for(publication_task, timeout=1)
    finally:
        release_parent_lock.set()
        if not publication_task.done():
            publication_task.cancel()
        await asyncio.gather(publication_task, return_exceptions=True)

    async with session_factory() as db:
        assert await _mark_organization_job_terminal(
            db,
            uid="user-1",
            job_id=parent_id,
            owner=owner,
            status=KnowledgeJobStatus.CANCELLED,
        )
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        snapshot_items = list((await db.execute(select(KnowledgeOrganizationSnapshotItem).where(KnowledgeOrganizationSnapshotItem.snapshot_id == snapshot_id))).scalars())
        current_item = await db.get(ManagedKnowledgeItem, item.id)

    assert all(child.operation != KnowledgeJobOperation.ORGANIZE_MUTATION for child in children)
    assert len(snapshot_items) == 1
    assert current_item is not None
    assert current_item.organization_lock_token is None


@pytest.mark.asyncio
async def test_snapshot_cancel_barrier_rejects_snapshot_without_lock(
    session_factory,
    monkeypatch,
):
    owner = "knowledge_organization_publication-snapshot-cancel-barrier"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="snapshot-cancel-barrier")
        item = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="snapshot-cancel-topic",
            content="snapshot cancel content",
        )
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="snapshot-cancel-barrier",
            owner=owner,
        )
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        knowledge_id = item.id

    parent_lock_entered = asyncio.Event()
    release_parent_lock = asyncio.Event()
    parent_lock_seen = False
    original_lock_by_id = knowledge_job_crud.lock_by_id

    async def lock_by_id_with_parent_barrier(db, *, uid: str, job_id: int):
        nonlocal parent_lock_seen
        if job_id == parent_id and not parent_lock_seen:
            parent_lock_seen = True
            parent_lock_entered.set()
            await release_parent_lock.wait()
        return await original_lock_by_id(db, uid=uid, job_id=job_id)

    monkeypatch.setattr(knowledge_job_crud, "lock_by_id", lock_by_id_with_parent_barrier)

    async def run_snapshot():
        async with session_factory() as db:
            return await create_knowledge_organization_snapshot(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base_id,
                organization_job_id=parent_id,
            )

    snapshot_task = asyncio.create_task(run_snapshot())
    try:
        await asyncio.wait_for(parent_lock_entered.wait(), timeout=1)
        async with session_factory() as db:
            cancellation = await _request_organization_cancel(db, uid="user-1", job_id=parent_id)
        assert cancellation.accepted is True
        assert cancellation.changed is True
        release_parent_lock.set()
        with pytest.raises(ManagedKnowledgeConflictError):
            await asyncio.wait_for(snapshot_task, timeout=1)
    finally:
        release_parent_lock.set()
        if not snapshot_task.done():
            snapshot_task.cancel()
        await asyncio.gather(snapshot_task, return_exceptions=True)

    async with session_factory() as db:
        snapshots = list(
            (
                await db.execute(
                    select(KnowledgeOrganizationSnapshot).where(
                        KnowledgeOrganizationSnapshot.uid == "user-1",
                        KnowledgeOrganizationSnapshot.knowledge_base_id == knowledge_base_id,
                    )
                )
            ).scalars()
        )
        current_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        current_item = await db.get(ManagedKnowledgeItem, knowledge_id)

    assert snapshots == []
    assert current_parent is not None
    assert current_parent.cancel_requested_at is not None
    assert current_item is not None
    assert current_item.organization_lock_token is None
