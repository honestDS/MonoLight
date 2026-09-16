from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.knowledge.managed import organization_lock_token_for_job
from app.core.crud.knowledge.organization import (
    knowledge_organization_fragment_crud,
    knowledge_organization_stage_crud,
)
from app.core.embedding.common import EmbeddingRuntimeConfig
from app.core.knowledge.errors import ManagedKnowledgeConflictError
from app.core.knowledge.managed import build_managed_knowledge_snapshot
from app.core.knowledge.organization import create_knowledge_organization_snapshot
from app.core.knowledge.organization_lifecycle import (
    coordinate_organization_cancel_request,
    coordinate_organization_terminal,
)
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
from app.core.knowledge.organization_types import (
    KnowledgeOrganizationExecutionResult,
    KnowledgeOrganizationPlan,
)
from app.core.knowledge_jobs.consumer import KnowledgeJobConsumer
from app.core.knowledge_jobs.executor import (
    KnowledgeJobCancelledError,
    KnowledgeJobExecutionContext,
    KnowledgeJobExecutionResult,
    KnowledgeJobExecutor,
)
from app.core.knowledge_jobs.handlers import (
    create_default_knowledge_job_executor,
    handle_organization_mutation,
)
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    KnowledgeOrganizationFragment,
    KnowledgeOrganizationFragmentStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationSnapshotItem,
    KnowledgeOrganizationStage,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.providers.database.time import get_database_time

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    KnowledgeJob.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeOrganizationSnapshot.__table__,
    KnowledgeOrganizationSnapshotItem.__table__,
    KnowledgeOrganizationStage.__table__,
    KnowledgeOrganizationFragment.__table__,
)


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "knowledge-organization-knowledge_organization_publication.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
        poolclass=NullPool,
    )

    @event.listens_for(engine.sync_engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SQLModel.metadata.create_all(sync_connection, tables=_TABLES))

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _create_managed_container(db: AsyncSession, *, suffix: str = "main") -> KnowledgeBase:
    channel = ModelChannel(
        name=f"organization-knowledge_organization_publication-channel-{suffix}",
        api_key="test-key",
        base_url="https://example.invalid",
        model_ids=[],
    )
    db.add(channel)
    await db.flush()

    prompt = PromptLibrary(uid="user-1", name=f"organization-knowledge_organization_publication-prompt-{suffix}", content="prompt")
    db.add(prompt)
    await db.flush()

    profile = Profile(
        uid="user-1",
        name=f"organization-knowledge_organization_publication-profile-{suffix}",
        prompt_id=prompt.id,
        configs={},
    )
    db.add(profile)
    await db.flush()

    knowledge_base = KnowledgeBase(
        uid="user-1",
        name=f"managed-knowledge_organization_publication-{suffix}",
        embedding_channel_id=channel.id,
        embedding_model_id="embedding-model",
        embedding_dimensions=1536,
        collection_name=f"managed-knowledge_organization_publication-{suffix}-collection",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=channel.id,
        active_embedding_model_id="embedding-model",
        active_embedding_dimensions=1536,
        active_embedding_signature="embedding-signature",
        active_embedding_revision=3,
        active_collection_name=f"managed-knowledge_organization_publication-{suffix}-active",
        index_revision=5,
    )
    db.add(knowledge_base)
    await db.commit()
    await db.refresh(knowledge_base)
    return knowledge_base


async def _add_item(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
    knowledge_key: str,
    content: str,
    llm_maintainable: bool = True,
    is_recallable: bool = True,
) -> ManagedKnowledgeItem:
    item = ManagedKnowledgeItem(
        knowledge_base_id=knowledge_base_id,
        uid="user-1",
        knowledge_key=knowledge_key,
        content=content,
        content_token_count=max(1, len(content.split())),
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        version=1,
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        source_reference={"source": knowledge_key},
        created_by=ManagedKnowledgeActorType.LLM,
        last_modified_by=ManagedKnowledgeActorType.LLM,
        llm_maintainable=llm_maintainable,
        indexed_version=1 if is_recallable else 0,
        vector_item_ids=[f"vector-{knowledge_key}"] if is_recallable else [],
        is_recallable=is_recallable,
        pending_job_id=None,
    )
    db.add(item)
    await db.flush()
    db.add(
        ManagedKnowledgeRevision(
            knowledge_base_id=knowledge_base_id,
            uid="user-1",
            knowledge_id=item.id,
            version=1,
            operation=ManagedKnowledgeRevisionOperation.CREATE,
            before_snapshot=None,
            after_snapshot=build_managed_knowledge_snapshot(item),
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            source_reference={"source": knowledge_key},
            modified_by=ManagedKnowledgeActorType.LLM,
        )
    )
    await db.commit()
    await db.refresh(item)
    return item


async def _create_running_organization_job(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
    suffix: str,
    operation: KnowledgeJobOperation = KnowledgeJobOperation.MANUAL_ORGANIZE,
    owner: str = "knowledge_organization_publication-parent-worker",
) -> KnowledgeJob:
    available_at = await get_database_time(db)
    job, created = await knowledge_job_crud.create(
        db,
        uid="user-1",
        operation=operation,
        dedupe_key=f"knowledge_organization_publication-parent:{knowledge_base_id}:{suffix}",
        request_hash=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
        active_change_key=f"kb-organization:{knowledge_base_id}",
        status=KnowledgeJobStatus.PENDING,
        knowledge_base_id=knowledge_base_id,
        payload={"source": "knowledge_organization_publication"},
        available_at=available_at,
        max_attempts=1,
        commit=False,
    )
    assert created is True
    assert job.id is not None
    claimed = await knowledge_job_crud.try_claim(
        db,
        uid="user-1",
        job_id=job.id,
        owner=owner,
        lease_seconds=60,
        enabled_operations=(operation,),
        commit=False,
    )
    assert claimed is not None
    await db.commit()
    return claimed


