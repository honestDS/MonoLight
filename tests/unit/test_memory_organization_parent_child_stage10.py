from __future__ import annotations

import json
from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.memory import memory_record_crud
from app.core.crud.memory_job import memory_job_crud
from app.core.memory import (
    build_memory_active_mutation_key,
    build_memory_organization_active_mutation_key,
    build_organization_merge_child_dedupe_key,
    build_organization_merge_child_payload,
)
from app.core.memory.organization import (
    MemoryOrganizationExecutionBudget,
    MemoryOrganizationExecutionRequest,
    MemoryOrganizationSnapshot,
    MemoryOrganizationValidatedItem,
    MemoryOrganizationValidatedPlan,
    MemoryOrganizationValidatedSource,
    MemoryOrganizationValidatedTarget,
)
from app.core.memory_jobs import organization_handler
from app.core.memory_jobs.executor import MemoryJobExecutionContext
from app.core.memory_jobs.manager import MemoryJobManager, MemoryJobValidationError
from app.models.memory import (
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRecordIndexStatus,
    LongTermMemoryStore,
    LongTermMemoryType,
)
from app.models.message import InternalMessage, InternalResponse, MessageRole
from app.providers.database.time import get_database_time


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[LongTermMemoryStore.__table__, LongTermMemoryMutationJob.__table__, LongTermMemoryRecord.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            yield session
    finally:
        await engine.dispose()


def _source(memory_id: int, expected_version: int, *, pinned: bool = False) -> MemoryOrganizationValidatedSource:
    return MemoryOrganizationValidatedSource(
        memory_id=memory_id,
        expected_version=expected_version,
        pinned=pinned,
    )


def _target(content: str, memory_key: str) -> MemoryOrganizationValidatedTarget:
    return MemoryOrganizationValidatedTarget(
        content=content,
        memory_key=memory_key,
        memory_type=LongTermMemoryType.FACT,
        content_token_count=3,
        content_hash=f"hash-{memory_key}",
    )


def _update(memory_id: int, expected_version: int, *, content: str, memory_key: str) -> MemoryOrganizationValidatedItem:
    return MemoryOrganizationValidatedItem(
        action="update",
        sources=(_source(memory_id, expected_version),),
        target=_target(content, memory_key),
    )


def _merge(
    source_ids: tuple[int, ...],
    *,
    primary_memory_id: int,
    content: str,
    memory_key: str,
) -> MemoryOrganizationValidatedItem:
    return MemoryOrganizationValidatedItem(
        action="merge",
        sources=tuple(_source(memory_id, memory_id + 10) for memory_id in source_ids),
        primary_memory_id=primary_memory_id,
        target=_target(content, memory_key),
    )


def _request(*, snapshot_count: int = 5) -> MemoryOrganizationExecutionRequest:
    snapshot = MemoryOrganizationSnapshot(
        digest="snapshot-digest",
        count=snapshot_count,
        active_embedding_revision=3,
        index_revision=8,
        policy_version=5,
        items=(),
    )
    return MemoryOrganizationExecutionRequest(
        trigger="manual",
        snapshot=snapshot,
        organization_model=object(),  # type: ignore[arg-type]
        messages=(InternalMessage(role=MessageRole.SYSTEM, content="system"),),
        budget=MemoryOrganizationExecutionBudget(
            required_input_tokens=10,
            available_input_tokens=10,
            context_window_tokens=1000,
            max_output_tokens=100,
            safety_margin_tokens=10,
            system_tokens=1,
            non_system_tokens=1,
            message_tokens=1,
            tools_tokens=0,
        ),
    )


def _response() -> InternalResponse:
    return InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content="unused model output",
            provider_metadata={"provider_secret": "provider-secret"},
        ),
        model="organization-model",
        usage={"prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9},
        finish_reason="stop",
        provider_metadata={"api_key": "provider-secret"},
    )


