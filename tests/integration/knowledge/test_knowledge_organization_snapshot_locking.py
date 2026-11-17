from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.constants import MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS
from app.core.crud.knowledge.embedding_transition import knowledge_base_migration_crud
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.exceptions import ResourceNotFoundException
from app.core.knowledge import (
    managed as managed_module,
)
from app.core.knowledge import (
    organization_executor as executor_module,
)
from app.core.knowledge import (
    organization_run,
)
from app.core.knowledge.managed import build_managed_knowledge_snapshot
from app.core.knowledge.organization import (
    create_knowledge_organization_snapshot,
    iter_knowledge_organization_snapshot_items,
)
from app.core.knowledge.organization_executor import execute_knowledge_organization
from app.core.knowledge.organization_lifecycle import coordinate_organization_terminal
from app.models.knowledge_base import (
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationSnapshotItem,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)
from app.providers.database.time import get_database_time
from tests.integration.knowledge import test_knowledge_organization_execution as execution_module
from tests.integration.knowledge.test_knowledge_organization_execution import (
    _add_item,
    _create_managed_container,
    _create_running_organization_job,
    _keep_plan_for_scope,
    _model,
)

session_factory = execution_module.session_factory


@pytest.mark.asyncio
async def test_snapshot_renews_job_lease_inside_freeze_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="lease-guard", content="stable content")
        job = await _create_running_organization_job(db, knowledge_base_id=knowledge_base.id, job_id_suffix="lease-guard")
        assert job.locked_by is not None

        renew_calls = []
        original_renew = knowledge_job_crud.renew_lease
        clock = 0.0

        def advancing_clock():
            nonlocal clock
            clock += 25.0
            return clock

        async def tracked_renew(db_arg, **kwargs):
            renew_calls.append((db_arg, dict(kwargs)))
            return await original_renew(db_arg, **kwargs)

        from app.core.knowledge import organization as organization_module

        monkeypatch.setattr(organization_module, "monotonic", advancing_clock)
        monkeypatch.setattr(knowledge_job_crud, "renew_lease", tracked_renew)

        await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=job.id,
        )

    assert len(renew_calls) >= 3
    assert all(db_arg is db for db_arg, _kwargs in renew_calls)
    assert all(kwargs["owner"] == job.locked_by for _db_arg, kwargs in renew_calls)
    assert all(kwargs["commit"] is False for _db_arg, kwargs in renew_calls)


@pytest.mark.asyncio
async def test_independent_lease_renewal_starts_after_snapshot_transaction(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)

    snapshot_started = asyncio.Event()
    release_snapshot = asyncio.Event()
    lease_started = asyncio.Event()

    async def blocked_snapshot(*_args, **_kwargs):
        snapshot_started.set()
        await release_snapshot.wait()
        raise RuntimeError("stop after snapshot transaction")

    async def tracked_lease(*_args, done: asyncio.Event, **_kwargs):
        lease_started.set()
        await done.wait()

    monkeypatch.setattr(organization_run, "create_knowledge_organization_snapshot", blocked_snapshot)
    monkeypatch.setattr(organization_run, "_renew_direct_organization_job_lease", tracked_lease)

    task = asyncio.create_task(
        execute_knowledge_organization(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            model_candidates=(_model(input_budget_tokens=4000),),
        )
    )
    await asyncio.wait_for(snapshot_started.wait(), timeout=1)
    await asyncio.sleep(0)
    try:
        assert lease_started.is_set() is False
    finally:
        release_snapshot.set()
    with pytest.raises(RuntimeError, match="stop after snapshot transaction"):
        await task


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", [ManagedKnowledgeActorType.USER, ManagedKnowledgeActorType.LLM])
async def test_snapshot_locks_involved_items_against_user_and_llm_mutation(session_factory, actor):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, _revision = await _add_item(db, knowledge_base_id=knowledge_base.id, key="locked", content="original content")
        knowledge_base_id = knowledge_base.id
        knowledge_id = item.id
        job = await _create_running_organization_job(db, knowledge_base_id=knowledge_base_id, job_id_suffix=f"locked-{actor.value}")
        await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            organization_job_id=job.id,
        )

        current = await db.get(ManagedKnowledgeItem, knowledge_id)
        assert current is not None
        await db.refresh(current)
        assert current.organization_lock_token == f"job:{job.id}"

    async with session_factory() as db:
        with pytest.raises(managed_module.ManagedKnowledgeConflictError):
            await managed_module.managed_knowledge_service.update(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base_id,
                knowledge_id=knowledge_id,
                expected_version=1,
                knowledge_key="locked",
                content="changed during organization",
                source_type=ManagedKnowledgeSourceType.USER_API if actor == ManagedKnowledgeActorType.USER else ManagedKnowledgeSourceType.LLM_TOOL,
                actor=actor,
            )

    async with session_factory() as db:
        with pytest.raises(managed_module.ManagedKnowledgeConflictError):
            await managed_module.managed_knowledge_service.delete(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base_id,
                knowledge_id=knowledge_id,
                expected_version=1,
                source_type=ManagedKnowledgeSourceType.USER_API if actor == ManagedKnowledgeActorType.USER else ManagedKnowledgeSourceType.LLM_TOOL,
                actor=actor,
            )