async def _create_pending_organization_job(
    db: AsyncSession,
    *,
    knowledge_base_id: int,
    suffix: str,
) -> KnowledgeJob:
    available_at = await get_database_time(db)
    job, created = await knowledge_job_crud.create(
        db,
        uid="user-1",
        operation=KnowledgeJobOperation.MANUAL_ORGANIZE,
        dedupe_key=f"knowledge_organization_publication-pending:{knowledge_base_id}:{suffix}",
        request_hash=hashlib.sha256(suffix.encode("utf-8")).hexdigest(),
        active_change_key=f"kb-organization:{knowledge_base_id}",
        status=KnowledgeJobStatus.PENDING,
        knowledge_base_id=knowledge_base_id,
        payload={"source": "knowledge_organization_publication"},
        available_at=available_at,
        max_attempts=1,
    )
    assert created is True
    assert job.id is not None
    return job


async def _mark_organization_job_terminal(
    db: AsyncSession,
    *,
    uid: str,
    job_id: int,
    owner: str,
    status: KnowledgeJobStatus,
    error: str | None = None,
) -> bool:
    if status == KnowledgeJobStatus.SUCCEEDED:
        changed = await knowledge_job_crud.mark_succeeded(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            commit=False,
        )
    elif status == KnowledgeJobStatus.FAILED:
        changed = await knowledge_job_crud.mark_failed(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            error=error,
            commit=False,
        )
    elif status == KnowledgeJobStatus.CANCELLED:
        changed = await knowledge_job_crud.mark_cancelled(
            db,
            uid=uid,
            job_id=job_id,
            owner=owner,
            commit=False,
        )
    else:
        raise ValueError(f"unsupported terminal status: {status}")

    if changed:
        await coordinate_organization_terminal(db, uid, job_id, error=error, commit=False)
    await db.commit()
    return changed


async def _request_organization_cancel(
    db: AsyncSession,
    *,
    uid: str,
    job_id: int,
):
    cancellation = await knowledge_job_crud.request_cancel(
        db,
        uid=uid,
        job_id=job_id,
        commit=False,
    )
    if cancellation.changed:
        await coordinate_organization_cancel_request(db, uid, job_id, commit=False)
        await coordinate_organization_terminal(db, uid, job_id, commit=False)
    await db.commit()
    return cancellation


def _full_plan(knowledge_ids: list[int]) -> KnowledgeOrganizationPlan:
    keep_id, update_id, merge_primary_id, merge_secondary_id, conflict_id, conflict_peer_id = knowledge_ids
    return KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "keep",
                    "source": {"knowledge_id": keep_id, "expected_version": 1},
                    "summary": "keep source",
                },
                {
                    "action": "update",
                    "source": {"knowledge_id": update_id, "expected_version": 1},
                    "target": {"knowledge_key": "updated-topic", "content": "updated organization content"},
                    "summary": "update source",
                },
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": merge_primary_id, "expected_version": 1},
                        {"knowledge_id": merge_secondary_id, "expected_version": 1},
                    ],
                    "primary_knowledge_id": merge_primary_id,
                    "target": {"knowledge_key": "merged-topic", "content": "merged organization content"},
                    "summary": "merge sources",
                },
                {
                    "action": "conflict",
                    "sources": [
                        {"knowledge_id": conflict_id, "expected_version": 1},
                        {"knowledge_id": conflict_peer_id, "expected_version": 1},
                    ],
                    "reason": "sources need review",
                    "summary": "conflicting sources",
                },
            ]
        }
    )


async def _complete_stage(
    db: AsyncSession,
    *,
    snapshot: KnowledgeOrganizationSnapshot,
    plan: KnowledgeOrganizationPlan,
    stage_key: str = "stage-15-final",
) -> KnowledgeOrganizationStage:
    work_key = hashlib.sha256(f"knowledge_organization_publication:{snapshot.id}:{stage_key}".encode()).hexdigest()
    model_key = hashlib.sha256(b"knowledge_organization_publication-model").hexdigest()
    stage, created = await knowledge_organization_stage_crud.create_stage(
        db,
        stage=KnowledgeOrganizationStage(
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            snapshot_id=snapshot.id,
            work_key=work_key,
            snapshot_key=snapshot.snapshot_key,
            stage_key=stage_key,
            stage_index=0,
            lower_stage_key=None,
            model_key=model_key,
            model_snapshot={
                "execution_model": {
                    "channel_id": 1,
                    "model_id": "knowledge_organization_publication-model",
                    "protocol": "openai",
                }
            },
            expected_fragment_count=1,
        ),
    )
    assert created is True
    fragment, fragment_created = await knowledge_organization_fragment_crud.write_ordered(
        db,
        fragment=KnowledgeOrganizationFragment(
            dedupe_key=knowledge_organization_fragment_crud.build_dedupe_key(
                work_key=stage.work_key,
                stage_key=stage.stage_key,
                model_key=stage.model_key,
                fragment_index=0,
            ),
            uid=snapshot.uid,
            knowledge_base_id=snapshot.knowledge_base_id,
            snapshot_id=snapshot.id,
            stage_id=stage.id,
            work_key=stage.work_key,
            snapshot_key=snapshot.snapshot_key,
            stage_key=stage.stage_key,
            model_key=stage.model_key,
            fragment_index=0,
            candidate_scope={"knowledge_ids": [1]},
            result=plan.model_dump(mode="json"),
            status=KnowledgeOrganizationFragmentStatus.COMPLETED,
        ),
    )
    assert fragment is not None and fragment_created is True
    stage = await knowledge_organization_stage_crud.get_by_id(db, stage_id=stage.id)
    assert stage is not None
    assert (
        await knowledge_organization_stage_crud.mark_completed(
            db,
            work_key=stage.work_key,
            stage_key=stage.stage_key,
            snapshot_key=stage.snapshot_key,
            model_key=stage.model_key,
        )
        is True
    )
    persisted = await knowledge_organization_stage_crud.get_by_id(db, stage_id=stage.id)
    assert persisted is not None
    return persisted


async def _seed_items(db: AsyncSession, *, knowledge_base_id: int, count: int) -> list[ManagedKnowledgeItem]:
    return [
        await _add_item(
            db,
            knowledge_base_id=knowledge_base_id,
            knowledge_key=f"topic-{index}",
            content=f"organization content {index}",
        )
        for index in range(count)
    ]