async def _claimed_parent(
    db: AsyncSession,
    *,
    uid: str,
    owner: str = "organization-worker",
    max_attempts: int = 4,
) -> LongTermMemoryMutationJob:
    db.add(LongTermMemoryStore(uid=uid))
    await db.flush()
    parent, created = await memory_job_crud.create(
        db,
        uid=uid,
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        dedupe_key=f"parent-{uid}",
        active_mutation_key=build_memory_organization_active_mutation_key(uid),
        payload={},
        available_at=await get_database_time(db),
        max_attempts=max_attempts,
        commit=False,
    )
    assert created
    assert parent.id is not None
    claimed = await memory_job_crud.try_claim(
        db,
        uid=uid,
        job_id=parent.id,
        owner=owner,
        lease_seconds=300,
        commit=False,
    )
    assert claimed is not None
    await db.commit()
    return claimed


async def _create_recallable_records(
    db: AsyncSession,
    *,
    uid: str,
    versions: dict[int, int],
) -> None:
    for memory_id, version in versions.items():
        await memory_record_crud.create(
            db,
            uid=uid,
            id=memory_id,
            memory_key=f"source-{memory_id}",
            content=f"source content {memory_id}",
            content_token_count=3,
            content_hash=f"source-hash-{memory_id}",
            version=version,
            indexed_version=version,
            vector_item_id=f"source-vector-{memory_id}",
            is_active=True,
            suppress_recall=False,
            index_status=LongTermMemoryRecordIndexStatus.READY,
            commit=False,
        )
    await db.commit()


def _install_handler_plan(
    monkeypatch: pytest.MonkeyPatch,
    request: MemoryOrganizationExecutionRequest,
    plan: MemoryOrganizationValidatedPlan,
) -> None:
    async def fake_call(_request: MemoryOrganizationExecutionRequest) -> InternalResponse:
        return _response()

    monkeypatch.setattr(organization_handler, "build_organization_execution_request", lambda _payload: request)
    monkeypatch.setattr(organization_handler, "call_organization_model", fake_call)
    monkeypatch.setattr(organization_handler, "validate_organization_model_output", lambda _output, _snapshot: plan)


