from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select

from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import organization_lock_token_for_job
from app.core.knowledge.organization import create_knowledge_organization_snapshot
from app.core.knowledge.organization_publication import (
    KnowledgeOrganizationPlanStaleError,
    load_knowledge_organization_mutation_item,
    publish_knowledge_organization_plan,
)
from app.core.knowledge.organization_run import (
    get_knowledge_organization_job,
    list_knowledge_organization_jobs,
    submit_auto_knowledge_organization,
    submit_knowledge_organization,
)
from app.core.knowledge.organization_types import KnowledgeOrganizationPlan
from app.core.knowledge_jobs.consumer import KnowledgeJobConsumer
from app.core.knowledge_jobs.executor import (
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutionResult,
    KnowledgeJobExecutor,
)
from app.core.knowledge_jobs.handlers import handle_organization_mutation
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    ManagedKnowledgeItem,
)
from app.providers.database.time import get_database_time
from tests.integration.knowledge.knowledge_publication_support import (
    _add_item,
    _complete_stage,
    _create_managed_container,
    _create_pending_organization_job,
    _create_running_organization_job,
    _full_plan,
    _mark_organization_job_terminal,
    _request_organization_cancel,
    _seed_items,
    _zero_progress,
)
from tests.integration.knowledge.knowledge_publication_support import _session_factory as _session_factory


@pytest.mark.asyncio
async def test_publication_validates_complete_plan_and_creates_mutation_children(session_factory):
    owner = "knowledge_organization_publication-publication-owner"
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=6)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="publication",
            owner=owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        plan = _full_plan([item.id for item in items])
        final_stage = await _complete_stage(db, snapshot=snapshot, plan=plan)
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    result = await publish_knowledge_organization_plan(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        organization_job_id=parent_id,
        owner=owner,
        snapshot_id=snapshot_id,
        final_stage_id=final_stage_id,
        plan=plan,
    )

    assert len(result.mutation_job_ids) == 2
    async with session_factory() as db:
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        assert len(children) == 2
        assert {child.operation for child in children} == {KnowledgeJobOperation.ORGANIZE_MUTATION}
        assert {child.status for child in children} == {KnowledgeJobStatus.PENDING}
        assert {child.id for child in children} == set(result.mutation_job_ids)
        database_time = await get_database_time(db)
        assert all(child.available_at <= database_time for child in children)

        by_action = {child.payload["action"]: child for child in children}
        update_child = by_action["update"]
        merge_child = by_action["merge"]
        assert update_child.parent_job_id == parent_id
        assert update_child.knowledge_id == items[1].id
        assert update_child.expected_version == 1
        assert update_child.payload == {
            "snapshot_id": snapshot_id,
            "stage_id": final_stage_id,
            "plan_item_index": 1,
            "action": "update",
            "sources": [{"knowledge_id": items[1].id, "expected_version": 1}],
        }
        assert merge_child.parent_job_id == parent_id
        assert merge_child.knowledge_id == items[2].id
        assert merge_child.expected_version == 1
        assert merge_child.payload == {
            "snapshot_id": snapshot_id,
            "stage_id": final_stage_id,
            "plan_item_index": 2,
            "action": "merge",
            "sources": [
                {"knowledge_id": items[2].id, "expected_version": 1},
                {"knowledge_id": items[3].id, "expected_version": 1},
            ],
            "primary_knowledge_id": items[2].id,
        }
        assert update_child.active_change_key != merge_child.active_change_key
        assert (
            await load_knowledge_organization_mutation_item(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base.id,
                snapshot_id=snapshot_id,
                stage_id=final_stage_id,
                plan_item_index=1,
            )
            == plan.items[1]
        )
        assert (
            await load_knowledge_organization_mutation_item(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base.id,
                snapshot_id=snapshot_id,
                stage_id=final_stage_id,
                plan_item_index=2,
            )
            == plan.items[2]
        )

        current_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base.id).order_by(ManagedKnowledgeItem.id))).scalars())
        assert [item.organization_lock_token for item in current_items] == [organization_lock_token_for_job(parent_id)] * 6
        assert all(source.expected_version == 1 for plan_item in plan.items for source in ((plan_item.source,) if plan_item.action in {"keep", "update"} else plan_item.sources) if plan_item.action in {"update", "merge"})


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["version", "maintenance", "lock"])
async def test_publication_rejects_changed_source_state_without_children(session_factory, change: str):
    owner = f"knowledge_organization_publication-stale-{change}"
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix=change)
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=6)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix=f"stale-{change}",
            owner=owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        plan = _full_plan([item.id for item in items])
        final_stage = await _complete_stage(db, snapshot=snapshot, plan=plan, stage_key=f"stage-15-{change}")
        changed = await db.get(ManagedKnowledgeItem, items[1].id)
        assert changed is not None
        if change == "version":
            changed.version = 2
            changed.indexed_version = 2
        elif change == "maintenance":
            changed.llm_maintainable = False
        else:
            changed.organization_lock_token = "job:another-organization"
        await db.commit()
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    with pytest.raises(KnowledgeOrganizationPlanStaleError) as exc_info:
        await publish_knowledge_organization_plan(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent_id,
            owner=owner,
            snapshot_id=snapshot_id,
            final_stage_id=final_stage_id,
            plan=plan,
        )
    assert exc_info.value.message == "ERR_KNOWLEDGE_ORGANIZATION_PLAN_STALE"

    async with session_factory() as db:
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        assert children == []