def _zero_progress() -> dict[str, int]:
    return {
        "stage_count": 0,
        "completed_stage_count": 0,
        "running_stage_count": 0,
        "failed_stage_count": 0,
        "invalidated_stage_count": 0,
        "expected_fragment_count": 0,
        "succeeded_fragment_count": 0,
        "completed_fragment_count": 0,
        "invalidated_fragment_count": 0,
    }


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
async def test_publication_uses_bounded_snapshot_and_current_item_pages(session_factory, monkeypatch):
    import app.core.knowledge.organization_publication as publication_module

    owner = "knowledge_organization_publication-publication-bounded-pages"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="bounded-pages")
        items = await _seed_items(db, knowledge_base_id=knowledge_base.id, count=6)
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="bounded-pages",
            owner=owner,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=parent.id,
        )
        plan = _full_plan([item.id for item in items])
        final_stage = await _complete_stage(db, snapshot=snapshot, plan=plan, stage_key="stage-15-bounded-pages")
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    snapshot_page_calls: list[tuple[int, int]] = []
    current_item_calls: list[tuple[int, ...]] = []
    original_list_with_revision_page = publication_module.knowledge_organization_snapshot_item_crud.list_with_revision_page
    original_get_by_ids = publication_module.managed_knowledge_item_crud.get_by_ids

    async def recording_list_with_revision_page(*args, **kwargs):
        snapshot_page_calls.append((kwargs["after_sequence"], kwargs["limit"]))
        return await original_list_with_revision_page(*args, **kwargs)

    async def recording_get_by_ids(*args, **kwargs):
        current_item_calls.append(tuple(kwargs["knowledge_ids"]))
        return await original_get_by_ids(*args, **kwargs)

    monkeypatch.setattr(
        publication_module,
        "_SNAPSHOT_PAGE_SIZE",
        2,
    )
    monkeypatch.setattr(
        publication_module.knowledge_organization_snapshot_item_crud,
        "list_with_revision_page",
        recording_list_with_revision_page,
    )
    monkeypatch.setattr(
        publication_module.managed_knowledge_item_crud,
        "get_by_ids",
        recording_get_by_ids,
    )

    result = await publish_knowledge_organization_plan(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        organization_job_id=parent_id,
        owner=owner,
        snapshot_id=snapshot_id,
        final_stage_id=final_stage_id,
        plan=plan,
    )

    assert snapshot_page_calls == [(-1, 2), (1, 2), (3, 2), (5, 1)]
    assert len(current_item_calls) == 3
    assert all(len(knowledge_ids) <= 2 for knowledge_ids in current_item_calls)
    assert len(result.mutation_job_ids) == 2

    async with session_factory() as db:
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)

    assert len(children) == 2
    assert {child.operation for child in children} == {KnowledgeJobOperation.ORGANIZE_MUTATION}
    assert {child.status for child in children} == {KnowledgeJobStatus.PENDING}
    assert {child.id for child in children} == set(result.mutation_job_ids)


@pytest.mark.asyncio
async def test_publication_uses_default_pages_for_401_item_keep_plan(session_factory, monkeypatch):
    import app.core.knowledge.organization_publication as publication_module

    owner = "knowledge_organization_publication-publication-default-large-pages"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="default-large-pages")
        assert knowledge_base.id is not None
        items = [
            ManagedKnowledgeItem(
                knowledge_base_id=knowledge_base.id,
                uid="user-1",
                knowledge_key=f"large-topic-{index}",
                content=f"large organization content {index}",
                content_token_count=4,
                content_hash=hashlib.sha256(f"large organization content {index}".encode()).hexdigest(),
                version=1,
                source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                source_reference={"source": f"large-topic-{index}"},
                created_by=ManagedKnowledgeActorType.LLM,
                last_modified_by=ManagedKnowledgeActorType.LLM,
                llm_maintainable=True,
                indexed_version=1,
                vector_item_ids=[f"vector-large-topic-{index}"],
                is_recallable=True,
                pending_job_id=None,
            )
            for index in range(401)
        ]
        db.add_all(items)
        await db.flush()
        assert all(item.id is not None for item in items)
        revisions = [
            ManagedKnowledgeRevision(
                knowledge_base_id=knowledge_base.id,
                uid="user-1",
                knowledge_id=item.id,
                version=1,
                operation=ManagedKnowledgeRevisionOperation.CREATE,
                before_snapshot=None,
                after_snapshot=build_managed_knowledge_snapshot(item),
                source_type=ManagedKnowledgeSourceType.LLM_TOOL,
                source_reference=item.source_reference,
                modified_by=ManagedKnowledgeActorType.LLM,
            )
            for item in items
        ]
        db.add_all(revisions)
        await db.commit()

        knowledge_ids = [item.id for item in items if item.id is not None]
        assert len(knowledge_ids) == 401
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="default-large-pages",
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
                        "action": "keep",
                        "source": {"knowledge_id": knowledge_id, "expected_version": 1},
                        "summary": "keep large page item",
                    }
                    for knowledge_id in knowledge_ids
                ]
            }
        )
        final_stage = await _complete_stage(
            db,
            snapshot=snapshot,
            plan=plan,
            stage_key="stage-15-default-large-pages",
        )
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    snapshot_page_calls: list[tuple[int, int]] = []
    current_item_calls: list[tuple[int, ...]] = []
    original_list_with_revision_page = publication_module.knowledge_organization_snapshot_item_crud.list_with_revision_page
    original_get_by_ids = publication_module.managed_knowledge_item_crud.get_by_ids

    async def recording_list_with_revision_page(*args, **kwargs):
        snapshot_page_calls.append((kwargs["after_sequence"], kwargs["limit"]))
        return await original_list_with_revision_page(*args, **kwargs)

    async def recording_get_by_ids(*args, **kwargs):
        current_item_calls.append(tuple(kwargs["knowledge_ids"]))
        return await original_get_by_ids(*args, **kwargs)

    monkeypatch.setattr(
        publication_module.knowledge_organization_snapshot_item_crud,
        "list_with_revision_page",
        recording_list_with_revision_page,
    )
    monkeypatch.setattr(
        publication_module.managed_knowledge_item_crud,
        "get_by_ids",
        recording_get_by_ids,
    )

    result = await publish_knowledge_organization_plan(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base_id,
        organization_job_id=parent_id,
        owner=owner,
        snapshot_id=snapshot_id,
        final_stage_id=final_stage_id,
        plan=plan,
    )

    assert result.mutation_job_ids == ()
    assert snapshot_page_calls == [(-1, 200), (199, 200), (399, 1), (400, 1)]
    assert [len(knowledge_ids) for knowledge_ids in current_item_calls] == [200, 200, 1]
    assert all(len(knowledge_ids) <= 200 for knowledge_ids in current_item_calls)
    validated_ids = [knowledge_id for batch in current_item_calls for knowledge_id in batch]
    assert len(validated_ids) == 401
    assert len(set(validated_ids)) == 401
    assert set(validated_ids) == set(knowledge_ids)

    async with session_factory() as db:
        children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)

    assert children == []


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