@pytest.mark.asyncio
async def test_organization_parent_submits_only_mutation_groups_and_finishes_atomically(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-parent-child-user"
    parent = await _claimed_parent(db_session, uid=uid)
    await _create_recallable_records(
        db_session,
        uid=uid,
        versions={1: 11, 2: 12, 3: 13, 4: 14, 5: 15},
    )
    request = _request()
    plan = MemoryOrganizationValidatedPlan(
        items=(
            MemoryOrganizationValidatedItem(action="keep", sources=(_source(1, 11, pinned=True),)),
            _update(2, 12, content="update-content-secret", memory_key="updated-key"),
            _merge((3, 4), primary_memory_id=3, content="merge-content-secret", memory_key="merged-key"),
            MemoryOrganizationValidatedItem(
                action="conflict",
                sources=(_source(5, 15),),
                reason="conflicting facts",
            ),
        ),
        final_record_count=3,
    )
    _install_handler_plan(monkeypatch, request, plan)
    context = MemoryJobExecutionContext(
        job=parent,
        worker_id="organization-worker",
        session_factory=lambda: async_sessionmaker(db_session.bind, expire_on_commit=False)(),  # type: ignore[arg-type]
    )

    execution = await organization_handler.handle_memory_organization(context)

    assert execution.finalized is True
    assert execution.result["completion_scope"] == "plan_submitted"
    assert execution.result["child_job_ids"]
    assert execution.result["stale_count"] == 0
    assert execution.result["skipped_count"] == 1
    assert [group["status"] for group in execution.result["group_results"]] == [
        "skipped",
        "submitted",
        "submitted",
        "conflict",
    ]
    assert execution.result["group_results"][1]["primary_memory_id"] == 2
    assert execution.result["group_results"][2]["primary_memory_id"] == 3
    result_text = json.dumps(execution.result, ensure_ascii=False)
    assert "update-content-secret" not in result_text
    assert "merge-content-secret" not in result_text
    assert "provider-secret" not in result_text

    assert parent.id is not None
    current_parent = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=parent.id)
    assert current_parent is not None
    assert current_parent.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert current_parent.active_mutation_key is None
    assert current_parent.result == execution.result
    children = await memory_job_crud.list_children_by_parent_job_id(
        db_session,
        uid=uid,
        parent_job_id=parent.id,
    )
    assert [child.id for child in children] == execution.result["child_job_ids"]
    assert [child.memory_id for child in children] == [2, 3]
    assert [child.expected_version for child in children] == [12, 13]
    assert all(child.parent_job_id == parent.id for child in children)
    assert all(child.operation == LongTermMemoryMutationOperation.ORGANIZE_MERGE for child in children)
    assert [child.max_attempts for child in children] == [parent.max_attempts, parent.max_attempts]
    assert all(child.source_session_id is None and child.source_profile_id is None and child.source_message_id is None for child in children)
    assert all(child.payload["snapshot_digest"] == request.snapshot.digest for child in children)
    assert all(child.payload["active_embedding_revision"] == request.snapshot.active_embedding_revision for child in children)
    assert all(child.payload["index_revision"] == request.snapshot.index_revision for child in children)
    assert all(child.payload["policy_version"] == request.snapshot.policy_version for child in children)
    assert children[0].active_mutation_key == build_memory_active_mutation_key(uid, memory_id=2)
    assert children[1].active_mutation_key == build_memory_active_mutation_key(uid, memory_id=3)
    assert children[0].payload["target"]["content"] == "update-content-secret"
    assert children[1].payload["target"]["content"] == "merge-content-secret"
    assert set(children[0].payload) == {
        "parent_job_id",
        "snapshot_digest",
        "active_embedding_revision",
        "index_revision",
        "policy_version",
        "action",
        "sources",
        "primary_memory_id",
        "target",
    }

    cancel_parent = await MemoryJobManager().request_cancel(db_session, uid=uid, job_id=parent.id)
    assert cancel_parent.accepted is False
    cancel_child = await MemoryJobManager().request_cancel(db_session, uid=uid, job_id=children[0].id)
    assert cancel_child.accepted is True
    assert cancel_child.changed is True
    refreshed_parent = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=parent.id)
    refreshed_other_child = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=children[1].id)
    assert refreshed_parent is not None and refreshed_parent.status == LongTermMemoryMutationStatus.SUCCEEDED
    assert refreshed_other_child is not None and refreshed_other_child.status == LongTermMemoryMutationStatus.PENDING