@pytest.mark.asyncio
@pytest.mark.parametrize("terminal", [KnowledgeJobStatus.FAILED, KnowledgeJobStatus.CANCELLED])
async def test_failed_or_cancelled_parent_cancels_pending_children_and_releases_locks(
    session_factory,
    terminal: KnowledgeJobStatus,
):
    owner = f"knowledge_organization_publication-terminal-{terminal.value}"
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix=terminal.value)
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=2)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix=f"terminal-{terminal.value}",
            owner=owner,
        )
        await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        child_ids = [
            (
                await knowledge_job_crud.create(
                    db,
                    uid="user-1",
                    parent_job_id=parent.id,
                    operation=KnowledgeJobOperation.ORGANIZE_MUTATION,
                    dedupe_key=f"knowledge_organization_publication-child:{parent.id}:{index}",
                    request_hash=hashlib.sha256(f"child-{index}".encode()).hexdigest(),
                    active_change_key=f"knowledge_organization_publication-child-active:{parent.id}:{index}",
                    status=KnowledgeJobStatus.PENDING,
                    knowledge_base_id=knowledge_base.id,
                    knowledge_id=item.id,
                    expected_version=1,
                    payload={"action": "update", "sources": []},
                    available_at=await get_database_time(db),
                    max_attempts=1,
                    commit=False,
                )
            )[0].id
            for index, item in enumerate(items)
        ]
        await db.commit()
        assert all(child_id is not None for child_id in child_ids)

        if terminal == KnowledgeJobStatus.FAILED:
            assert await _mark_organization_job_terminal(
                db,
                uid="user-1",
                job_id=parent.id,
                owner=owner,
                status=KnowledgeJobStatus.FAILED,
                error="parent organization failed",
            )
        else:
            cancellation = await _request_organization_cancel(db, uid="user-1", job_id=parent.id)
            assert cancellation.accepted is True
            assert cancellation.changed is True
            assert await _mark_organization_job_terminal(
                db,
                uid="user-1",
                job_id=parent.id,
                owner=owner,
                status=KnowledgeJobStatus.CANCELLED,
            )

        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent.id)
        current_items = [await db.get(ManagedKnowledgeItem, item.id) for item in items]
        current_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent.id)
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base.id)

    assert current_parent is not None and current_parent.status == terminal
    assert [child.id for child in children] == child_ids
    assert all(child.status == KnowledgeJobStatus.CANCELLED for child in children)
    assert all(child.finished_at is not None for child in children)
    assert all(item is not None and item.organization_lock_token is None for item in current_items)
    assert current_knowledge_base is not None and current_knowledge_base.organization_error is not None