@pytest.mark.asyncio
async def test_auto_organization_counts_candidates_without_loading_full_list(session_factory, monkeypatch):
    import app.core.knowledge.organization_run as organization_run

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="auto-count-only")
        organization_last_run_at = await get_database_time(db) - timedelta(seconds=1)
        knowledge_base.organization_last_run_at = organization_last_run_at
        await db.commit()
        await _seed_items(db, knowledge_base_id=knowledge_base.id, count=5)

    count_calls: list[tuple[bool, object | None]] = []
    list_calls = 0
    original_count = organization_run.managed_knowledge_item_crud.count_organization_candidates

    async def fail_list(*_args, **_kwargs):
        nonlocal list_calls
        list_calls += 1
        raise AssertionError("automatic organization must not load the full candidate list")

    async def recording_count(*args, **kwargs):
        count_calls.append(("updated_after" in kwargs, kwargs.get("updated_after")))
        return await original_count(*args, **kwargs)

    monkeypatch.setattr(organization_run.managed_knowledge_item_crud, "list_organization_candidates", fail_list)
    monkeypatch.setattr(organization_run.managed_knowledge_item_crud, "count_organization_candidates", recording_count)

    async with session_factory() as db:
        job = await submit_auto_knowledge_organization(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )

    assert job is not None
    assert job.operation == KnowledgeJobOperation.AUTO_ORGANIZE
    assert count_calls == [(False, None), (True, organization_last_run_at)]
    assert list_calls == 0


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