@pytest.mark.asyncio
async def test_expired_organization_job_fails_and_releases_persistent_item_locks(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, _revision = await _add_item(db, knowledge_base_id=knowledge_base.id, key="orphan-lock", content="stable content")
        job = await _create_running_organization_job(db, knowledge_base_id=knowledge_base.id, job_id_suffix="orphan")
        await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=job.id,
        )
        job.lock_until = job.started_at - timedelta(seconds=1)
        db.add(job)
        await db.commit()

    async with session_factory() as db:
        recovery = await knowledge_job_crud.recover_expired(db, max_attempts_error="expired", commit=False)
        for terminal in recovery.terminal_jobs:
            if terminal.job.id == job.id:
                assert await coordinate_organization_terminal(
                    db,
                    uid="user-1",
                    job_id=terminal.job.id,
                    error=terminal.error,
                    commit=False,
                )
        await db.commit()
        recovered = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=job.id)
        current = await db.get(ManagedKnowledgeItem, item.id)

    assert recovery.failed == 1
    assert recovery.retried == 0
    assert recovered is not None and recovered.status == KnowledgeJobStatus.FAILED
    assert current is not None and current.organization_lock_token is None


@pytest.mark.asyncio
async def test_snapshot_rejects_nonrunning_organization_job_as_lock_owner(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, _revision = await _add_item(db, knowledge_base_id=knowledge_base.id, key="invalid-owner", content="stable content")
        available_at = await get_database_time(db)
        job, created = await knowledge_job_crud.create(
            db,
            uid="user-1",
            operation=KnowledgeJobOperation.MANUAL_ORGANIZE,
            dedupe_key=f"manual-organize:{knowledge_base.id}:pending-owner",
            request_hash=hashlib.sha256(b"pending-owner").hexdigest(),
            active_change_key=f"kb-organization:{knowledge_base.id}",
            knowledge_base_id=knowledge_base.id,
            payload={"source": "test"},
            available_at=available_at,
            max_attempts=1,
        )
        assert created and job.id is not None

        with pytest.raises(managed_module.ManagedKnowledgeConflictError):
            await create_knowledge_organization_snapshot(
                db,
                uid="user-1",
                knowledge_base_id=knowledge_base.id,
                organization_job_id=job.id,
            )
        current = await db.get(ManagedKnowledgeItem, item.id)

    assert current is not None and current.organization_lock_token is None


@pytest.mark.asyncio
async def test_direct_runs_are_serialized_by_persistent_active_change_key(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)

    first_job_id, first_worker = await organization_run._create_direct_organization_job(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
    )
    with pytest.raises(executor_module.KnowledgeOrganizationExecutionError) as exc_info:
        await organization_run._create_direct_organization_job(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )
    assert exc_info.value.code == 409
    assert exc_info.value.data == {"status": "organization_target_busy", "retryable": True}

    async with session_factory() as db:
        assert await knowledge_job_crud.mark_failed(
            db,
            uid="user-1",
            job_id=first_job_id,
            owner=first_worker,
            error="test cleanup",
        )


@pytest.mark.asyncio
async def test_direct_run_reports_missing_knowledge_base_instead_of_busy(session_factory):
    with pytest.raises(ResourceNotFoundException) as exc_info:
        await organization_run._create_direct_organization_job(
            session_factory,
            uid="user-1",
            knowledge_base_id=999,
        )

    assert exc_info.value.code == 404


@pytest.mark.asyncio
async def test_direct_run_is_created_already_claimed_without_pending_claim_window(session_factory, monkeypatch: pytest.MonkeyPatch):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)

    async def unexpected_claim(*_args, **_kwargs):
        raise AssertionError("direct organization runs must not use a second-step claim")

    monkeypatch.setattr(knowledge_job_crud, "try_claim", unexpected_claim)

    job_id, worker_id = await organization_run._create_direct_organization_job(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
    )

    async with session_factory() as db:
        job = await knowledge_job_crud.get_by_id(db, uid="user-1", job_id=job_id)

    assert job is not None
    assert job.status == KnowledgeJobStatus.RUNNING
    assert job.locked_by == worker_id
    assert job.lock_until is not None
    assert job.attempt_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("fail", [False, True])