@pytest.mark.asyncio
async def test_successful_parent_waits_for_all_children_before_releasing_locks(session_factory):
    owner = "knowledge_organization_publication-success-parent"
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="success")
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=2)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="success",
            owner=owner,
        )
        await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        for index, item in enumerate(items):
            child, created = await knowledge_job_crud.create(
                db,
                uid="user-1",
                parent_job_id=parent.id,
                operation=KnowledgeJobOperation.ORGANIZE_MUTATION,
                dedupe_key=f"knowledge_organization_publication-success-child:{parent.id}:{index}",
                request_hash=hashlib.sha256(f"success-child-{index}".encode()).hexdigest(),
                active_change_key=f"knowledge_organization_publication-success-child-active:{parent.id}:{index}",
                status=KnowledgeJobStatus.PENDING,
                knowledge_base_id=knowledge_base.id,
                knowledge_id=item.id,
                expected_version=1,
                payload={"action": "update", "sources": []},
                available_at=await get_database_time(db),
                max_attempts=1,
                commit=False,
            )
            assert created is True and child.id is not None
        await db.commit()
        parent_id = parent.id

    async with session_factory() as db:
        assert not await _mark_organization_job_terminal(
            db,
            uid="user-1",
            job_id=parent_id,
            owner=owner,
            status=KnowledgeJobStatus.SUCCEEDED,
        )
        current_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        current_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base.id))).scalars())
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base.id)
    assert current_parent is not None and current_parent.status == KnowledgeJobStatus.RUNNING
    assert all(item.organization_lock_token == organization_lock_token_for_job(parent_id) for item in current_items)
    assert current_knowledge_base is not None and current_knowledge_base.organization_last_job_id is None

    for index in range(2):
        async with session_factory() as db:
            children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
            child = children[index]
            child_owner = f"knowledge_organization_publication-child-worker-{index}"
            claimed = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=child.id,
                owner=child_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.ORGANIZE_MUTATION,),
                commit=False,
            )
            assert claimed is not None
            assert await _mark_organization_job_terminal(
                db,
                uid="user-1",
                job_id=child.id,
                owner=child_owner,
                status=KnowledgeJobStatus.SUCCEEDED,
            )
        if index == 0:
            async with session_factory() as db:
                partially_released = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base.id))).scalars())
                partial_knowledge_base = await db.get(KnowledgeBase, knowledge_base.id)
            assert all(item.organization_lock_token == organization_lock_token_for_job(parent_id) for item in partially_released)
            assert partial_knowledge_base is not None and partial_knowledge_base.organization_last_job_id is None

    async with session_factory() as db:
        assert await _mark_organization_job_terminal(
            db,
            uid="user-1",
            job_id=parent_id,
            owner=owner,
            status=KnowledgeJobStatus.SUCCEEDED,
        )

    async with session_factory() as db:
        released_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base.id))).scalars())
        released_knowledge_base = await db.get(KnowledgeBase, knowledge_base.id)
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
    assert all(item.organization_lock_token is None for item in released_items)
    assert released_knowledge_base is not None
    assert released_knowledge_base.organization_last_job_id == parent_id
    assert released_knowledge_base.organization_last_run_at is not None
    assert released_knowledge_base.organization_error is None
    assert all(child.status == KnowledgeJobStatus.SUCCEEDED for child in children)


@pytest.mark.asyncio
async def test_organization_parent_cannot_succeed_with_failed_child(session_factory):
    owner = "knowledge_organization_publication-failed-child-parent"
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="failed-child-parent")
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="failed-child-parent",
            owner=owner,
        )
        child = KnowledgeJob(
            uid="user-1",
            parent_job_id=parent.id,
            operation=KnowledgeJobOperation.ORGANIZE_MUTATION,
            dedupe_key="knowledge_organization_publication-failed-child-parent-child",
            request_hash="f" * 64,
            status=KnowledgeJobStatus.FAILED,
            knowledge_base_id=knowledge_base.id,
            error="publication failed",
        )
        db.add(child)
        await db.commit()

        changed = await knowledge_job_crud.mark_succeeded(
            db,
            uid="user-1",
            job_id=parent.id,
            owner=owner,
            result={},
        )
        current = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent.id)

    assert changed is False
    assert current is not None
    assert current.status == KnowledgeJobStatus.RUNNING
    assert current.active_change_key is not None