@pytest.mark.asyncio
async def test_real_worker_publishes_update_and_completes_organization_parent(
    session_factory,
    monkeypatch,
):
    import app.core.knowledge.organization_run as organization_run

    parent_owner = "knowledge_organization_publication-real-worker-parent"
    mutation_owner = "knowledge_organization_publication-real-worker-mutation"
    update_owner = "knowledge_organization_publication-real-worker-update"
    target_key = "real-worker-updated-topic"
    target_content = "updated through the real knowledge worker path"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="real-worker")
        channel = await db.get(ModelChannel, knowledge_base.embedding_channel_id)
        assert channel is not None
        channel.model_ids = [
            {
                "model_id": "embedding-model",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "embedding_dimensions": 1536,
            }
        ]
        await db.commit()

        item = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="real-worker-topic",
            content="original real worker content",
        )
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="real-worker",
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
                        "action": "update",
                        "source": {"knowledge_id": item.id, "expected_version": 1},
                        "target": {"knowledge_key": target_key, "content": target_content},
                        "summary": "publish through the worker",
                    }
                ]
            }
        )
        final_stage = await _complete_stage(
            db,
            snapshot=snapshot,
            plan=plan,
            stage_key="stage-15-real-worker",
        )
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        target_id = item.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    organization_run_calls: list[dict[str, int | str]] = []

    async def fake_organization_run(*_args, **kwargs):
        organization_run_calls.append(
            {
                "uid": kwargs["uid"],
                "knowledge_base_id": kwargs["knowledge_base_id"],
                "organization_job_id": kwargs["organization_job_id"],
            }
        )
        return KnowledgeOrganizationExecutionResult(
            plan=plan,
            model_id=final_stage.model_snapshot["execution_model"]["model_id"],
            stage_count=1,
            snapshot_id=snapshot_id,
            final_stage_id=final_stage_id,
            final_stage_key=final_stage.stage_key,
            work_key=final_stage.work_key,
        )

    embedding_calls: list[tuple[EmbeddingRuntimeConfig, list[str]]] = []
    upsert_calls: list[tuple[str, list[str], list[str], list[list[float]], list[dict]]] = []

    async def fake_get_or_create_collection(collection_name: str, **_kwargs):
        return {"name": collection_name}

    async def fake_embed(
        config: EmbeddingRuntimeConfig,
        texts: list[str],
        **kwargs,
    ) -> list[list[float]]:
        assert isinstance(config, EmbeddingRuntimeConfig)
        assert config.embedding_dimensions == 1536
        assert kwargs["dimensions"] == 1536
        embedding_calls.append((config, list(texts)))
        return [[0.1] * 1536 for _ in texts]

    async def fake_upsert(
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        **_kwargs,
    ) -> int:
        upsert_calls.append(
            (
                collection_name,
                list(item_ids),
                list(documents),
                [list(vector) for vector in embeddings],
                [dict(metadata) for metadata in metadatas],
            )
        )
        return len(item_ids)

    monkeypatch.setattr(organization_run, "_execute_knowledge_organization_run", fake_organization_run)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", fake_get_or_create_collection)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)

    executor = create_default_knowledge_job_executor(session_factory=session_factory)
    consumer = KnowledgeJobConsumer(
        executor,
        session_factory=session_factory,
        poll_interval_seconds=0.01,
    )

    async def wait_for_children(expected_count: int) -> list[KnowledgeJob]:
        async def poll() -> list[KnowledgeJob]:
            while True:
                async with session_factory() as db:
                    children = await knowledge_job_crud.list_children(
                        db,
                        uid="user-1",
                        parent_job_id=parent_id,
                    )
                if len(children) >= expected_count:
                    return children
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout=2.0)

    parent_task = asyncio.create_task(consumer._execute(parent, parent_owner))
    try:
        children = await wait_for_children(1)
        assert organization_run_calls == [
            {
                "uid": "user-1",
                "knowledge_base_id": knowledge_base_id,
                "organization_job_id": parent_id,
            }
        ]
        assert len(children) == 1
        assert children[0].operation == KnowledgeJobOperation.ORGANIZE_MUTATION
        assert children[0].payload["snapshot_id"] == snapshot_id
        assert children[0].payload["stage_id"] == final_stage_id
        async with session_factory() as db:
            running_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        assert running_parent is not None and running_parent.status == KnowledgeJobStatus.RUNNING
        assert not parent_task.done()

        async with session_factory() as db:
            mutation_job = await knowledge_job_crud.get_by_id(
                db,
                uid="user-1",
                job_id=children[0].id,
            )
            assert mutation_job is not None
            mutation_job.available_at = await get_database_time(db)
            await db.flush()
            claimed_mutation = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=mutation_job.id,
                owner=mutation_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.ORGANIZE_MUTATION,),
                commit=False,
            )
            assert claimed_mutation is not None
            await db.commit()

        await asyncio.wait_for(
            consumer._execute(claimed_mutation, mutation_owner),
            timeout=2.0,
        )

        async with session_factory() as db:
            direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
            mutation_job = next(child for child in direct_children if child.operation == KnowledgeJobOperation.ORGANIZE_MUTATION)
            managed_update = next(child for child in direct_children if child.operation == KnowledgeJobOperation.MANAGED_UPDATE)
        assert mutation_job.status == KnowledgeJobStatus.SUCCEEDED
        assert managed_update.parent_job_id == parent_id
        assert managed_update.knowledge_id == target_id
        assert managed_update.expected_version == 2
        assert managed_update.status == KnowledgeJobStatus.PENDING

        async with session_factory() as db:
            managed_update = await knowledge_job_crud.get_by_id(
                db,
                uid="user-1",
                job_id=managed_update.id,
            )
            assert managed_update is not None
            managed_update.available_at = await get_database_time(db)
            await db.flush()
            claimed_update = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=managed_update.id,
                owner=update_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.MANAGED_UPDATE,),
                commit=False,
            )
            assert claimed_update is not None
            await db.commit()

        await asyncio.wait_for(
            consumer._execute(claimed_update, update_owner),
            timeout=2.0,
        )
        await asyncio.wait_for(parent_task, timeout=2.0)
    finally:
        if not parent_task.done():
            parent_task.cancel()
        await asyncio.gather(parent_task, return_exceptions=True)

    async with session_factory() as db:
        final_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        target = await db.get(ManagedKnowledgeItem, target_id)
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base_id)
        all_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id))).scalars())

    assert final_parent is not None
    assert final_parent.status == KnowledgeJobStatus.SUCCEEDED
    assert final_parent.active_change_key is None
    assert final_parent.locked_by is None
    assert {(child.operation, child.status) for child in direct_children} == {
        (KnowledgeJobOperation.ORGANIZE_MUTATION, KnowledgeJobStatus.SUCCEEDED),
        (KnowledgeJobOperation.MANAGED_UPDATE, KnowledgeJobStatus.SUCCEEDED),
    }
    assert target is not None
    assert target.knowledge_key == target_key
    assert target.content == target_content
    assert target.version == 2
    assert target.indexed_version == target.version
    assert target.is_recallable is True
    assert target.pending_job_id is None
    assert all(item.organization_lock_token is None for item in all_items)
    assert current_knowledge_base is not None
    assert current_knowledge_base.index_status == KnowledgeBaseIndexStatus.READY
    assert current_knowledge_base.organization_last_job_id == parent_id
    assert current_knowledge_base.organization_last_run_at is not None
    assert current_knowledge_base.organization_error is None

    assert len(embedding_calls) == 1
    embedding_config, embedded_texts = embedding_calls[0]
    assert embedding_config.declared_dimensions == 1536
    assert embedded_texts == [target_content]
    assert len(upsert_calls) == 1
    collection_name, vector_ids, documents, embeddings, metadatas = upsert_calls[0]
    assert collection_name == "managed-knowledge_organization_publication-real-worker-active"
    assert vector_ids == target.vector_item_ids
    assert documents == [target_content]
    assert len(vector_ids) == len(embeddings) == len(metadatas)
    assert all(len(vector) == 1536 for vector in embeddings)


