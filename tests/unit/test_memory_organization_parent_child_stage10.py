from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from dataclasses import replace
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.channel import channel_crud
from app.core.crud.memory import memory_record_crud, memory_store_crud
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
    MemoryOrganizationPlanCheckpoint,
    MemoryOrganizationSnapshot,
    MemoryOrganizationValidatedItem,
    MemoryOrganizationValidatedPlan,
    MemoryOrganizationValidatedSource,
    MemoryOrganizationValidatedTarget,
)
from app.core.memory_jobs import organization_handler
from app.core.memory_jobs.consumer import MemoryJobConsumer
from app.core.memory_jobs.executor import MemoryJobExecutionContext, MemoryJobExecutor, MemoryJobRetryableError
from app.core.memory_jobs.manager import MemoryJobManager, MemoryJobValidationError
from app.models.channel import ModelChannel
from app.models.memory import (
    LongTermMemoryIndexStatus,
    LongTermMemoryMigrationStatus,
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
async def db_session(tmp_path) -> AsyncGenerator[AsyncSession]:
    database_path = tmp_path / "memory-organization-parent-child-stage10.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path}",
        connect_args={"timeout": 30},
    )
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[ModelChannel.__table__, LongTermMemoryStore.__table__, LongTermMemoryMutationJob.__table__, LongTermMemoryRecord.__table__],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            session.add(
                ModelChannel(
                    id=7,
                    name="memory-organization-parent-child-stage10-channel",
                    api_key="enc:v1:test-api-key",
                    model_ids=[],
                )
            )
            await session.commit()
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
        organization_model=SimpleNamespace(channel_id=7),  # type: ignore[arg-type]
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


async def _created_parent(
    db: AsyncSession,
    *,
    uid: str,
    payload: dict[str, object] | None = None,
    max_attempts: int = 4,
) -> LongTermMemoryMutationJob:
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
        payload={} if payload is None else payload,
        available_at=await get_database_time(db),
        max_attempts=max_attempts,
        commit=False,
    )
    assert created
    assert parent.id is not None
    await db.commit()
    return parent


async def _wait_for_job_status(
    db_session: AsyncSession,
    *,
    uid: str,
    job_id: int,
    status: LongTermMemoryMutationStatus,
) -> LongTermMemoryMutationJob:
    deadline = asyncio.get_running_loop().time() + 2
    while True:
        job = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=job_id)
        if job is not None and job.status == status:
            return job
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError(f"job {job_id} did not reach {status.value}")
        await asyncio.sleep(0.01)


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
@pytest.mark.parametrize("initial_status", [LongTermMemoryMutationStatus.PENDING, LongTermMemoryMutationStatus.RETRY])
async def test_pending_and_retry_organization_can_be_cancelled_and_clear_active_key(
    db_session: AsyncSession,
    initial_status: LongTermMemoryMutationStatus,
) -> None:
    uid = f"organization-cancel-{initial_status.value}-user"
    parent = await _created_parent(db_session, uid=uid)

    if initial_status == LongTermMemoryMutationStatus.RETRY:
        claimed = await memory_job_crud.try_claim(
            db_session,
            uid=uid,
            job_id=parent.id,
            owner="organization-retry-worker",
            lease_seconds=300,
            commit=False,
        )
        assert claimed is not None
        assert await memory_job_crud.release_for_retry(
            db_session,
            uid=uid,
            job_id=parent.id,
            owner="organization-retry-worker",
            delay_seconds=0,
            commit=False,
        )
        await db_session.commit()

    cancellation = await MemoryJobManager().request_cancel(db_session, uid=uid, job_id=parent.id)

    assert cancellation.accepted is True
    assert cancellation.changed is True
    assert cancellation.job is not None
    assert cancellation.job.status == LongTermMemoryMutationStatus.CANCELLED
    refreshed = await memory_job_crud.get_by_id(db_session, uid=uid, job_id=parent.id)
    assert refreshed is not None
    assert refreshed.active_mutation_key is None
    assert (
        await memory_job_crud.get_by_active_mutation_key(
            db_session,
            uid=uid,
            active_mutation_key=build_memory_organization_active_mutation_key(uid),
        )
        is None
    )