@pytest.mark.asyncio
async def test_consumer_waits_for_failed_organization_child_before_failing_parent(session_factory):
    owner = "knowledge_organization_publication-consumer-parent-worker"
    child_owner = "knowledge_organization_publication-consumer-child-worker"
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="consumer-failure")
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="consumer-failure",
            owner=owner,
        )
        child, created = await knowledge_job_crud.create(
            db,
            uid="user-1",
            parent_job_id=parent.id,
            operation=KnowledgeJobOperation.ORGANIZE_MUTATION,
            dedupe_key=f"knowledge_organization_publication-consumer-child:{parent.id}",
            request_hash=hashlib.sha256(b"knowledge_organization_publication-consumer-child").hexdigest(),
            active_change_key=f"knowledge_organization_publication-consumer-child-active:{parent.id}",
            status=KnowledgeJobStatus.PENDING,
            knowledge_base_id=knowledge_base.id,
            knowledge_id=1,
            expected_version=1,
            payload={"action": "update", "sources": []},
            available_at=await get_database_time(db),
            max_attempts=1,
            commit=False,
        )
        assert created is True and child.id is not None
        await db.commit()

    handler_started = asyncio.Event()

    async def organize(_context):
        handler_started.set()
        return KnowledgeJobExecutionResult(result={"prepared": True}, wait_for_children=True)

    consumer = KnowledgeJobConsumer(
        KnowledgeJobExecutor(
            {KnowledgeJobOperation.MANUAL_ORGANIZE: organize},
            session_factory=session_factory,
        ),
        session_factory=session_factory,
        poll_interval_seconds=0.01,
    )
    execution_task = asyncio.create_task(consumer._execute(parent, owner))
    try:
        await asyncio.wait_for(handler_started.wait(), timeout=1)
        await asyncio.sleep(0.01)

        async with session_factory() as db:
            current_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent.id)
        assert current_parent is not None and current_parent.status == KnowledgeJobStatus.RUNNING
        assert not execution_task.done()

        async with session_factory() as db:
            claimed_child = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=child.id,
                owner=child_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.ORGANIZE_MUTATION,),
                commit=False,
            )
            assert claimed_child is not None
            assert await _mark_organization_job_terminal(
                db,
                uid="user-1",
                job_id=child.id,
                owner=child_owner,
                status=KnowledgeJobStatus.FAILED,
                error="organization mutation failed",
            )

        await asyncio.wait_for(execution_task, timeout=1)
    finally:
        if not execution_task.done():
            execution_task.cancel()
            await asyncio.gather(execution_task, return_exceptions=True)

    async with session_factory() as db:
        current_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent.id)
    assert current_parent is not None and current_parent.status == KnowledgeJobStatus.FAILED


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("recently_organized", "changed_item_count", "submitted"),
    [(False, 5, True), (False, 4, False), (True, 5, True), (True, 4, False)],
)
async def test_auto_organization_obeys_threshold_without_interval(
    session_factory,
    recently_organized: bool,
    changed_item_count: int,
    submitted: bool,
):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix=f"auto-{recently_organized}-{changed_item_count}")
        previous_job_id = None
        previous_run_at = None
        if recently_organized:
            previous_job_id = 901
            previous_run_at = await get_database_time(db) - timedelta(seconds=1)
            knowledge_base.organization_last_job_id = previous_job_id
            knowledge_base.organization_last_run_at = previous_run_at
            await db.commit()
        await _seed_items(db, knowledge_base_id=knowledge_base.id, count=changed_item_count)
        job = await submit_auto_knowledge_organization(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )
        parent_jobs = list(
            (
                await db.execute(
                    select(KnowledgeJob).where(
                        KnowledgeJob.knowledge_base_id == knowledge_base.id,
                        KnowledgeJob.parent_job_id.is_(None),
                        KnowledgeJob.operation.in_([KnowledgeJobOperation.AUTO_ORGANIZE, KnowledgeJobOperation.MANUAL_ORGANIZE]),
                    )
                )
            ).scalars()
        )
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base.id)

    assert (job is not None) is submitted
    assert len(parent_jobs) == (1 if submitted else 0)
    assert current_knowledge_base is not None
    if submitted:
        assert job is not None
        assert job.operation == KnowledgeJobOperation.AUTO_ORGANIZE
        assert current_knowledge_base.organization_last_job_id == job.id
        assert current_knowledge_base.organization_last_run_at is not None
        if previous_run_at is not None:
            assert current_knowledge_base.organization_last_run_at > previous_run_at
    else:
        assert current_knowledge_base.organization_last_job_id == previous_job_id
        assert current_knowledge_base.organization_last_run_at == previous_run_at