@pytest.mark.asyncio
async def test_real_worker_publishes_merge_with_secondary_identity_and_completes_organization_parent(
    session_factory,
    monkeypatch,
):
    import app.core.knowledge.organization_run as organization_run

    parent_owner = "knowledge_organization_publication-real-worker-merge-parent"
    mutation_owner = "knowledge_organization_publication-real-worker-merge-mutation"
    delete_owner = "knowledge_organization_publication-real-worker-merge-delete"
    update_owner = "knowledge_organization_publication-real-worker-merge-update"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="real-worker-merge")
        channel = await db.get(ModelChannel, knowledge_base.embedding_channel_id)
        assert channel is not None
        channel.model_ids = [
            {
                "model_id": "embedding-model",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "embedding_dimensions": 1536,
            }
        ]
        await db.commit()

        primary = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="real-worker-merge-primary",
            content="original real worker merge primary content",
        )
        secondary = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="real-worker-merge-secondary",
            content="original real worker merge secondary content",
        )
        secondary_key = secondary.knowledge_key
        assert secondary_key is not None
        target_key = secondary_key
        target_content = secondary.content
        primary_id = primary.id
        secondary_id = secondary.id
        assert primary_id is not None and secondary_id is not None
        primary_vector_item_ids = list(primary.vector_item_ids)
        secondary_vector_item_ids = list(secondary.vector_item_ids)

        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="real-worker-merge",
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
                            {"knowledge_id": primary_id, "expected_version": 1},
                            {"knowledge_id": secondary_id, "expected_version": 1},
                        ],
                        "primary_knowledge_id": primary_id,
                        "target": {
                            "knowledge_key": target_key,
                            "content": target_content,
                        },
                        "summary": "publish merge through the real worker",
                    }
                ]
            }
        )
        final_stage = await _complete_stage(
            db,
            snapshot=snapshot,
            plan=plan,
            stage_key="stage-15-real-worker-merge",
        )
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id
        collection_name = knowledge_base.active_collection_name
        assert collection_name is not None

    organization_run_calls: list[dict[str, int | str]] = []

    async def fake_organization_run(*_args, **kwargs):
        organization_run_calls.append(
            {
                "uid": kwargs["uid"],
                "knowledge_base_id": kwargs["knowledge_base_id"],
                "organization_job_id": kwargs["organization_job_id"],
            }
        )
        return KnowledgeOrganizationExecutionResult(
            plan=plan,
            model_id=final_stage.model_snapshot["execution_model"]["model_id"],
            stage_count=1,
            snapshot_id=snapshot_id,
            final_stage_id=final_stage_id,
            final_stage_key=final_stage.stage_key,
            work_key=final_stage.work_key,
        )

    embedding_calls: list[tuple[EmbeddingRuntimeConfig, list[str]]] = []
    upsert_calls: list[tuple[str, list[str], list[str], list[list[float]], list[dict]]] = []
    delete_calls: list[tuple[str, list[str]]] = []

    async def fake_get_or_create_collection(collection_name: str, **_kwargs):
        return {"name": collection_name}

    async def fake_embed(
        config: EmbeddingRuntimeConfig,
        texts: list[str],
        **kwargs,
    ) -> list[list[float]]:
        assert isinstance(config, EmbeddingRuntimeConfig)
        assert config.embedding_dimensions == 1536
        assert kwargs["dimensions"] == 1536
        embedding_calls.append((config, list(texts)))
        return [[0.1] * 1536 for _ in texts]

    async def fake_upsert(
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        **_kwargs,
    ) -> int:
        upsert_calls.append(
            (
                collection_name,
                list(item_ids),
                list(documents),
                [list(vector) for vector in embeddings],
                [dict(metadata) for metadata in metadatas],
            )
        )
        return len(item_ids)

    async def fake_delete(collection_name: str, item_ids: list[str], **_kwargs) -> int:
        delete_calls.append((collection_name, list(item_ids)))
        return len(item_ids)

    monkeypatch.setattr(organization_run, "_execute_knowledge_organization_run", fake_organization_run)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", fake_get_or_create_collection)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_delete_collection_items", fake_delete)

    executor = create_default_knowledge_job_executor(session_factory=session_factory)
    consumer = KnowledgeJobConsumer(
        executor,
        session_factory=session_factory,
        poll_interval_seconds=0.01,
    )

    async def wait_for_children(expected_count: int) -> list[KnowledgeJob]:
        async def poll() -> list[KnowledgeJob]:
            while True:
                async with session_factory() as db:
                    children = await knowledge_job_crud.list_children(
                        db,
                        uid="user-1",
                        parent_job_id=parent_id,
                    )
                if len(children) >= expected_count:
                    return children
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout=2.0)

    async def execute_child(job_id: int, owner: str, operation: KnowledgeJobOperation) -> None:
        async with session_factory() as db:
            job = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=job_id)
            assert job is not None
            job.available_at = await get_database_time(db)
            await db.flush()
            claimed = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=job_id,
                owner=owner,
                lease_seconds=60,
                enabled_operations=(operation,),
                commit=False,
            )
            assert claimed is not None
            await db.commit()
        await asyncio.wait_for(consumer._execute(claimed, owner), timeout=2.0)

    parent_task = asyncio.create_task(consumer._execute(parent, parent_owner))
    try:
        children = await wait_for_children(1)
        assert organization_run_calls == [
            {
                "uid": "user-1",
                "knowledge_base_id": knowledge_base_id,
                "organization_job_id": parent_id,
            }
        ]
        assert len(children) == 1
        mutation_job = children[0]
        assert mutation_job.operation == KnowledgeJobOperation.ORGANIZE_MUTATION
        assert mutation_job.status == KnowledgeJobStatus.PENDING
        assert mutation_job.payload["snapshot_id"] == snapshot_id
        assert mutation_job.payload["stage_id"] == final_stage_id
        assert not parent_task.done()

        await execute_child(mutation_job.id, mutation_owner, KnowledgeJobOperation.ORGANIZE_MUTATION)

        children = await wait_for_children(3)
        children_by_operation = {child.operation: child for child in children}
        assert len(children) == 3
        assert set(children_by_operation) == {
            KnowledgeJobOperation.ORGANIZE_MUTATION,
            KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
            KnowledgeJobOperation.MANAGED_UPDATE,
        }
        assert children_by_operation[KnowledgeJobOperation.ORGANIZE_MUTATION].status == KnowledgeJobStatus.SUCCEEDED
        assert children_by_operation[KnowledgeJobOperation.MANAGED_DELETE_CLEANUP].status == KnowledgeJobStatus.PENDING
        assert children_by_operation[KnowledgeJobOperation.MANAGED_UPDATE].status == KnowledgeJobStatus.PENDING

        await execute_child(
            children_by_operation[KnowledgeJobOperation.MANAGED_DELETE_CLEANUP].id,
            delete_owner,
            KnowledgeJobOperation.MANAGED_DELETE_CLEANUP,
        )
        await execute_child(
            children_by_operation[KnowledgeJobOperation.MANAGED_UPDATE].id,
            update_owner,
            KnowledgeJobOperation.MANAGED_UPDATE,
        )
        await asyncio.wait_for(parent_task, timeout=2.0)
    finally:
        if not parent_task.done():
            parent_task.cancel()
        await asyncio.gather(parent_task, return_exceptions=True)

    async with session_factory() as db:
        final_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        final_primary = await db.get(ManagedKnowledgeItem, primary_id)
        final_secondary = await db.get(ManagedKnowledgeItem, secondary_id)
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base_id)
        all_items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.knowledge_base_id == knowledge_base_id).order_by(ManagedKnowledgeItem.id))).scalars())

    assert final_parent is not None
    assert final_parent.status == KnowledgeJobStatus.SUCCEEDED
    assert final_parent.active_change_key is None
    assert final_parent.locked_by is None
    assert final_parent.lock_until is None
    assert len(direct_children) == 3
    assert {(child.operation, child.status) for child in direct_children} == {
        (KnowledgeJobOperation.ORGANIZE_MUTATION, KnowledgeJobStatus.SUCCEEDED),
        (KnowledgeJobOperation.MANAGED_DELETE_CLEANUP, KnowledgeJobStatus.SUCCEEDED),
        (KnowledgeJobOperation.MANAGED_UPDATE, KnowledgeJobStatus.SUCCEEDED),
    }
    assert final_primary is not None
    assert final_primary.knowledge_key == target_key
    assert final_primary.content == target_content
    assert final_primary.version == 2
    assert final_primary.indexed_version == 2
    assert final_primary.is_recallable is True
    assert final_primary.pending_job_id is None
    assert final_primary.organization_lock_token is None
    assert final_primary.vector_item_ids != primary_vector_item_ids
    assert final_secondary is None
    assert all(item.organization_lock_token is None for item in all_items)
    assert current_knowledge_base is not None
    assert current_knowledge_base.index_status == KnowledgeBaseIndexStatus.READY
    assert current_knowledge_base.organization_last_job_id == parent_id
    assert current_knowledge_base.organization_last_run_at is not None
    assert current_knowledge_base.organization_error is None

    assert len(embedding_calls) == 1
    embedding_config, embedded_texts = embedding_calls[0]
    assert embedding_config.declared_dimensions == 1536
    assert embedded_texts == [target_content]
    assert len(upsert_calls) == 1
    upsert_collection, vector_ids, documents, embeddings, metadatas = upsert_calls[0]
    assert upsert_collection == collection_name
    assert vector_ids == final_primary.vector_item_ids
    assert vector_ids != primary_vector_item_ids
    assert documents == [target_content]
    assert len(vector_ids) == len(embeddings) == len(metadatas)
    assert all(len(vector) == 1536 for vector in embeddings)
    assert any(deleted_collection == collection_name and set(secondary_vector_item_ids).issubset(deleted_ids) for deleted_collection, deleted_ids in delete_calls)