async def test_execution_holds_item_lock_until_success_or_failure_then_releases_it(session_factory, fail):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, _revision = await _add_item(db, knowledge_base_id=knowledge_base.id, key="task-lock", content="stable content")
        knowledge_base_id = knowledge_base.id
        knowledge_id = item.id

    async def model_caller(_model_config, *, scope):
        async with session_factory() as db:
            current = await db.get(ManagedKnowledgeItem, knowledge_id)
            assert current is not None
            assert current.organization_lock_token is not None
        if fail:
            raise RuntimeError("forced organization failure")
        return _keep_plan_for_scope(scope)

    if fail:
        with pytest.raises(executor_module.KnowledgeOrganizationModelFailedError):
            await execute_knowledge_organization(
                session_factory,
                uid="user-1",
                knowledge_base_id=knowledge_base_id,
                model_candidates=(_model(input_budget_tokens=4000),),
                model_caller=model_caller,
                semantic_neighbor_loader=lambda _scope, _collection: {},
            )
    else:
        await execute_knowledge_organization(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            model_candidates=(_model(input_budget_tokens=4000),),
            model_caller=model_caller,
            semantic_neighbor_loader=lambda _scope, _collection: {},
        )

    async with session_factory() as db:
        current = await db.get(ManagedKnowledgeItem, knowledge_id)
        assert current is not None
        assert current.organization_lock_token is None


def test_managed_knowledge_limit_and_database_text_type(monkeypatch: pytest.MonkeyPatch):
    assert MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS == 16384
    assert ManagedKnowledgeItem.__table__.c.content.type.compile(dialect=mysql.dialect()) == "LONGTEXT"
    assert ManagedKnowledgeItem.__table__.c.content.type.compile(dialect=sqlite.dialect()) == "TEXT"

    monkeypatch.setattr(managed_module, "estimate_tokens", lambda _value: 16384)
    _, token_count, _ = managed_module._normalize_content("large complete knowledge")
    assert token_count == 16384

    monkeypatch.setattr(managed_module, "estimate_tokens", lambda _value: 16385)
    with pytest.raises(Exception) as exc_info:
        managed_module._normalize_content("too large knowledge")
    assert getattr(exc_info.value, "data", {}).get("max_tokens") == 16384


@pytest.mark.asyncio
async def test_snapshot_persists_items_as_rows_and_resolves_content_in_pages(session_factory, monkeypatch: pytest.MonkeyPatch):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        for index in range(5):
            await _add_item(
                db,
                knowledge_base_id=knowledge_base.id,
                key=f"topic-{index}",
                content=f"complete content {index}",
            )

        import app.core.knowledge.organization as organization_module

        monkeypatch.setattr(organization_module, "KNOWLEDGE_ORGANIZATION_SNAPSHOT_PAGE_SIZE", 2)
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )

        assert snapshot.item_count == 5
        assert snapshot.items == []
        persisted_rows = list((await db.execute(select(KnowledgeOrganizationSnapshotItem).where(KnowledgeOrganizationSnapshotItem.snapshot_id == snapshot.id).order_by(KnowledgeOrganizationSnapshotItem.sequence))).scalars().all())
        assert [row.sequence for row in persisted_rows] == list(range(5))
        assert [row.knowledge_key for row in persisted_rows] == [f"topic-{index}" for index in range(5)]

        resolved = [
            item
            async for item in iter_knowledge_organization_snapshot_items(
                db,
                snapshot=snapshot,
                page_size=2,
            )
        ]
        assert [item["knowledge_key"] for item in resolved] == [f"topic-{index}" for index in range(5)]
        assert [item["content"] for item in resolved] == [f"complete content {index}" for index in range(5)]