@pytest.mark.asyncio
async def test_repeated_submission_is_idempotent_and_busy_auto_submission_does_not_duplicate(session_factory):
    async with session_factory() as db:
        auto_base = await _create_managed_container(db, suffix="auto-repeat")
        await _seed_items(db, knowledge_base_id=auto_base.id, count=5)
        first_auto = await submit_auto_knowledge_organization(
            db,
            uid="user-1",
            knowledge_base_id=auto_base.id,
        )
        second_auto = await submit_auto_knowledge_organization(
            db,
            uid="user-1",
            knowledge_base_id=auto_base.id,
        )
        assert first_auto is not None
        assert second_auto is None

        busy_base = await _create_managed_container(db, suffix="busy")
        await _seed_items(db, knowledge_base_id=busy_base.id, count=5)
        busy_base_id = busy_base.id
        first_deduped = await submit_knowledge_organization(
            db,
            uid="user-1",
            knowledge_base_id=busy_base_id,
            source="auto",
            dedupe_key="knowledge_organization_publication-same-request",
        )
        second_deduped = await submit_knowledge_organization(
            db,
            uid="user-1",
            knowledge_base_id=busy_base_id,
            source="auto",
            dedupe_key="knowledge_organization_publication-same-request",
        )
        busy_auto = await submit_auto_knowledge_organization(
            db,
            uid="user-1",
            knowledge_base_id=busy_base_id,
        )
        busy_jobs = list(
            (
                await db.execute(
                    select(KnowledgeJob).where(
                        KnowledgeJob.knowledge_base_id == busy_base_id,
                        KnowledgeJob.parent_job_id.is_(None),
                    )
                )
            ).scalars()
        )

    assert first_deduped.id == second_deduped.id
    assert busy_auto is None
    assert len(busy_jobs) == 1


@pytest.mark.asyncio
async def test_job_detail_and_list_expose_snapshot_progress_and_zero_missing_snapshot_progress(session_factory):
    async with session_factory() as db:
        with_snapshot_base = await _create_managed_container(db, suffix="views")
        item = await _add_item(
            db,
            knowledge_base_id=with_snapshot_base.id,
            knowledge_key="view-topic",
            content="view content",
        )
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=with_snapshot_base.id,
            suffix="views",
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=with_snapshot_base.id,
            organization_job_id=parent.id,
        )
        plan = KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "keep",
                        "source": {"knowledge_id": item.id, "expected_version": 1},
                        "summary": "keep view topic",
                    }
                ]
            }
        )
        await _complete_stage(db, snapshot=snapshot, plan=plan, stage_key="stage-15-view")
        parent.payload = {"source": "knowledge_organization_publication", "snapshot_id": snapshot.id}
        await db.commit()

        missing_snapshot_base = await _create_managed_container(db, suffix="missing-view")
        missing_job = await _create_pending_organization_job(
            db,
            knowledge_base_id=missing_snapshot_base.id,
            suffix="missing-view",
        )
        snapshot_parent_id = parent.id
        missing_job_id = missing_job.id
        with_snapshot_base_id = with_snapshot_base.id
        missing_snapshot_base_id = missing_snapshot_base.id

    async with session_factory() as db:
        snapshot_detail = await get_knowledge_organization_job(
            db,
            uid="user-1",
            job_id=snapshot_parent_id,
        )
        snapshot_list = await list_knowledge_organization_jobs(
            db,
            uid="user-1",
            knowledge_base_id=with_snapshot_base_id,
        )
        missing_detail = await get_knowledge_organization_job(
            db,
            uid="user-1",
            job_id=missing_job_id,
        )
        missing_list = await list_knowledge_organization_jobs(
            db,
            uid="user-1",
            knowledge_base_id=missing_snapshot_base_id,
        )

    expected_progress = {
        "stage_count": 1,
        "completed_stage_count": 1,
        "running_stage_count": 0,
        "failed_stage_count": 0,
        "invalidated_stage_count": 0,
        "expected_fragment_count": 1,
        "succeeded_fragment_count": 1,
        "completed_fragment_count": 1,
        "invalidated_fragment_count": 0,
    }
    assert snapshot_detail["snapshot_id"] == snapshot.id
    assert snapshot_detail["organization_progress"] == expected_progress
    assert snapshot_list["total"] == 1
    assert snapshot_list["items"][0]["snapshot_id"] == snapshot.id
    assert snapshot_list["items"][0]["organization_progress"] == expected_progress

    assert missing_detail["snapshot_id"] is None
    assert missing_detail["organization_progress"] == _zero_progress()
    assert missing_list["total"] == 1
    assert missing_list["items"][0]["snapshot_id"] is None
    assert missing_list["items"][0]["organization_progress"] == _zero_progress()