@pytest.mark.asyncio
async def test_empty_organization_snapshot_finishes_parent_without_children(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-parent-empty-user"
    parent = await _claimed_parent(db_session, uid=uid)
    request = _request(snapshot_count=0)
    plan = MemoryOrganizationValidatedPlan(items=(), final_record_count=0)
    _install_handler_plan(monkeypatch, request, plan)
    context = MemoryJobExecutionContext(
        job=parent,
        worker_id="organization-worker",
        session_factory=lambda: async_sessionmaker(db_session.bind, expire_on_commit=False)(),  # type: ignore[arg-type]
    )

    execution = await organization_handler.handle_memory_organization(context)

    assert execution.finalized is True
    assert execution.result["completion_scope"] == "plan_submitted"
    assert execution.result["child_job_ids"] == []
    assert execution.result["group_results"] == []
    current_parent = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=parent.id)
    assert current_parent is not None and current_parent.status == LongTermMemoryMutationStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_organization_parent_marks_primary_key_collision_stale_and_continues(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-parent-stale-user"
    parent = await _claimed_parent(db_session, uid=uid)
    await _create_recallable_records(
        db_session,
        uid=uid,
        versions={1: 11, 2: 12},
    )
    blocker, created = await memory_job_crud.create(
        db_session,
        uid=uid,
        operation=LongTermMemoryMutationOperation.UPDATE,
        dedupe_key="blocking-primary-mutation",
        active_mutation_key=build_memory_active_mutation_key(uid, memory_id=1),
        memory_id=1,
        expected_version=11,
        payload={"kind": "blocking"},
        commit=False,
    )
    assert created
    assert blocker.status == LongTermMemoryMutationStatus.PENDING
    await db_session.commit()
    request = _request()
    plan = MemoryOrganizationValidatedPlan(
        items=(
            _update(1, 11, content="stale-content", memory_key="stale-key"),
            _update(2, 12, content="submitted-content", memory_key="submitted-key"),
        ),
        final_record_count=2,
    )
    _install_handler_plan(monkeypatch, request, plan)
    context = MemoryJobExecutionContext(
        job=parent,
        worker_id="organization-worker",
        session_factory=lambda: async_sessionmaker(db_session.bind, expire_on_commit=False)(),  # type: ignore[arg-type]
    )

    execution = await organization_handler.handle_memory_organization(context)

    assert execution.finalized is True
    assert execution.result["stale_count"] == 1
    assert execution.result["child_job_ids"]
    assert [group["status"] for group in execution.result["group_results"]] == ["stale", "submitted"]
    children = await memory_job_crud.list_children_by_parent_job_id(
        db_session,
        uid=uid,
        parent_job_id=parent.id,
    )
    assert len(children) == 1
    assert children[0].memory_id == 2


def test_organization_merge_child_payload_and_dedupe_are_stable_and_safe() -> None:
    item = _merge((7, 8), primary_memory_id=7, content="merge-content", memory_key="merged")
    payload = build_organization_merge_child_payload(
        item,
        parent_job_id=31,
        group_index=2,
        snapshot_digest="snapshot-digest",
        active_embedding_revision=3,
        index_revision=8,
        policy_version=5,
    )
    dedupe_key = build_organization_merge_child_dedupe_key(
        parent_job_id=31,
        group_index=2,
        payload=payload,
    )

    assert payload["primary_memory_id"] == 7
    assert payload["sources"] == [
        {"memory_id": 7, "expected_version": 17, "pinned": False},
        {"memory_id": 8, "expected_version": 18, "pinned": False},
    ]
    assert payload["target"] == {
        "content": "merge-content",
        "memory_key": "merged",
        "memory_type": LongTermMemoryType.FACT.value,
        "content_token_count": 3,
        "content_hash": "hash-merged",
    }
    assert dedupe_key == build_organization_merge_child_dedupe_key(
        parent_job_id=31,
        group_index=2,
        payload=dict(payload),
    )
    assert "uid" not in json.dumps(payload)
    assert "session" not in json.dumps(payload)
    assert "api_key" not in json.dumps(payload)


@pytest.mark.asyncio
async def test_generic_submit_rejects_organization_merge_operation(db_session: AsyncSession) -> None:
    with pytest.raises(MemoryJobValidationError):
        await MemoryJobManager().submit(
            db_session,
            uid="organization-merge-generic-user",
            operation=LongTermMemoryMutationOperation.ORGANIZE_MERGE,
            dedupe_key="generic-merge",
            payload={},
        )


@pytest.mark.asyncio
async def test_generic_organization_submit_rejects_parent_job_as_top_level_dedupe(
    db_session: AsyncSession,
) -> None:
    uid = "organization-parent-boundary-user"
    dedupe_key = "organization-parent-boundary-dedupe"
    active_mutation_key = build_memory_organization_active_mutation_key(uid)
    existing, created = await memory_job_crud.create(
        db_session,
        uid=uid,
        parent_job_id=31,
        operation=LongTermMemoryMutationOperation.ORGANIZE,
        dedupe_key=dedupe_key,
        active_mutation_key=active_mutation_key,
        payload={"kind": "parent-job"},
        commit=True,
    )
    assert created
    assert existing.parent_job_id == 31

    with pytest.raises(MemoryJobValidationError):
        await MemoryJobManager().submit(
            db_session,
            uid=uid,
            operation=LongTermMemoryMutationOperation.ORGANIZE,
            dedupe_key=dedupe_key,
            active_mutation_key=active_mutation_key,
            payload={"kind": "parent-job"},
        )