@pytest.mark.asyncio
async def test_snapshot_keeps_frozen_revision_after_current_item_changes(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, revision = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            key="frozen-topic",
            content="content at frozen boundary",
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )

        item.content = "newer content outside frozen snapshot"
        item.content_hash = hashlib.sha256(item.content.encode("utf-8")).hexdigest()
        item.content_token_count = 5
        item.version = 2
        item.indexed_version = 2
        revision_v2 = ManagedKnowledgeRevision(
            knowledge_base_id=knowledge_base.id,
            uid="user-1",
            knowledge_id=item.id,
            version=2,
            operation=ManagedKnowledgeRevisionOperation.UPDATE,
            before_snapshot=revision.after_snapshot,
            after_snapshot=build_managed_knowledge_snapshot(item),
            source_type=ManagedKnowledgeSourceType.LLM_TOOL,
            source_reference={"source": "frozen-topic"},
            modified_by=ManagedKnowledgeActorType.LLM,
        )
        db.add(revision_v2)
        await db.commit()

        resolved = [item async for item in iter_knowledge_organization_snapshot_items(db, snapshot=snapshot, page_size=1)]
        assert len(resolved) == 1
        assert resolved[0]["expected_version"] == 1
        assert resolved[0]["content"] == "content at frozen boundary"
        assert resolved[0]["content_reference"] == {"revision_id": revision.id, "version": 1}


@pytest.mark.asyncio
async def test_snapshot_preparation_rejects_legacy_partial_scope_without_expanding(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        first, _first_revision = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            key="legacy-scope-first",
            content="first knowledge",
        )
        second, _second_revision = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            key="legacy-scope-second",
            content="second knowledge",
        )
        job = await _create_running_organization_job(db, knowledge_base_id=knowledge_base.id, job_id_suffix="legacy-scope")
        job.payload = {**job.payload, "knowledge_ids": [first.id]}
        await db.commit()

        knowledge_base_id = knowledge_base.id
        job_id = job.id
        knowledge_ids = (first.id, second.id)

    with pytest.raises(ValueError):
        await organization_run._prepare_knowledge_organization_snapshot(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            organization_job_id=job_id,
        )

    async with session_factory() as db:
        snapshots = list((await db.execute(select(KnowledgeOrganizationSnapshot))).scalars())
        snapshot_items = list((await db.execute(select(KnowledgeOrganizationSnapshotItem))).scalars())
        items = list((await db.execute(select(ManagedKnowledgeItem).where(ManagedKnowledgeItem.id.in_(knowledge_ids)).order_by(ManagedKnowledgeItem.id))).scalars())
        current_job = await db.get(KnowledgeJob, job_id)

    assert snapshots == []
    assert snapshot_items == []
    assert [item.organization_lock_token for item in items] == [None, None]
    assert current_job is not None
    assert current_job.payload == {"source": "test", "knowledge_ids": [knowledge_ids[0]]}


@pytest.mark.asyncio
async def test_snapshot_uses_current_index_vector_ids_without_mutating_content_revision(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, revision = await _add_item(db, knowledge_base_id=knowledge_base.id, key="migrated-index", content="stable content")
        original_revision_snapshot = dict(revision.after_snapshot)
        item.vector_item_ids = ["new-active-vector-1", "new-active-vector-2"]
        item.indexed_version = item.version
        knowledge_base.active_embedding_revision += 1
        knowledge_base.index_revision += 1
        await db.commit()

        snapshot = await create_knowledge_organization_snapshot(db, uid="user-1", knowledge_base_id=knowledge_base.id)
        resolved = [entry async for entry in iter_knowledge_organization_snapshot_items(db, snapshot=snapshot)]
        await db.refresh(revision)

    assert resolved[0]["vector_item_ids"] == ["new-active-vector-1", "new-active-vector-2"]
    assert revision.after_snapshot == original_revision_snapshot


@pytest.mark.asyncio
async def test_embedding_switch_cannot_rebind_vectors_while_item_is_organization_locked(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, _revision = await _add_item(db, knowledge_base_id=knowledge_base.id, key="migration-lock", content="stable content")
        job = await _create_running_organization_job(db, knowledge_base_id=knowledge_base.id, job_id_suffix="migration-lock")
        await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=job.id,
        )

        changed = await knowledge_base_migration_crud.update_managed_vectors_batch(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            updates=[(item.id, item.version, ["migration-vector"])],
        )

    assert changed is False