@pytest.mark.asyncio
async def test_job_view_counts_only_fully_published_organization_mutations(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="publication-count")
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="publication-count",
        )

        mutation = KnowledgeJob(
            uid="user-1",
            parent_job_id=parent.id,
            operation=KnowledgeJobOperation.ORGANIZE_MUTATION,
            dedupe_key="knowledge_organization_publication-publication-count-mutation",
            request_hash="a" * 64,
            status=KnowledgeJobStatus.SUCCEEDED,
            knowledge_base_id=knowledge_base.id,
            result={},
        )
        db.add(mutation)
        await db.flush()

        publication = KnowledgeJob(
            uid="user-1",
            parent_job_id=parent.id,
            operation=KnowledgeJobOperation.MANAGED_UPDATE,
            dedupe_key="knowledge_organization_publication-publication-count-update",
            request_hash="b" * 64,
            status=KnowledgeJobStatus.PENDING,
            knowledge_base_id=knowledge_base.id,
        )
        cleanup = KnowledgeJob(
            uid="user-1",
            parent_job_id=parent.id,
            operation=KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
            dedupe_key="knowledge_organization_publication-publication-count-delete",
            request_hash="e" * 64,
            status=KnowledgeJobStatus.SUCCEEDED,
            knowledge_base_id=knowledge_base.id,
        )
        db.add_all([publication, cleanup])
        await db.flush()
        mutation.result = {"mutation_job_ids": [publication.id, cleanup.id]}
        await db.commit()
        parent_id = parent.id
        publication_id = publication.id

    async with session_factory() as db:
        before_publication = await get_knowledge_organization_job(
            db,
            uid="user-1",
            job_id=parent_id,
        )
        publication = await knowledge_job_crud.get_by_id(
            db,
            uid="user-1",
            job_id=publication_id,
        )
        assert publication is not None
        publication.status = KnowledgeJobStatus.SUCCEEDED
        await db.commit()
        after_publication = await get_knowledge_organization_job(
            db,
            uid="user-1",
            job_id=parent_id,
        )

    assert before_publication["publication_success_count"] == 0
    assert after_publication["publication_success_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("collision_field", ["knowledge_key", "content"])
async def test_merge_releases_secondary_unique_fields_before_primary_update(
    session_factory,
    collision_field: str,
):
    parent_owner = f"knowledge_organization_publication-merge-unique-{collision_field}-parent"
    mutation_owner = f"knowledge_organization_publication-merge-unique-{collision_field}-mutation"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix=f"merge-unique-{collision_field}")
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=2)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix=f"merge-unique-{collision_field}",
            owner=parent_owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        target = {
            "knowledge_key": items[1].knowledge_key if collision_field == "knowledge_key" else "merged-topic",
            "content": items[1].content if collision_field == "content" else "merged organization content",
        }
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
                        "target": target,
                        "summary": "merge sources with secondary identity",
                    }
                ]
            }
        )
        final_stage = await _complete_stage(db, snapshot=snapshot, plan=plan, stage_key=f"stage-15-merge-unique-{collision_field}")
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id
        primary_id = items[0].id
        secondary_id = items[1].id
        secondary_content = items[1].content

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
    managed_job_ids = execution.result["mutation_job_ids"]
    assert len(managed_job_ids) == 2

    async with session_factory() as db:
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        managed_children = [child for child in children if child.id in managed_job_ids]
        children_by_operation = {child.operation: child for child in managed_children}
        primary = await db.get(ManagedKnowledgeItem, primary_id)
        secondary = await db.get(ManagedKnowledgeItem, secondary_id)

    assert len(managed_children) == 2
    assert set(children_by_operation) == {
        KnowledgeJobOperation.MANAGED_UPDATE,
        KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
    }
    managed_update = children_by_operation[KnowledgeJobOperation.MANAGED_UPDATE]
    managed_delete = children_by_operation[KnowledgeJobOperation.MANAGED_DELETE_CLEANUP]
    assert managed_update.status == KnowledgeJobStatus.PENDING
    assert managed_delete.status == KnowledgeJobStatus.PENDING
    assert managed_update.parent_job_id == parent_id
    assert managed_delete.parent_job_id == parent_id
    assert managed_update.knowledge_id == primary_id
    assert managed_update.expected_version == 2
    assert managed_delete.knowledge_id == secondary_id
    assert managed_delete.expected_version == 2

    assert primary is not None
    assert primary.knowledge_key == target["knowledge_key"]
    assert primary.content == target["content"]
    assert primary.content_hash == hashlib.sha256(target["content"].encode("utf-8")).hexdigest()
    assert primary.version == 2
    assert primary.deleted_at is None
    assert primary.pending_job_id == managed_update.id
    assert secondary is not None
    assert secondary.content == secondary_content
    assert secondary.version == 2
    assert secondary.deleted_at is not None
    assert secondary.is_recallable is False
    assert secondary.pending_job_id == managed_delete.id
    assert secondary.knowledge_key is None
    assert secondary.content_hash is None