@pytest.mark.asyncio
async def test_running_organization_cancelled_during_model_call_stops_after_return_without_children(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-running-cancel-user"
    parent = await _created_parent(db_session, uid=uid)
    request = _request()
    model_started = asyncio.Event()
    release_model = asyncio.Event()
    model_calls = 0

    async def fake_call(_request: MemoryOrganizationExecutionRequest) -> InternalResponse:
        nonlocal model_calls
        model_calls += 1
        model_started.set()
        await release_model.wait()
        return _response()

    def fail_if_plan_is_validated(_output: str | None, _snapshot: MemoryOrganizationSnapshot) -> MemoryOrganizationValidatedPlan:
        raise AssertionError("cancelled organization must stop before plan validation")

    monkeypatch.setattr(organization_handler, "build_organization_execution_request", lambda _payload: request)
    monkeypatch.setattr(organization_handler, "call_organization_model", fake_call)
    monkeypatch.setattr(organization_handler, "validate_organization_model_output", fail_if_plan_is_validated)

    def session_factory() -> Any:
        return async_sessionmaker(db_session.bind, expire_on_commit=False)()  # type: ignore[arg-type]

    consumer = MemoryJobConsumer(
        MemoryJobExecutor(
            {LongTermMemoryMutationOperation.ORGANIZE: organization_handler.handle_memory_organization},
            session_factory=session_factory,
        ),
        session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=30,
        renew_interval_seconds=10,
        recovery_interval_seconds=1_000_000,
        max_concurrency=1,
        recovery_retry_delay_seconds=1,
        shutdown_retry_delay_seconds=0.01,
    )
    try:
        assert await consumer.run_once() == 1
        await asyncio.wait_for(model_started.wait(), timeout=2)
        async with session_factory() as db:
            cancellation = await MemoryJobManager().request_cancel(db, uid=uid, job_id=parent.id)
        assert cancellation.accepted is True
        assert cancellation.changed is True
        release_model.set()

        cancelled = await _wait_for_job_status(
            db_session,
            uid=uid,
            job_id=parent.id,
            status=LongTermMemoryMutationStatus.CANCELLED,
        )
        assert model_calls == 1
        assert cancelled.active_mutation_key is None
        children = await memory_job_crud.list_children_by_parent_job_id(
            db_session,
            uid=uid,
            parent_job_id=parent.id,
        )
        assert children == []
    finally:
        release_model.set()
        await consumer.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "maintenance_state",
    [
        pytest.param(
            (LongTermMemoryIndexStatus.REINDEXING, None, None),
            id="reindexing",
        ),
        pytest.param(
            (LongTermMemoryIndexStatus.READY, LongTermMemoryMigrationStatus.BUILDING, 9103),
            id="embedding-migration",
        ),
    ],
)
async def test_organization_parent_rechecks_maintenance_state_before_submitting_children(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    maintenance_state: tuple[LongTermMemoryIndexStatus, LongTermMemoryMigrationStatus | None, int | None],
) -> None:
    uid = f"organization-parent-maintenance-{maintenance_state[0].value}-{maintenance_state[1].value if maintenance_state[1] else 'none'}"
    parent = await _created_parent(db_session, uid=uid)
    await _create_recallable_records(db_session, uid=uid, versions={1: 11, 2: 12, 3: 13})
    request = _request(snapshot_count=3)
    plan = MemoryOrganizationValidatedPlan(
        items=(
            _update(1, 11, content="maintenance-update-content", memory_key="maintenance-update-key"),
            _merge((2, 3), primary_memory_id=2, content="maintenance-merge-content", memory_key="maintenance-merge-key"),
        ),
        final_record_count=2,
    )
    model_started = asyncio.Event()
    release_model = asyncio.Event()

    async def fake_call(_request: MemoryOrganizationExecutionRequest) -> InternalResponse:
        model_started.set()
        await release_model.wait()
        return _response()

    monkeypatch.setattr(organization_handler, "build_organization_execution_request", lambda _payload: request)
    monkeypatch.setattr(organization_handler, "call_organization_model", fake_call)
    monkeypatch.setattr(organization_handler, "validate_organization_model_output", lambda _output, _snapshot: plan)

    def session_factory() -> Any:
        return async_sessionmaker(db_session.bind, expire_on_commit=False)()  # type: ignore[arg-type]

    consumer = MemoryJobConsumer(
        MemoryJobExecutor(
            {LongTermMemoryMutationOperation.ORGANIZE: organization_handler.handle_memory_organization},
            session_factory=session_factory,
        ),
        session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=30,
        renew_interval_seconds=10,
        recovery_interval_seconds=1_000_000,
        max_concurrency=1,
        recovery_retry_delay_seconds=1,
        shutdown_retry_delay_seconds=0.01,
    )
    try:
        assert await consumer.run_once() == 1
        await asyncio.wait_for(model_started.wait(), timeout=2)
        index_status, migration_status, migration_job_id = maintenance_state
        async with session_factory() as maintenance_db:
            changed = await maintenance_db.execute(
                update(LongTermMemoryStore)
                .where(LongTermMemoryStore.uid == uid)
                .values(
                    index_status=index_status,
                    migration_status=migration_status,
                    migration_job_id=migration_job_id,
                )
            )
            assert changed.rowcount == 1
            await maintenance_db.commit()
        release_model.set()

        succeeded = await _wait_for_job_status(
            db_session,
            uid=uid,
            job_id=parent.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert succeeded.result is not None
        assert succeeded.result["stale_count"] == 2
        assert succeeded.result["child_job_ids"] == []
        assert [group["status"] for group in succeeded.result["group_results"]] == ["stale", "stale"]
        children = await memory_job_crud.list_children_by_parent_job_id(
            db_session,
            uid=uid,
            parent_job_id=parent.id,
        )
        assert children == []
        for memory_id in (1, 2, 3):
            record = await memory_record_crud.get_by_id(db_session, uid=uid, memory_id=memory_id)
            assert record is not None
            assert record.pending_mutation_job_id is None
    finally:
        release_model.set()
        await consumer.stop()


@pytest.mark.asyncio
async def test_organization_retry_reuses_validated_plan_checkpoint_and_frozen_snapshot(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-plan-checkpoint-retry-user"
    parent = await _created_parent(db_session, uid=uid)
    await _create_recallable_records(db_session, uid=uid, versions={1: 11})
    request = _request(snapshot_count=1)
    plan = MemoryOrganizationValidatedPlan(
        items=(_update(1, 11, content="checkpoint-content", memory_key="checkpoint-key"),),
        final_record_count=1,
    )
    model_calls = 0

    async def fake_call(_request: MemoryOrganizationExecutionRequest) -> InternalResponse:
        nonlocal model_calls
        model_calls += 1
        if model_calls > 1:
            raise AssertionError("organization retry must reuse plan_checkpoint without calling the model")
        return _response()

    def build_request(payload: object) -> MemoryOrganizationExecutionRequest:
        if not isinstance(payload, dict) or "plan_checkpoint" not in payload:
            return request
        raw_checkpoint = payload["plan_checkpoint"]
        assert isinstance(raw_checkpoint, dict)
        checkpoint = MemoryOrganizationPlanCheckpoint(
            model_output=raw_checkpoint["model_output"],
            usage=raw_checkpoint["usage"],
            finish_reason=raw_checkpoint["finish_reason"],
        )
        return replace(request, plan_checkpoint=checkpoint)

    monkeypatch.setattr(organization_handler, "build_organization_execution_request", build_request)
    monkeypatch.setattr(organization_handler, "call_organization_model", fake_call)
    monkeypatch.setattr(organization_handler, "validate_organization_model_output", lambda _output, _snapshot: plan)
    original_submit = organization_handler._submit_organization_plan

    async def fail_after_checkpoint(*_args: object, **_kwargs: object) -> object:
        raise MemoryJobRetryableError("retry after validated plan checkpoint")

    monkeypatch.setattr(organization_handler, "_submit_organization_plan", fail_after_checkpoint)

    def session_factory() -> Any:
        return async_sessionmaker(db_session.bind, expire_on_commit=False)()  # type: ignore[arg-type]

    consumer = MemoryJobConsumer(
        MemoryJobExecutor(
            {LongTermMemoryMutationOperation.ORGANIZE: organization_handler.handle_memory_organization},
            session_factory=session_factory,
        ),
        session_factory,
        poll_interval_seconds=0.01,
        lease_seconds=30,
        renew_interval_seconds=10,
        recovery_interval_seconds=1_000_000,
        max_concurrency=1,
        recovery_retry_delay_seconds=1,
        shutdown_retry_delay_seconds=0.01,
    )
    try:
        assert await consumer.run_once() == 1
        retried = await _wait_for_job_status(
            db_session,
            uid=uid,
            job_id=parent.id,
            status=LongTermMemoryMutationStatus.RETRY,
        )
        assert retried.payload["plan_checkpoint"] == {
            "model_output": _response().message.content,
            "usage": _response().usage,
            "finish_reason": _response().finish_reason,
        }

        changed = await memory_record_crud.update_if_version(
            db_session,
            uid=uid,
            memory_id=1,
            expected_version=11,
            indexed_version=12,
            commit=False,
        )
        assert changed is not None
        await db_session.commit()
        await _create_recallable_records(db_session, uid=uid, versions={2: 22})

        await db_session.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == parent.id,
            )
            .values(available_at=await get_database_time(db_session))
        )
        await db_session.commit()
        claimed = await memory_job_crud.try_claim(
            db_session,
            uid=uid,
            job_id=parent.id,
            owner="organization-expired-lease-worker",
            lease_seconds=300,
            commit=False,
        )
        assert claimed is not None
        expired_at = await get_database_time(db_session) - timedelta(seconds=1)
        await db_session.execute(
            update(LongTermMemoryMutationJob)
            .where(
                LongTermMemoryMutationJob.uid == uid,
                LongTermMemoryMutationJob.id == parent.id,
            )
            .values(lock_until=expired_at)
        )
        recovery = await memory_job_crud.recover_expired(db_session, delay_seconds=0, commit=False)
        assert recovery.retried == 1
        await db_session.commit()

        monkeypatch.setattr(organization_handler, "_submit_organization_plan", original_submit)
        assert await consumer.run_once() == 1
        succeeded = await _wait_for_job_status(
            db_session,
            uid=uid,
            job_id=parent.id,
            status=LongTermMemoryMutationStatus.SUCCEEDED,
        )
        assert model_calls == 1
        assert succeeded.result is not None
        assert succeeded.result["snapshot_digest"] == request.snapshot.digest
        assert succeeded.result["plan_summary"] == plan.plan_summary
        assert succeeded.result["stale_count"] == 1
        assert succeeded.result["child_job_ids"] == []
        children = await memory_job_crud.list_children_by_parent_job_id(
            db_session,
            uid=uid,
            parent_job_id=parent.id,
        )
        assert children == []
    finally:
        await consumer.stop()


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
    lock_order: list[str] = []
    original_channel_lock = channel_crud.lock_for_mutation
    original_store_lock = memory_store_crud.lock_for_mutation

    async def record_channel_lock(*args: Any, **kwargs: Any) -> Any:
        lock_order.append("channel")
        return await original_channel_lock(*args, **kwargs)

    async def record_store_lock(*args: Any, **kwargs: Any) -> Any:
        lock_order.append("store")
        return await original_store_lock(*args, **kwargs)

    monkeypatch.setattr(channel_crud, "lock_for_mutation", record_channel_lock)
    monkeypatch.setattr(memory_store_crud, "lock_for_mutation", record_store_lock)
    context = MemoryJobExecutionContext(
        job=parent,
        worker_id="organization-worker",
        session_factory=lambda: async_sessionmaker(db_session.bind, expire_on_commit=False)(),  # type: ignore[arg-type]
    )

    execution = await organization_handler.handle_memory_organization(context)

    assert lock_order.count("channel") >= 1
    assert lock_order.count("store") >= 1
    assert lock_order.index("channel") < lock_order.index("store")
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


@pytest.mark.asyncio
async def test_organization_parent_marks_version_changed_group_stale_and_submits_other_groups(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "organization-parent-version-stale-user"
    parent = await _claimed_parent(db_session, uid=uid)
    await _create_recallable_records(db_session, uid=uid, versions={1: 11, 2: 12})
    changed = await memory_record_crud.update_if_version(
        db_session,
        uid=uid,
        memory_id=1,
        expected_version=11,
        indexed_version=12,
        commit=False,
    )
    assert changed is not None
    await db_session.commit()
    request = _request()
    plan = MemoryOrganizationValidatedPlan(
        items=(
            _update(1, 11, content="stale-version-content", memory_key="stale-version-key"),
            _update(2, 12, content="continued-content", memory_key="continued-key"),
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
    assert [group["status"] for group in execution.result["group_results"]] == ["stale", "submitted"]
    children = await memory_job_crud.list_children_by_parent_job_id(
        db_session,
        uid=uid,
        parent_job_id=parent.id,
    )
    assert len(children) == 1
    assert children[0].memory_id == 2
    assert children[0].expected_version == 12


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