@pytest.mark.asyncio
async def test_real_worker_failed_update_fails_parent_and_rolls_back_item(
    session_factory,
    monkeypatch,
):
    import app.core.knowledge.organization_run as organization_run

    parent_owner = "knowledge_organization_publication-real-worker-failure-parent"
    mutation_owner = "knowledge_organization_publication-real-worker-failure-mutation"
    update_owner = "knowledge_organization_publication-real-worker-failure-update"
    target_key = "real-worker-failure-updated-topic"
    target_content = "updated through the failing real knowledge worker path"

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db, suffix="real-worker-failure")
        channel = await db.get(ModelChannel, knowledge_base.embedding_channel_id)
        assert channel is not None
        channel.model_ids = [
            {
                "model_id": "embedding-model",
                "usage": "EMBEDDING",
                "protocol": "OPENAI_EMBEDDING",
                "embedding_dimensions": 1536,
            }
        ]
        await db.commit()

        item = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            knowledge_key="real-worker-failure-topic",
            content="original real worker failure content",
        )
        before_state = (
            item.knowledge_key,
            item.content,
            item.version,
            item.indexed_version,
            item.is_recallable,
            list(item.vector_item_ids),
        )
        parent = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            suffix="real-worker-failure",
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
                        "action": "update",
                        "source": {"knowledge_id": item.id, "expected_version": 1},
                        "target": {"knowledge_key": target_key, "content": target_content},
                        "summary": "fail publication through the worker",
                    }
                ]
            }
        )
        final_stage = await _complete_stage(
            db,
            snapshot=snapshot,
            plan=plan,
            stage_key="stage-15-real-worker-failure",
        )
        knowledge_base_id = knowledge_base.id
        parent_id = parent.id
        target_id = item.id
        snapshot_id = snapshot.id
        final_stage_id = final_stage.id

    organization_run_calls: list[dict[str, int | str]] = []

    async def fake_organization_run(*_args, **kwargs):
        organization_run_calls.append(
            {
                "uid": kwargs["uid"],
                "knowledge_base_id": kwargs["knowledge_base_id"],
                "organization_job_id": kwargs["organization_job_id"],
            }
        )
        return KnowledgeOrganizationExecutionResult(
            plan=plan,
            model_id=final_stage.model_snapshot["execution_model"]["model_id"],
            stage_count=1,
            snapshot_id=snapshot_id,
            final_stage_id=final_stage_id,
            final_stage_key=final_stage.stage_key,
            work_key=final_stage.work_key,
        )

    collection_calls: list[str] = []
    embedding_calls: list[tuple[EmbeddingRuntimeConfig, list[str]]] = []
    upsert_calls: list[tuple[str, list[str], list[str], list[list[float]], list[dict]]] = []

    async def fake_get_or_create_collection(collection_name: str, **_kwargs):
        collection_calls.append(collection_name)
        return {"name": collection_name}

    async def fake_embed(
        config: EmbeddingRuntimeConfig,
        texts: list[str],
        **kwargs,
    ) -> list[list[float]]:
        assert isinstance(config, EmbeddingRuntimeConfig)
        assert config.embedding_dimensions == 1536
        assert kwargs["dimensions"] == 1536
        embedding_calls.append((config, list(texts)))
        raise RuntimeError("embedding backend unavailable")

    async def fake_upsert(
        collection_name: str,
        item_ids: list[str],
        documents: list[str],
        embeddings: list[list[float]],
        metadatas: list[dict],
        **_kwargs,
    ) -> int:
        upsert_calls.append(
            (
                collection_name,
                list(item_ids),
                list(documents),
                [list(vector) for vector in embeddings],
                [dict(metadata) for metadata in metadatas],
            )
        )
        return len(item_ids)

    monkeypatch.setattr(organization_run, "_execute_knowledge_organization_run", fake_organization_run)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_get_or_create_collection", fake_get_or_create_collection)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.embed_texts_with_config", fake_embed)
    monkeypatch.setattr("app.core.knowledge_jobs.handlers.async_upsert_collection_items", fake_upsert)

    executor = create_default_knowledge_job_executor(session_factory=session_factory)
    consumer = KnowledgeJobConsumer(
        executor,
        session_factory=session_factory,
        poll_interval_seconds=0.01,
    )

    async def wait_for_children(expected_count: int) -> list[KnowledgeJob]:
        async def poll() -> list[KnowledgeJob]:
            while True:
                async with session_factory() as db:
                    children = await knowledge_job_crud.list_children(
                        db,
                        uid="user-1",
                        parent_job_id=parent_id,
                    )
                if len(children) >= expected_count:
                    return children
                await asyncio.sleep(0.01)

        return await asyncio.wait_for(poll(), timeout=2.0)

    parent_task = asyncio.create_task(consumer._execute(parent, parent_owner))
    try:
        children = await wait_for_children(1)
        assert organization_run_calls == [
            {
                "uid": "user-1",
                "knowledge_base_id": knowledge_base_id,
                "organization_job_id": parent_id,
            }
        ]
        assert len(children) == 1
        mutation_wrapper = children[0]
        assert mutation_wrapper.operation == KnowledgeJobOperation.ORGANIZE_MUTATION
        assert mutation_wrapper.status == KnowledgeJobStatus.PENDING
        assert mutation_wrapper.active_change_key is not None
        assert mutation_wrapper.payload["snapshot_id"] == snapshot_id
        assert mutation_wrapper.payload["stage_id"] == final_stage_id

        async with session_factory() as db:
            running_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
            assert running_parent is not None
            assert running_parent.status == KnowledgeJobStatus.RUNNING
            assert running_parent.active_change_key is not None

            mutation_job = await knowledge_job_crud.get_by_id(
                db,
                uid="user-1",
                job_id=mutation_wrapper.id,
            )
            assert mutation_job is not None
            mutation_job.available_at = await get_database_time(db)
            await db.flush()
            claimed_mutation = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=mutation_job.id,
                owner=mutation_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.ORGANIZE_MUTATION,),
                commit=False,
            )
            assert claimed_mutation is not None
            await db.commit()

        await asyncio.wait_for(
            consumer._execute(claimed_mutation, mutation_owner),
            timeout=2.0,
        )

        async with session_factory() as db:
            direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
            mutation_job = next(child for child in direct_children if child.operation == KnowledgeJobOperation.ORGANIZE_MUTATION)
            managed_update = next(child for child in direct_children if child.operation == KnowledgeJobOperation.MANAGED_UPDATE)
        assert mutation_job.status == KnowledgeJobStatus.SUCCEEDED
        assert mutation_job.active_change_key is None
        assert mutation_job.result is not None
        assert mutation_job.result["mutation_job_ids"] == [managed_update.id]
        assert managed_update.status == KnowledgeJobStatus.PENDING
        assert managed_update.active_change_key is not None
        assert not parent_task.done()

        async with session_factory() as db:
            managed_update = await knowledge_job_crud.get_by_id(
                db,
                uid="user-1",
                job_id=managed_update.id,
            )
            assert managed_update is not None
            managed_update.max_attempts = 1
            managed_update.available_at = await get_database_time(db)
            await db.flush()
            claimed_update = await knowledge_job_crud.try_claim(
                db,
                uid="user-1",
                job_id=managed_update.id,
                owner=update_owner,
                lease_seconds=60,
                enabled_operations=(KnowledgeJobOperation.MANAGED_UPDATE,),
                commit=False,
            )
            assert claimed_update is not None
            await db.commit()

        await asyncio.wait_for(
            consumer._execute(claimed_update, update_owner),
            timeout=2.0,
        )
        await asyncio.wait_for(parent_task, timeout=2.0)
    finally:
        if not parent_task.done():
            parent_task.cancel()
        await asyncio.gather(parent_task, return_exceptions=True)

    async with session_factory() as db:
        final_parent = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=parent_id)
        direct_children = await knowledge_job_crud.list_children(db, uid="user-1", parent_job_id=parent_id)
        target = await db.get(ManagedKnowledgeItem, target_id)
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base_id)

    assert final_parent is not None
    assert final_parent.status == KnowledgeJobStatus.FAILED
    assert final_parent.active_change_key is None
    assert final_parent.locked_by is None
    assert final_parent.lock_until is None
    children_by_operation = {child.operation: child for child in direct_children}
    assert {(child.operation, child.status) for child in direct_children} == {
        (KnowledgeJobOperation.ORGANIZE_MUTATION, KnowledgeJobStatus.SUCCEEDED),
        (KnowledgeJobOperation.MANAGED_UPDATE, KnowledgeJobStatus.FAILED),
    }
    assert children_by_operation[KnowledgeJobOperation.ORGANIZE_MUTATION].active_change_key is None
    failed_update = children_by_operation[KnowledgeJobOperation.MANAGED_UPDATE]
    assert failed_update.active_change_key is None
    assert failed_update.error
    assert failed_update.finished_at is not None
    assert target is not None
    assert (
        target.knowledge_key,
        target.content,
        target.version,
        target.indexed_version,
        target.is_recallable,
        list(target.vector_item_ids),
    ) == before_state
    assert target.pending_job_id is None
    assert target.organization_lock_token is None
    assert current_knowledge_base is not None
    assert current_knowledge_base.organization_error is not None

    assert collection_calls == [current_knowledge_base.active_collection_name]
    assert len(embedding_calls) == 1
    embedding_config, embedded_texts = embedding_calls[0]
    assert embedding_config.declared_dimensions == 1536
    assert embedded_texts == [target_content]
    assert upsert_calls == []