@pytest.mark.asyncio
async def test_waiting_organization_parent_does_not_block_single_worker_child_execution(
    session_factory,
):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="single-worker")
        parent, created = await knowledge_job_crud.create(
            db,
            uid="user-1",
            operation=KnowledgeJobOperation.MANUAL_ORGANIZE,
            dedupe_key="knowledge_organization_publication-single-worker-parent",
            request_hash="c" * 64,
            active_change_key=f"kb-organization:{knowledge_base.id}",
            knowledge_base_id=knowledge_base.id,
            payload={},
            available_at=await get_database_time(db),
        )
        assert created is True
        parent_id = parent.id
        knowledge_base_id = knowledge_base.id

    async def parent_handler(context: KnowledgeJobExecutionContext) -> KnowledgeJobExecutionResult:
        async with session_factory() as db:
            available_at = await get_database_time(db)
            for index in range(3):
                unrelated, created = await knowledge_job_crud.create(
                    db,
                    uid=context.job.uid,
                    operation=KnowledgeJobOperation.MANAGED_CREATE,
                    dedupe_key=f"knowledge_organization_publication-single-worker-unrelated:{context.job.id}:{index}",
                    request_hash=f"{index + 1}" * 64,
                    knowledge_base_id=context.job.knowledge_base_id,
                    payload={},
                    available_at=available_at,
                    commit=False,
                )
                assert created is True
                assert unrelated.id is not None
            child, created = await knowledge_job_crud.create(
                db,
                uid=context.job.uid,
                operation=KnowledgeJobOperation.ORGANIZE_MUTATION,
                dedupe_key=f"knowledge_organization_publication-single-worker-child:{context.job.id}",
                request_hash="d" * 64,
                parent_job_id=context.job.id,
                knowledge_base_id=context.job.knowledge_base_id,
                payload={},
                available_at=available_at,
                commit=False,
            )
            assert created is True
            assert child.id is not None
            await db.commit()
        return KnowledgeJobExecutionResult(result={}, wait_for_children=True)

    async def child_handler(_context: KnowledgeJobExecutionContext) -> dict:
        return {}

    async def unrelated_handler(_context: KnowledgeJobExecutionContext) -> dict:
        return {}

    executor = KnowledgeJobExecutor(
        {
            KnowledgeJobOperation.MANUAL_ORGANIZE: parent_handler,
            KnowledgeJobOperation.ORGANIZE_MUTATION: child_handler,
            KnowledgeJobOperation.MANAGED_CREATE: unrelated_handler,
        },
        session_factory=session_factory,
    )
    consumer = KnowledgeJobConsumer(
        executor,
        session_factory=session_factory,
        poll_interval_seconds=0.01,
        max_concurrency=1,
    )
    consumer.start()
    try:

        async def wait_until_terminal() -> tuple[KnowledgeJob, list[KnowledgeJob]]:
            while True:
                async with session_factory() as db:
                    current_parent = await knowledge_job_crud.get_by_id(
                        db,
                        uid="user-1",
                        job_id=parent_id,
                    )
                    children = await knowledge_job_crud.list_children(
                        db,
                        uid="user-1",
                        parent_job_id=parent_id,
                    )
                if current_parent is not None and current_parent.status == KnowledgeJobStatus.SUCCEEDED:
                    return current_parent, children
                await asyncio.sleep(0.01)

        final_parent, children = await asyncio.wait_for(wait_until_terminal(), timeout=1.0)
    finally:
        await consumer.stop()

    assert final_parent.knowledge_base_id == knowledge_base_id
    assert len(children) == 1
    assert children[0].operation == KnowledgeJobOperation.ORGANIZE_MUTATION
    assert children[0].status == KnowledgeJobStatus.SUCCEEDED
