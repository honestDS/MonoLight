from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS
from app.core.crud.knowledge.embedding_transition import knowledge_base_migration_crud
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.exceptions import BaseBusinessException, LLMException, ParameterException, ResourceNotFoundException, ServerException
from app.core.knowledge import (
    managed as managed_module,
)
from app.core.knowledge import (
    organization_analysis,
    organization_plan,
    organization_reduction,
    organization_run,
    organization_scope,
    organization_stages,
)
from app.core.knowledge import (
    organization_executor as executor_module,
)
from app.core.knowledge.managed import build_managed_knowledge_snapshot
from app.core.knowledge.organization import (
    create_knowledge_organization_snapshot,
    iter_knowledge_organization_snapshot_items,
)
from app.core.knowledge.organization_executor import (
    KnowledgeOrganizationContextExceededError,
    KnowledgeOrganizationScopeItem,
    execute_knowledge_organization,
    run_bounded_knowledge_organization_pipeline,
)
from app.core.knowledge.organization_lifecycle import coordinate_organization_terminal
from app.core.knowledge.organization_runtime import (
    KnowledgeOrganizationCandidate,
    KnowledgeOrganizationModelConfig,
    build_semantic_fragment_groups,
    call_knowledge_organization_model,
    load_knowledge_organization_model_candidates,
    load_vector_semantic_neighbors,
    split_content_for_analysis,
    validate_knowledge_organization_plan,
)
from app.core.knowledge.organization_types import KnowledgeOrganizationPlan
from app.core.prompts import KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT
from app.core.utils.tokenizer import estimate_tokens
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
    KnowledgeOrganizationFragment,
    KnowledgeOrganizationSnapshot,
    KnowledgeOrganizationSnapshotItem,
    KnowledgeOrganizationStage,
    KnowledgeOrganizationStageStatus,
    ManagedKnowledgeActorType,
    ManagedKnowledgeItem,
    ManagedKnowledgeRevision,
    ManagedKnowledgeRevisionOperation,
    ManagedKnowledgeSourceType,
)
from app.models.memory import LongTermMemoryStore
from app.models.message import InternalMessage, InternalResponse, MessageRole
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.providers.database.time import get_database_time
from scripts import migration_20260910_add_knowledge_organization_stage as organization_stage_migration
from scripts import migration_20260911_add_knowledge_organization_snapshot_items as organization_snapshot_item_migration
from tests.database_support import clone_sqlite_schema

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
    LongTermMemoryStore.__table__,
)


def test_organization_errors_follow_project_business_exception_hierarchy():
    context_error = executor_module.KnowledgeOrganizationContextExceededError()
    config_error = executor_module.KnowledgeOrganizationConfigurationError()
    model_error = executor_module.KnowledgeOrganizationModelFailedError()
    convergence_error = executor_module.KnowledgeOrganizationNotConvergedError()
    execution_error = executor_module.KnowledgeOrganizationExecutionError()

    assert isinstance(context_error, ParameterException)
    assert isinstance(config_error, ParameterException)
    assert isinstance(model_error, LLMException)
    assert isinstance(convergence_error, ServerException)
    assert isinstance(execution_error, ServerException)
    errors = (context_error, config_error, model_error, convergence_error, execution_error)
    assert [error.code for error in errors] == [400, 400, 502, 500, 500]
    assert all(isinstance(error.code, int) for error in errors)


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "knowledge-organization-knowledge_organization_execution.db"
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

    await clone_sqlite_schema(database_path, tables=_TABLES)

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _create_managed_container(db: AsyncSession) -> KnowledgeBase:
    channel = ModelChannel(
        name="organization-knowledge_organization_execution",
        api_key="test-key",
        base_url="https://example.invalid",
        model_ids=[],
    )
    db.add(channel)
    await db.flush()
    prompt = PromptLibrary(uid="user-1", name="organization-knowledge_organization_execution", content="prompt")
    db.add(prompt)
    await db.flush()
    profile = Profile(uid="user-1", name="organization-knowledge_organization_execution", prompt_id=prompt.id, configs={})
    db.add(profile)
    await db.flush()
    knowledge_base = KnowledgeBase(
        uid="user-1",
        name="managed-knowledge_organization_execution",
        embedding_channel_id=channel.id,
        embedding_model_id="embedding-model",
        embedding_dimensions=1536,
        collection_name="managed-knowledge_organization_execution-collection",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=channel.id,
        active_embedding_model_id="embedding-model",
        active_embedding_dimensions=1536,
        active_embedding_signature="embedding-signature",
        active_embedding_revision=3,
        active_collection_name="managed-knowledge_organization_execution-active",
        index_revision=5,
    )
    db.add(knowledge_base)
    await db.commit()
    await db.refresh(knowledge_base)
    return knowledge_base


async def _add_item(db: AsyncSession, *, knowledge_base_id: int, key: str, content: str) -> tuple[ManagedKnowledgeItem, ManagedKnowledgeRevision]:
    item = ManagedKnowledgeItem(
        knowledge_base_id=knowledge_base_id,
        uid="user-1",
        knowledge_key=key,
        content=content,
        content_token_count=max(1, len(content.split())),
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        version=1,
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        source_reference={"source": key},
        created_by=ManagedKnowledgeActorType.LLM,
        last_modified_by=ManagedKnowledgeActorType.LLM,
        llm_maintainable=True,
        indexed_version=1,
        vector_item_ids=[f"managed-vector-{key}"],
        is_recallable=True,
    )
    db.add(item)
    await db.flush()
    revision = ManagedKnowledgeRevision(
        knowledge_base_id=knowledge_base_id,
        uid="user-1",
        knowledge_id=item.id,
        version=1,
        operation=ManagedKnowledgeRevisionOperation.CREATE,
        after_snapshot=build_managed_knowledge_snapshot(item),
        source_type=ManagedKnowledgeSourceType.LLM_TOOL,
        source_reference={"source": key},
        modified_by=ManagedKnowledgeActorType.LLM,
    )
    db.add(revision)
    await db.commit()
    await db.refresh(item)
    await db.refresh(revision)
    return item, revision


async def _create_running_organization_job(db: AsyncSession, *, knowledge_base_id: int, job_id_suffix: str = "test") -> KnowledgeJob:
    available_at = await get_database_time(db)
    job, created = await knowledge_job_crud.create(
        db,
        uid="user-1",
        operation=KnowledgeJobOperation.MANUAL_ORGANIZE,
        dedupe_key=f"manual-organize:{knowledge_base_id}:{job_id_suffix}",
        request_hash=hashlib.sha256(job_id_suffix.encode("utf-8")).hexdigest(),
        active_change_key=f"kb-organization:{knowledge_base_id}",
        knowledge_base_id=knowledge_base_id,
        payload={"source": "test"},
        available_at=available_at,
        max_attempts=1,
    )
    assert created and job.id is not None
    claimed = await knowledge_job_crud.try_claim(
        db,
        uid="user-1",
        job_id=job.id,
        owner=f"worker-{job_id_suffix}",
        lease_seconds=60,
        enabled_operations=(KnowledgeJobOperation.MANUAL_ORGANIZE,),
    )
    assert claimed is not None
    return claimed


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


def _candidate(knowledge_id: int, key: str, content: str, *, source: str | None = None) -> KnowledgeOrganizationCandidate:
    return KnowledgeOrganizationCandidate(
        knowledge_id=knowledge_id,
        expected_version=1,
        knowledge_key=key,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        content_token_count=max(1, len(content.split())),
        source_type="llm_tool",
        source_reference={"source": source} if source is not None else None,
        vector_item_ids=(f"vector-{knowledge_id}",),
    )


def _model(*, input_budget_tokens: int = 2000) -> KnowledgeOrganizationModelConfig:
    return KnowledgeOrganizationModelConfig(
        channel_id=1,
        channel_name="organization",
        model_id="model-a",
        protocol="openai",
        base_url="https://example.invalid",
        api_key="test-key",
        http_proxy=None,
        custom_headers={},
        temperature=0.1,
        top_p=None,
        timeout=60.0,
        context_window_tokens=input_budget_tokens + 1024,
        max_output_tokens=768,
        safety_margin_tokens=256,
        system_prompt_tokens=0,
    )


def test_semantic_groups_use_neighbors_source_and_budget():
    candidates = (
        _candidate(1, "postgres-index", "postgres index tuning"),
        _candidate(2, "database-index", "database btree tuning"),
        _candidate(3, "redis-cache", "redis cache policy", source="cache-doc"),
        _candidate(4, "cache-expiry", "cache ttl policy", source="cache-doc"),
    )
    groups = build_semantic_fragment_groups(
        candidates,
        model=_model(input_budget_tokens=2000),
        semantic_neighbors={1: {2}, 2: {1}},
    )
    assert [tuple(candidate.knowledge_id for candidate in group.candidates) for group in groups] == [(1, 2), (3, 4)]

    tiny_budget_groups = build_semantic_fragment_groups(
        candidates[:2],
        model=_model(input_budget_tokens=120),
        semantic_neighbors={1: {2}, 2: {1}},
    )
    assert len(tiny_budget_groups) == 2
    assert [group.candidates[0].knowledge_id for group in tiny_budget_groups] == [1, 2]


def test_internal_analysis_split_covers_complete_content_without_truncation():
    content = " ".join(f"fact-{index}" for index in range(200))
    parts = split_content_for_analysis(content, max_tokens=40)
    assert len(parts) > 1
    assert "".join(parts) == content
    assert all(part for part in parts)


def test_plan_validation_enforces_scope_coverage_versions_and_target_conflicts():
    candidates = (
        _candidate(1, "topic-a", "alpha knowledge"),
        _candidate(2, "topic-b", "beta knowledge"),
    )
    valid = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "topic-ab", "content": "merged knowledge"},
                    "summary": "alpha and beta describe one topic",
                }
            ]
        }
    )
    validated = validate_knowledge_organization_plan(valid, candidates=candidates)
    assert validated.items[0].action == "merge"

    missing_source = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "keep",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "summary": "alpha",
                }
            ]
        }
    )
    with pytest.raises(ValueError):
        validate_knowledge_organization_plan(missing_source, candidates=candidates)

    wrong_version = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "keep",
                    "source": {"knowledge_id": 1, "expected_version": 2},
                    "summary": "alpha",
                },
                {
                    "action": "keep",
                    "source": {"knowledge_id": 2, "expected_version": 1},
                    "summary": "beta",
                },
            ]
        }
    )
    with pytest.raises(ValueError):
        validate_knowledge_organization_plan(wrong_version, candidates=candidates)

    duplicate_target = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "update",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "target": {"knowledge_key": "same-key", "content": "same content"},
                    "summary": "first",
                },
                {
                    "action": "update",
                    "source": {"knowledge_id": 2, "expected_version": 1},
                    "target": {"knowledge_key": "same-key", "content": "same content"},
                    "summary": "second",
                },
            ]
        }
    )
    with pytest.raises(ValueError):
        validate_knowledge_organization_plan(duplicate_target, candidates=candidates)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("context_window_k", "max_tokens"),
    [(64, 0), (1, 700)],
    ids=["zero-output-budget", "no-organization-input-budget"],
)
async def test_model_config_rejects_unusable_budget_before_execution(
    session_factory: async_sessionmaker[AsyncSession],
    context_window_k: int,
    max_tokens: int,
):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        knowledge_base_id = knowledge_base.id
        channel = ModelChannel(
            name=f"invalid-organization-budget-{context_window_k}-{max_tokens}",
            api_key="secret-api-key",
            base_url="https://example.invalid",
            model_ids=[
                {
                    "model_id": "primary",
                    "usage": "CHAT",
                    "protocol": "OPENAI",
                    "context_window_k": context_window_k,
                    "max_tokens": max_tokens,
                    "is_enabled": True,
                }
            ],
        )
        db.add(channel)
        await db.flush()
        db.add(
            LongTermMemoryStore(
                uid="user-1",
                organization_channel_id=channel.id,
                organization_model_id="primary",
            )
        )
        await db.commit()

        with pytest.raises(ValueError):
            await load_knowledge_organization_model_candidates(db, uid="user-1")

    with pytest.raises(executor_module.KnowledgeOrganizationConfigurationError):
        await execute_knowledge_organization(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
        )


@pytest.mark.asyncio
async def test_model_candidates_are_resolved_from_current_config_without_persisting_connection_secrets(session_factory):
    async with session_factory() as db:
        channel = ModelChannel(
            name="organization-models",
            api_key="secret-api-key",
            base_url="https://example.invalid",
            model_ids=[
                {
                    "model_id": "fallback-1",
                    "usage": "CHAT",
                    "protocol": "OPENAI",
                    "context_window_k": 64,
                    "max_tokens": 4096,
                    "is_enabled": True,
                },
                {
                    "model_id": "primary",
                    "usage": "CHAT",
                    "protocol": "OPENAI",
                    "context_window_k": 128,
                    "max_tokens": 8192,
                    "is_enabled": True,
                },
                {
                    "model_id": "disabled",
                    "usage": "CHAT",
                    "protocol": "OPENAI",
                    "context_window_k": 64,
                    "max_tokens": 4096,
                    "is_enabled": False,
                },
                {
                    "model_id": "embedding",
                    "usage": "EMBEDDING",
                    "protocol": "OPENAI_EMBEDDING",
                    "embedding_dimensions": 16,
                    "is_enabled": True,
                },
                {
                    "model_id": "fallback-2",
                    "usage": "CHAT",
                    "protocol": "OPENAI_RESPONSES",
                    "context_window_k": 32,
                    "max_tokens": 2048,
                    "is_enabled": True,
                },
            ],
        )
        db.add(channel)
        await db.flush()
        db.add(
            LongTermMemoryStore(
                uid="user-1",
                organization_channel_id=channel.id,
                organization_model_id="primary",
            )
        )
        await db.commit()

        candidates = await load_knowledge_organization_model_candidates(db, uid="user-1")
        assert [candidate.model_id for candidate in candidates] == ["primary"]
        assert candidates[0].context_window_tokens == 128000
        assert candidates[0].max_output_tokens == 8192

        primary = candidates[0]
        transport_changed = replace(
            primary,
            base_url="https://replacement.invalid",
            api_key="replacement-key",
            http_proxy="http://127.0.0.1:8080",
            custom_headers={"x-route": "replacement"},
        )
        runtime_changed = replace(
            primary,
            temperature=0.8,
            top_p=0.7,
            context_window_tokens=64000,
            max_output_tokens=4096,
        )
        stage_snapshot = organization_stages._stage_model_snapshot(primary, purpose="initial")
        assert stage_snapshot == {
            "execution_model": {"channel_id": channel.id, "model_id": "primary", "protocol": primary.protocol},
            "purpose": "initial",
        }
        assert "secret-api-key" not in str(stage_snapshot)
        assert organization_stages._model_key(transport_changed) == organization_stages._model_key(primary)
        assert organization_stages._model_key(runtime_changed) == organization_stages._model_key(primary)
        assert organization_stages._model_key(replace(primary, model_id="replacement-model")) != organization_stages._model_key(primary)


@pytest.mark.asyncio
async def test_model_call_uses_full_candidate_scope_and_strict_json(monkeypatch: pytest.MonkeyPatch):
    candidates = (
        _candidate(1, "topic-a", "alpha knowledge"),
        _candidate(2, "topic-b", "beta knowledge"),
    )
    captured = {}

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content='{"items":[{"action":"keep","source":{"knowledge_id":1,"expected_version":1},"summary":"alpha"},{"action":"keep","source":{"knowledge_id":2,"expected_version":1},"summary":"beta"}]}',
            ),
            model="model-a",
        )

    import app.core.knowledge.organization_runtime as runtime_module

    monkeypatch.setattr(runtime_module.LLMClient, "generate", fake_generate)
    plan = await call_knowledge_organization_model(_model(), candidates=candidates)
    assert len(plan.items) == 2
    assert captured["model_id"] == "model-a"
    assert captured["tools"] is None
    assert captured["max_tokens"] == 768
    request_text = captured["messages"][1].content
    assert isinstance(request_text, str)
    assert "alpha knowledge" in request_text
    assert "beta knowledge" in request_text


@pytest.mark.asyncio
async def test_vector_neighbors_only_accept_snapshot_ids_and_versions(monkeypatch: pytest.MonkeyPatch):
    candidates = (
        _candidate(1, "topic-a", "alpha knowledge"),
        _candidate(2, "topic-b", "beta knowledge"),
    )

    async def fake_get_by_ids(_collection_name, ids, include=None):
        assert include == ["embeddings"]
        return {"ids": list(ids), "embeddings": [[1.0, 0.0] for _ in ids]}

    async def fake_query(_collection_name, _query_embedding, n_results=1, include=None):
        assert n_results == 8
        return {
            "ids": [["a", "b", "c"]],
            "metadatas": [
                [
                    {"managed_knowledge_id": 2, "managed_knowledge_version": 1},
                    {"managed_knowledge_id": 2, "managed_knowledge_version": 2},
                    {"managed_knowledge_id": 999, "managed_knowledge_version": 1},
                ]
            ],
        }

    import app.core.knowledge.organization_runtime as runtime_module

    monkeypatch.setattr(runtime_module, "async_get_collection_items_by_ids", fake_get_by_ids)
    monkeypatch.setattr(runtime_module, "async_query_collection", fake_query)
    neighbors = await load_vector_semantic_neighbors(
        candidates,
        collection_name="managed-knowledge_organization_execution-active",
    )
    assert neighbors == {1: frozenset({2}), 2: frozenset({1})}


@pytest.mark.asyncio
async def test_bounded_pipeline_persists_in_order_with_bounded_concurrency():
    async def inputs():
        for index in range(12):
            yield index

    active = 0
    max_active = 0
    persisted: list[int] = []

    async def process(index: int) -> tuple[int, str]:
        nonlocal active, max_active
        active += 1
        max_active = max(max_active, active)
        await asyncio.sleep(0.002 * (12 - index))
        active -= 1
        return index, f"result-{index}"

    async def persist(result: tuple[int, str]) -> None:
        persisted.append(result[0])

    stats = await run_bounded_knowledge_organization_pipeline(
        inputs=inputs(),
        expected_count=12,
        process=process,
        persist=persist,
    )
    assert persisted == list(range(12))
    assert max_active <= 4
    assert stats.max_active_tasks <= 4
    assert stats.max_input_queue_size <= 8
    assert stats.max_result_queue_size <= 8
    assert stats.max_reorder_size <= 8


def _keep_plan_for_scope(scope: tuple[KnowledgeOrganizationScopeItem, ...]) -> KnowledgeOrganizationPlan:
    items = []
    for input_item in scope:
        for knowledge_id, expected_version in input_item.sources:
            items.append(
                {
                    "action": "keep",
                    "source": {"knowledge_id": knowledge_id, "expected_version": expected_version},
                    "summary": f"summary-{knowledge_id}",
                }
            )
    return KnowledgeOrganizationPlan.model_validate({"items": items})


@pytest.mark.asyncio
async def test_small_snapshot_calls_model_once_and_persists_one_completed_fragment(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="one", content="alpha")
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="two", content="beta")

    calls: list[tuple[str, int]] = []

    async def model_caller(model, *, scope):
        calls.append((model.model_id, len(scope)))
        return _keep_plan_for_scope(scope)

    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(_model(input_budget_tokens=4000),),
        model_caller=model_caller,
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    assert calls == [("model-a", 2)]
    assert len(result.plan.items) == 2
    assert result.stage_count == 1

    async with session_factory() as db:
        stages = list((await db.execute(select(KnowledgeOrganizationStage))).scalars().all())
        fragments = list((await db.execute(select(KnowledgeOrganizationFragment))).scalars().all())
        assert len(stages) == 1
        assert stages[0].status == KnowledgeOrganizationStageStatus.COMPLETED
        assert "frozen_candidates" not in stages[0].model_snapshot
        assert stages[0].model_snapshot["execution_model"] == {
            "channel_id": 1,
            "model_id": "model-a",
            "protocol": "openai",
        }
        assert len(fragments) == 1


@pytest.mark.asyncio
async def test_large_snapshot_uses_multiple_fragments_then_merges_to_one_final_plan(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        for index in range(5):
            await _add_item(
                db,
                knowledge_base_id=knowledge_base.id,
                key=f"large-{index}",
                content=(f"fact-{index} " * 120).strip(),
            )

    calls: list[tuple[int, int]] = []

    async def model_caller(_model_config, *, scope):
        calls.append((len(calls), len(scope)))
        items = []
        for input_item in scope:
            for knowledge_id, expected_version in input_item.sources:
                items.append(
                    {
                        "action": "keep",
                        "source": {"knowledge_id": knowledge_id, "expected_version": expected_version},
                        "summary": f"s{knowledge_id}" if input_item.source_type == "organization_fragment" else f"summary-{knowledge_id}",
                    }
                )
        return KnowledgeOrganizationPlan.model_validate({"items": items})

    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(_model(input_budget_tokens=450),),
        model_caller=model_caller,
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    assert result.stage_count >= 2
    assert len(calls) > 1
    assert len(result.plan.items) == 5
    assert {item.source.knowledge_id for item in result.plan.items} == {1, 2, 3, 4, 5}

    async with session_factory() as db:
        stages = list((await db.execute(select(KnowledgeOrganizationStage).order_by(KnowledgeOrganizationStage.stage_index))).scalars().all())
    assert stages[-1].expected_fragment_count == 1


@pytest.mark.asyncio
async def test_initial_grouping_reuses_one_frozen_neighbor_result_for_count_and_execution(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        for index in range(3):
            await _add_item(
                db,
                knowledge_base_id=knowledge_base.id,
                key=f"neighbor-{index}",
                content=(f"fact{index} " * 20).strip(),
            )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )

    neighbor_calls = 0

    async def changing_neighbors(_scope, _collection):
        nonlocal neighbor_calls
        neighbor_calls += 1
        if neighbor_calls == 1:
            return {1: {2}, 2: {1}}
        return {}

    stage_result, analysis_stage_count = await organization_plan._execute_plan_stage_for_model(
        session_factory,
        snapshot=snapshot,
        work_key=organization_run._work_key(snapshot, organization_job_id=1),
        stage_index=0,
        lower_stage=None,
        model=_model(input_budget_tokens=250),
        collection_name=knowledge_base.active_collection_name,
        model_caller=lambda _model_config, *, scope: _keep_plan_for_scope(scope),
        analysis_caller=lambda _model_config, *, content: content,
        semantic_neighbor_loader=changing_neighbors,
    )

    assert neighbor_calls == 1
    assert analysis_stage_count == 0
    assert stage_result.stage.expected_fragment_count == 2
    async with session_factory() as db:
        persisted_stage = await db.get(KnowledgeOrganizationStage, stage_result.stage.id)
    assert persisted_stage is not None
    assert persisted_stage.status == KnowledgeOrganizationStageStatus.COMPLETED


@pytest.mark.asyncio
async def test_reduction_keep_preserves_lower_update_action(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="alpha", content=("alpha detail " * 120).strip())
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="beta", content=("beta detail " * 120).strip())

    async def model_caller(_model_config, *, scope):
        if all(item.source_type != "organization_fragment" for item in scope):
            items = []
            for input_item in scope:
                knowledge_id, expected_version = input_item.sources[0]
                if knowledge_id == 1:
                    items.append(
                        {
                            "action": "update",
                            "source": {"knowledge_id": knowledge_id, "expected_version": expected_version},
                            "target": {"knowledge_key": "alpha-updated", "content": "updated alpha content"},
                            "summary": "updated alpha",
                        }
                    )
                else:
                    items.append(
                        {
                            "action": "keep",
                            "source": {"knowledge_id": knowledge_id, "expected_version": expected_version},
                            "summary": "kept beta",
                        }
                    )
            return KnowledgeOrganizationPlan.model_validate({"items": items})

        return KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "keep",
                        "source": {"knowledge_id": knowledge_id, "expected_version": expected_version},
                        "summary": "x",
                    }
                    for input_item in scope
                    for knowledge_id, expected_version in input_item.sources
                ]
            }
        )

    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(_model(input_budget_tokens=360),),
        model_caller=model_caller,
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    actions = {item.source.knowledge_id: item for item in result.plan.items}
    assert actions[1].action == "update"
    assert actions[1].target.knowledge_key == "alpha-updated"
    assert actions[1].target.content == "updated alpha content"
    assert actions[2].action == "keep"


@pytest.mark.asyncio
async def test_reduction_can_merge_multiple_lower_actions_without_new_action_types(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="alpha", content=("alpha detail " * 120).strip())
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="beta", content=("beta detail " * 120).strip())

    async def model_caller(_model_config, *, scope):
        if all(item.source_type != "organization_fragment" for item in scope):
            return _keep_plan_for_scope(scope)
        sources = [{"knowledge_id": knowledge_id, "expected_version": expected_version} for input_item in scope for knowledge_id, expected_version in input_item.sources]
        if len(sources) == 1:
            return KnowledgeOrganizationPlan.model_validate({"items": [{"action": "keep", "source": sources[0], "summary": "x"}]})
        return KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "merge",
                        "sources": sources,
                        "primary_knowledge_id": sources[0]["knowledge_id"],
                        "target": {"knowledge_key": "alpha-beta", "content": "merged alpha beta content"},
                        "summary": "merged",
                    }
                ]
            }
        )

    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(_model(input_budget_tokens=360),),
        model_caller=model_caller,
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    assert len(result.plan.items) == 1
    assert result.plan.items[0].action == "merge"
    assert {source.knowledge_id for source in result.plan.items[0].sources} == {1, 2}
    assert {item.action for item in result.plan.items} <= {"keep", "update", "merge", "conflict"}


def test_reduction_preserves_existing_merge_without_new_action_type():
    lower_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "alpha-beta", "content": "canonical merged alpha beta content"},
                    "summary": "lower merged summary",
                }
            ]
        }
    )
    lower_item = lower_plan.items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1), (2, 1)),
            knowledge_key="alpha-beta",
            content="lower merged summary",
            content_hash=hashlib.sha256(b"canonical merged alpha beta content").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_item,
        ),
    )
    upper_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "alpha-beta", "content": "lower merged summary"},
                    "summary": "still merged",
                }
            ]
        }
    )

    effective_plan, output_scope = organization_reduction._compose_scope_plan(upper_plan, scope=scope)

    assert len(effective_plan.items) == 1
    assert effective_plan.items[0].action == "merge"
    assert effective_plan.items[0].target.content == "canonical merged alpha beta content"
    assert output_scope[0].effective_item == effective_plan.items[0]
    assert {item.action for item in effective_plan.items} <= {"keep", "update", "merge", "conflict"}


def test_reduction_new_single_source_update_replaces_lower_update():
    lower_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "update",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "target": {"knowledge_key": "topic-v1", "content": "first proposed content"},
                    "summary": "first proposal",
                }
            ]
        }
    )
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1),),
            knowledge_key="topic-v1",
            content="first proposal",
            content_hash=hashlib.sha256(b"first proposed content").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_plan.items[0],
        ),
    )
    upper_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "update",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "target": {"knowledge_key": "topic-v2", "content": "second proposed content"},
                    "summary": "second proposal",
                }
            ]
        }
    )

    effective_plan, output_scope = organization_reduction._compose_scope_plan(upper_plan, scope=scope)

    assert effective_plan.items[0].action == "update"
    assert effective_plan.items[0].target.knowledge_key == "topic-v2"
    assert effective_plan.items[0].target.content == "second proposed content"
    assert output_scope[0].effective_item == effective_plan.items[0]


def test_schema_rejects_single_source_conflict_before_reduction_composition():
    with pytest.raises(ValueError):
        KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "conflict",
                        "sources": [{"knowledge_id": 1, "expected_version": 1}],
                        "reason": "uncertain",
                        "summary": "uncertain",
                    }
                ]
            }
        )


@pytest.mark.parametrize("upper_action", ["conflict", "merge"])
def test_reduction_cannot_change_existing_multi_source_action_without_combining_candidates(upper_action: str):
    lower_merge = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "merged", "content": "canonical merged content"},
                    "summary": "merged summary",
                }
            ]
        }
    ).items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1), (2, 1)),
            knowledge_key="merged",
            content="merged summary",
            content_hash=hashlib.sha256(b"canonical merged content").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_merge,
        ),
    )
    if upper_action == "conflict":
        raw_item = {
            "action": "conflict",
            "sources": [
                {"knowledge_id": 1, "expected_version": 1},
                {"knowledge_id": 2, "expected_version": 1},
            ],
            "reason": "changed interpretation",
            "summary": "changed",
        }
    else:
        raw_item = {
            "action": "merge",
            "sources": [
                {"knowledge_id": 1, "expected_version": 1},
                {"knowledge_id": 2, "expected_version": 1},
            ],
            "primary_knowledge_id": 1,
            "target": {"knowledge_key": "different", "content": "different merged target"},
            "summary": "changed",
        }
    upper_plan = KnowledgeOrganizationPlan.model_validate({"items": [raw_item]})

    with pytest.raises(ValueError):
        organization_reduction._compose_scope_plan(upper_plan, scope=scope)


def test_reduction_existing_conflict_keeps_effective_action_and_accepts_new_compact_summary():
    lower_conflict = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "conflict",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "reason": "lower unresolved reason",
                    "summary": "lower conflict summary",
                }
            ]
        }
    ).items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1), (2, 1)),
            knowledge_key="conflict-1-2",
            content="lower conflict summary",
            content_hash=hashlib.sha256(b"lower conflict summary").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_conflict,
        ),
    )
    upper_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "conflict",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "reason": "upper comparison still unresolved",
                    "summary": "new compact conflict summary",
                }
            ]
        }
    )

    effective_plan, output_scope = organization_reduction._compose_scope_plan(upper_plan, scope=scope)

    assert effective_plan.items[0] == lower_conflict
    assert output_scope[0].effective_item == lower_conflict
    assert output_scope[0].content == "new compact conflict summary"


def test_reduction_existing_merge_rejects_primary_change_even_when_compact_target_matches():
    lower_merge = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "merged", "content": "canonical merged content"},
                    "summary": "compact merged summary",
                }
            ]
        }
    ).items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1), (2, 1)),
            knowledge_key="merged",
            content="compact merged summary",
            content_hash=hashlib.sha256(b"canonical merged content").hexdigest(),
            source_type="organization_fragment",
            effective_item=lower_merge,
        ),
    )
    changed_primary = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 2,
                    "target": {"knowledge_key": "merged", "content": "compact merged summary"},
                    "summary": "still merged",
                }
            ]
        }
    )

    with pytest.raises(ValueError):
        organization_reduction._compose_scope_plan(changed_primary, scope=scope)


def test_reduction_can_create_conflict_by_combining_multiple_previous_candidates():
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1),),
            knowledge_key="topic-a",
            content="alpha",
            content_hash=hashlib.sha256(b"alpha").hexdigest(),
            source_type="organization_fragment",
        ),
        KnowledgeOrganizationScopeItem(
            sources=((2, 1),),
            knowledge_key="topic-b",
            content="beta",
            content_hash=hashlib.sha256(b"beta").hexdigest(),
            source_type="organization_fragment",
        ),
    )
    upper_conflict = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "conflict",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 2, "expected_version": 1},
                    ],
                    "reason": "incompatible facts",
                    "summary": "conflicting alpha and beta",
                }
            ]
        }
    )

    effective_plan, output_scope = organization_reduction._compose_scope_plan(upper_conflict, scope=scope)

    assert effective_plan.items[0].action == "conflict"
    assert output_scope[0].sources == ((1, 1), (2, 1))


def test_reduction_rejects_single_candidate_merge_without_combining_previous_candidates():
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1),),
            knowledge_key="topic-a",
            content="alpha",
            content_hash=hashlib.sha256(b"alpha").hexdigest(),
            source_type="organization_fragment",
        ),
    )
    invalid_merge = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "merge",
                    "sources": [
                        {"knowledge_id": 1, "expected_version": 1},
                        {"knowledge_id": 1, "expected_version": 1},
                    ],
                    "primary_knowledge_id": 1,
                    "target": {"knowledge_key": "topic-a", "content": "alpha"},
                    "summary": "invalid merge",
                }
            ]
        }
    )

    with pytest.raises(ValueError):
        organization_reduction._compose_scope_plan(invalid_merge, scope=scope)


def test_reduction_keep_revalidates_inherited_targets_across_fragments():
    first_update = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "update",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "target": {"knowledge_key": "shared-target", "content": "first revised content"},
                    "summary": "first update",
                }
            ]
        }
    ).items[0]
    second_update = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "update",
                    "source": {"knowledge_id": 2, "expected_version": 1},
                    "target": {"knowledge_key": "shared-target", "content": "second revised content"},
                    "summary": "second update",
                }
            ]
        }
    ).items[0]
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((1, 1),),
            knowledge_key="shared-target",
            content="first update",
            content_hash=hashlib.sha256(b"first revised content").hexdigest(),
            source_type="organization_fragment",
            effective_item=first_update,
        ),
        KnowledgeOrganizationScopeItem(
            sources=((2, 1),),
            knowledge_key="shared-target",
            content="second update",
            content_hash=hashlib.sha256(b"second revised content").hexdigest(),
            source_type="organization_fragment",
            effective_item=second_update,
        ),
    )
    upper_plan = KnowledgeOrganizationPlan.model_validate(
        {
            "items": [
                {
                    "action": "keep",
                    "source": {"knowledge_id": 1, "expected_version": 1},
                    "summary": "keep first",
                },
                {
                    "action": "keep",
                    "source": {"knowledge_id": 2, "expected_version": 1},
                    "summary": "keep second",
                },
            ]
        }
    )

    with pytest.raises(ValueError):
        organization_reduction._compose_scope_plan(upper_plan, scope=scope)


@pytest.mark.asyncio
async def test_each_reduction_layer_resolves_current_model_config(session_factory, monkeypatch: pytest.MonkeyPatch):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        for index in range(5):
            await _add_item(
                db,
                knowledge_base_id=knowledge_base.id,
                key=f"dynamic-{index}",
                content=(f"dynamic-fact-{index} " * 120).strip(),
            )

    model_a = _model(input_budget_tokens=450)
    model_b = replace(model_a, model_id="model-b")
    resolve_calls = 0

    async def resolve_current_models(_db, *, uid):
        nonlocal resolve_calls
        assert uid == "user-1"
        resolve_calls += 1
        return (model_a,) if resolve_calls == 1 else (model_b,)

    model_calls: list[str] = []

    async def model_caller(model, *, scope):
        model_calls.append(model.model_id)
        summary_prefix = "first-layer-summary" if model.model_id == "model-a" else "s"
        return KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "keep",
                        "source": {"knowledge_id": knowledge_id, "expected_version": expected_version},
                        "summary": f"{summary_prefix}-{knowledge_id}",
                    }
                    for input_item in scope
                    for knowledge_id, expected_version in input_item.sources
                ]
            }
        )

    monkeypatch.setattr(organization_run.organization_runtime, "load_knowledge_organization_model_candidates", resolve_current_models)
    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_caller=model_caller,
        analysis_caller=lambda _model_config, *, content: "compressed",
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )

    assert resolve_calls >= 2
    assert "model-a" in model_calls
    assert "model-b" in model_calls
    assert model_calls.index("model-b") > model_calls.index("model-a")
    assert result.model_id == "model-b"


@pytest.mark.asyncio
async def test_rejects_and_logs_when_current_model_config_is_unavailable(session_factory, monkeypatch: pytest.MonkeyPatch):
    async def unavailable(_db, *, uid):
        assert uid == "user-1"
        raise ValueError("organization primary model unavailable")

    warnings: list[str] = []

    class _Logger:
        def bind(self, **_kwargs):
            return self

        def warning(self, message):
            warnings.append(str(message))

    monkeypatch.setattr(organization_run.organization_runtime, "load_knowledge_organization_model_candidates", unavailable)
    monkeypatch.setattr(organization_run, "logger", _Logger())

    with pytest.raises(executor_module.KnowledgeOrganizationConfigurationError) as exc_info:
        await execute_knowledge_organization(
            session_factory,
            uid="user-1",
            knowledge_base_id=1,
        )

    assert exc_info.value.code == 400
    assert exc_info.value.data == {"status": "organization_model_config_invalid", "retryable": False}
    assert len(warnings) == 1
    assert "organization primary model unavailable" in warnings[0]


@pytest.mark.asyncio
async def test_primary_model_fails_three_times_without_using_unselected_model(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="fallback", content="fallback knowledge")

    primary = _model(input_budget_tokens=4000)
    fallback = replace(primary, model_id="model-b")
    attempts: list[str] = []

    async def model_caller(model, *, scope):
        attempts.append(model.model_id)
        if model.model_id == "model-a":
            raise RuntimeError("model unavailable")
        return _keep_plan_for_scope(scope)

    with pytest.raises(executor_module.KnowledgeOrganizationModelFailedError) as exc_info:
        await execute_knowledge_organization(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            model_candidates=(primary, fallback),
            model_caller=model_caller,
            semantic_neighbor_loader=lambda _scope, _collection: {},
        )
    assert exc_info.value.code == 502
    assert exc_info.value.data == {"status": "organization_model_execution_failed", "retryable": True}
    assert attempts == ["model-a", "model-a", "model-a"]

    async with session_factory() as db:
        stages = list((await db.execute(select(KnowledgeOrganizationStage).order_by(KnowledgeOrganizationStage.id))).scalars().all())
        assert [stage.status for stage in stages] == [KnowledgeOrganizationStageStatus.INVALIDATED]
        assert all("frozen_candidates" not in stage.model_snapshot for stage in stages)
        assert [stage.model_snapshot["execution_model"]["model_id"] for stage in stages] == ["model-a"]


@pytest.mark.asyncio
async def test_long_single_item_is_fully_analyzed_before_organization(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        long_content = " ".join(f"fact-{index}" for index in range(500))
        await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            key="long-topic",
            content=long_content,
        )

    analyzed_parts: list[str] = []

    async def analysis_caller(_model_config, *, content):
        analyzed_parts.append(content)
        return f"part-summary-{len(analyzed_parts)}"

    async def model_caller(_model_config, *, scope):
        assert len(scope) == 1
        assert scope[0].content.startswith("part-summary-")
        return _keep_plan_for_scope(scope)

    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(_model(input_budget_tokens=180),),
        model_caller=model_caller,
        analysis_caller=analysis_caller,
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    assert len(analyzed_parts) > 1
    assert "".join(analyzed_parts) == long_content
    assert len(result.plan.items) == 1
    assert result.plan.items[0].source.knowledge_id == 1
    assert result.stage_count >= 2


@pytest.mark.asyncio
async def test_long_item_split_accounts_for_serialized_analysis_payload(session_factory):
    content = '\\"' * 1000
    raw_tokens = estimate_tokens(content)
    serialized_tokens = estimate_tokens(canonical_json_dumps({"content": content}))
    assert serialized_tokens > raw_tokens + 32

    prompt_tokens = estimate_tokens(KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT)
    analysis_output_tokens = 256
    safety_margin_tokens = 64
    available_payload_tokens = raw_tokens + 32
    model = replace(
        _model(input_budget_tokens=4000),
        context_window_tokens=prompt_tokens + analysis_output_tokens + safety_margin_tokens + available_payload_tokens,
        max_output_tokens=analysis_output_tokens,
        safety_margin_tokens=safety_margin_tokens,
    )

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, _revision = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            key="escaped-json",
            content=content,
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )

    scope_item = KnowledgeOrganizationScopeItem(
        sources=((item.id, item.version),),
        knowledge_key=item.knowledge_key,
        content=content,
        content_hash=item.content_hash,
        source_type=item.source_type.value,
        source_reference=item.source_reference,
        vector_item_ids=tuple(item.vector_item_ids),
    )
    analyzed_parts: list[str] = []

    async def analysis_caller(_model_config, *, content):
        payload_tokens = estimate_tokens(canonical_json_dumps({"content": content}))
        if payload_tokens > available_payload_tokens:
            raise KnowledgeOrganizationContextExceededError("serialized analysis payload exceeds budget")
        analyzed_parts.append(content)
        return "s"

    reduced, _stage = await organization_analysis._execute_analysis_stage(
        session_factory,
        snapshot=snapshot,
        work_key=organization_run._work_key(snapshot, organization_job_id=1),
        model=model,
        scope_item=scope_item,
        content=content,
        analysis_layer=0,
        analysis_caller=analysis_caller,
    )

    assert len(analyzed_parts) > 1
    assert "".join(analyzed_parts) == content
    assert reduced


@pytest.mark.asyncio
async def test_single_candidate_budget_uses_final_array_payload_and_compacts_instead_of_failing(session_factory):
    content = "payload boundary detail " * 24
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, _revision = await _add_item(db, knowledge_base_id=knowledge_base.id, key="payload-boundary", content=content)
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
        )

    scope_item = KnowledgeOrganizationScopeItem(
        sources=((item.id, item.version),),
        knowledge_key=item.knowledge_key,
        content=content,
        content_hash=item.content_hash,
        source_type=item.source_type.value,
        source_reference=item.source_reference,
        vector_item_ids=tuple(item.vector_item_ids),
    )
    item_tokens = organization_scope._scope_item_tokens(scope_item)
    actual_payload_tokens = organization_scope._scope_tokens((scope_item,))
    assert actual_payload_tokens > item_tokens
    model = _model(input_budget_tokens=item_tokens)

    prepared, analysis_stage_count = await organization_analysis._prepare_scope_for_model(
        session_factory,
        snapshot=snapshot,
        work_key=organization_run._work_key(snapshot, organization_job_id=1),
        model=model,
        scope=(scope_item,),
        analysis_caller=lambda _model_config, *, content: "compact summary",
    )

    assert analysis_stage_count > 0
    assert organization_scope._scope_tokens(prepared) <= model.input_budget_tokens


@pytest.mark.asyncio
async def test_followup_migration_creates_snapshot_item_table(tmp_path: Path):
    database_path = tmp_path / "knowledge_organization_execution-migration.db"
    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=(
                    PromptLibrary.__table__,
                    ModelChannel.__table__,
                    Profile.__table__,
                    KnowledgeBase.__table__,
                    ManagedKnowledgeItem.__table__,
                    ManagedKnowledgeRevision.__table__,
                ),
            )
        )
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    try:
        async with factory() as db:
            await organization_stage_migration.migrate(db)
            await db.commit()
            await organization_snapshot_item_migration.migrate(db)
            await db.commit()
        async with engine.connect() as connection:
            table_names = set(await connection.run_sync(lambda sync_connection: inspect(sync_connection).get_table_names()))
    finally:
        await engine.dispose()

    assert organization_snapshot_item_migration.MIGRATION_ID == "20260911_add_knowledge_organization_snapshot_items_v1"
    assert "knowledge_organization_snapshot_item" in table_names


@pytest.mark.asyncio
async def test_context_exceeded_is_only_used_when_minimum_analysis_input_cannot_fit(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            key="unfit-topic",
            content=" ".join(f"fact-{index}" for index in range(300)),
        )

    impossible_model = replace(
        _model(input_budget_tokens=20),
        context_window_tokens=300,
        max_output_tokens=256,
        safety_margin_tokens=256,
    )
    with pytest.raises(KnowledgeOrganizationContextExceededError) as exc_info:
        await execute_knowledge_organization(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            model_candidates=(impossible_model,),
            model_caller=lambda _model_config, *, scope: _keep_plan_for_scope(scope),
            semantic_neighbor_loader=lambda _scope, _collection: {},
        )
    assert isinstance(exc_info.value, BaseBusinessException)
    assert exc_info.value.code == 400
    assert exc_info.value.data == {"status": "organization_context_exceeded", "retryable": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_content", ["   ", "x " * 20000], ids=["blank", "oversized"])
async def test_executor_uses_canonical_target_validation(session_factory, invalid_content: str):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="invalid-target", content="original content")

    attempts = 0

    async def model_caller(_model_config, *, scope):
        nonlocal attempts
        attempts += 1
        source = scope[0].sources[0]
        return KnowledgeOrganizationPlan.model_validate(
            {
                "items": [
                    {
                        "action": "update",
                        "source": {"knowledge_id": source[0], "expected_version": source[1]},
                        "target": {"knowledge_key": "updated-key", "content": invalid_content},
                        "summary": "updated summary",
                    }
                ]
            }
        )

    with pytest.raises(executor_module.KnowledgeOrganizationModelFailedError):
        await execute_knowledge_organization(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            model_candidates=(_model(input_budget_tokens=50000),),
            model_caller=model_caller,
            semantic_neighbor_loader=lambda _scope, _collection: {},
        )
    assert attempts == 3


@pytest.mark.asyncio
async def test_completed_reduction_stage_revalidates_strict_decrease(monkeypatch: pytest.MonkeyPatch):
    snapshot = KnowledgeOrganizationSnapshot(
        id=1,
        uid="user-1",
        knowledge_base_id=1,
        snapshot_key="s" * 64,
        boundary_revision_id=1,
        active_embedding_revision=1,
        index_revision=1,
        item_count=2,
        items=[],
    )
    lower_stage = KnowledgeOrganizationStage(
        id=1,
        uid="user-1",
        knowledge_base_id=1,
        snapshot_id=1,
        work_key="w" * 64,
        snapshot_key=snapshot.snapshot_key,
        stage_key="l" * 64,
        stage_index=0,
        model_key="m" * 64,
        model_snapshot={},
        expected_fragment_count=2,
        succeeded_fragment_count=2,
        status=KnowledgeOrganizationStageStatus.COMPLETED,
    )
    completed_stage = KnowledgeOrganizationStage(
        id=2,
        uid="user-1",
        knowledge_base_id=1,
        snapshot_id=1,
        work_key="w" * 64,
        snapshot_key=snapshot.snapshot_key,
        stage_key="r" * 64,
        stage_index=1,
        lower_stage_key=lower_stage.stage_key,
        model_key="m" * 64,
        model_snapshot={},
        expected_fragment_count=1,
        succeeded_fragment_count=1,
        status=KnowledgeOrganizationStageStatus.COMPLETED,
    )

    async def fake_count_groups(_groups):
        return 1

    async def fake_create_stage(*_args, **_kwargs):
        return completed_stage

    async def fake_output_tokens(*_args, **_kwargs):
        return 1000

    async def fake_compact_items(*_args, **_kwargs):
        yield (
            0,
            KnowledgeOrganizationScopeItem(
                sources=((1, 1),),
                knowledge_key="topic",
                content="tiny",
                content_hash=hashlib.sha256(b"tiny").hexdigest(),
                source_type="organization_fragment",
            ),
        )

    invalidated = False

    async def fake_invalidate(*_args, **_kwargs):
        nonlocal invalidated
        invalidated = True

    monkeypatch.setattr(organization_scope, "_count_groups", fake_count_groups)
    monkeypatch.setattr(organization_stages, "_create_stage", fake_create_stage)
    monkeypatch.setattr(organization_reduction, "_measure_stage_output_tokens", fake_output_tokens)
    monkeypatch.setattr(organization_reduction, "_iter_compact_scope_items", fake_compact_items)
    monkeypatch.setattr(organization_stages, "_fail_and_invalidate_stage", fake_invalidate)

    with pytest.raises(executor_module.KnowledgeOrganizationNotConvergedError):
        await organization_plan._execute_plan_stage_for_model(
            None,
            snapshot=snapshot,
            work_key=completed_stage.work_key,
            stage_index=1,
            lower_stage=lower_stage,
            model=_model(input_budget_tokens=4000),
            collection_name="collection",
            model_caller=lambda _model_config, *, scope: _keep_plan_for_scope(scope),
            analysis_caller=lambda _model_config, *, content: content,
            semantic_neighbor_loader=lambda _scope, _collection: {},
        )
    assert invalidated is True


@pytest.mark.asyncio
async def test_completed_analysis_stage_revalidates_strict_decrease(monkeypatch: pytest.MonkeyPatch):
    snapshot = KnowledgeOrganizationSnapshot(
        id=1,
        uid="user-1",
        knowledge_base_id=1,
        snapshot_key="s" * 64,
        boundary_revision_id=1,
        active_embedding_revision=1,
        index_revision=1,
        item_count=1,
        items=[],
    )
    completed_stage = KnowledgeOrganizationStage(
        id=3,
        uid="user-1",
        knowledge_base_id=1,
        snapshot_id=1,
        work_key="w" * 64,
        snapshot_key=snapshot.snapshot_key,
        stage_key="a" * 64,
        stage_index=0,
        model_key="m" * 64,
        model_snapshot={},
        expected_fragment_count=1,
        succeeded_fragment_count=1,
        status=KnowledgeOrganizationStageStatus.COMPLETED,
    )
    scope_item = KnowledgeOrganizationScopeItem(
        sources=((1, 1),),
        knowledge_key="topic",
        content="same content",
        content_hash=hashlib.sha256(b"same content").hexdigest(),
        source_type="llm_tool",
    )

    async def fake_create_stage(*_args, **_kwargs):
        return completed_stage

    async def fake_read_analysis(*_args, **_kwargs):
        return "same content"

    invalidated = False

    async def fake_invalidate(*_args, **_kwargs):
        nonlocal invalidated
        invalidated = True

    monkeypatch.setattr(organization_stages, "_create_stage", fake_create_stage)
    monkeypatch.setattr(organization_analysis, "_read_analysis_stage_text", fake_read_analysis)
    monkeypatch.setattr(organization_stages, "_fail_and_invalidate_stage", fake_invalidate)

    with pytest.raises(executor_module.KnowledgeOrganizationNotConvergedError):
        await organization_analysis._execute_analysis_stage(
            None,
            snapshot=snapshot,
            work_key=completed_stage.work_key,
            model=_model(input_budget_tokens=4000),
            scope_item=scope_item,
            content="same content",
            analysis_layer=0,
            analysis_caller=lambda _model_config, *, content: content,
        )
    assert invalidated is True


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_status", [KnowledgeOrganizationStageStatus.INVALIDATED, KnowledgeOrganizationStageStatus.FAILED])
async def test_new_execution_retries_failed_stage_without_changing_snapshot_or_model(session_factory, failed_status):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="retry-topic", content="retry knowledge")

    calls = 0

    async def unavailable(_model_config, *, scope):
        nonlocal calls
        calls += 1
        raise RuntimeError("temporary model failure")

    arguments = dict(
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(_model(input_budget_tokens=4000),),
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    with pytest.raises(executor_module.KnowledgeOrganizationModelFailedError):
        await execute_knowledge_organization(session_factory, **arguments, model_caller=unavailable)
    assert calls == 3
    async with session_factory() as db:
        old = (await db.execute(select(KnowledgeOrganizationStage))).scalar_one()
        old.status = failed_status
        await db.commit()
        old_id, old_snapshot_id, old_error = old.id, old.snapshot_id, old.error

    async def recovered(_model_config, *, scope):
        nonlocal calls
        calls += 1
        return _keep_plan_for_scope(scope)

    result = await execute_knowledge_organization(session_factory, **arguments, model_caller=recovered)
    assert calls == 4
    assert result.snapshot_id == old_snapshot_id
    assert len(result.plan.items) == 1
    async with session_factory() as db:
        stages = list((await db.execute(select(KnowledgeOrganizationStage).order_by(KnowledgeOrganizationStage.id))).scalars())
        assert len(stages) == 2
        assert stages[0].id == old_id
        assert stages[0].status == failed_status
        assert stages[0].error == old_error
        assert stages[1].status == KnowledgeOrganizationStageStatus.COMPLETED
        assert stages[0].work_key != stages[1].work_key
        assert stages[0].model_key == stages[1].model_key
        assert stages[0].stage_key != stages[1].stage_key
        fragments = list((await db.execute(select(KnowledgeOrganizationFragment))).scalars())
        assert [fragment.stage_id for fragment in fragments] == [stages[1].id]


@pytest.mark.asyncio
async def test_new_execution_never_resumes_running_stage_from_previous_task(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="resume-topic", content="resume knowledge")
        previous_job = await _create_running_organization_job(
            db,
            knowledge_base_id=knowledge_base.id,
            job_id_suffix="previous-task",
        )
        snapshot = await create_knowledge_organization_snapshot(
            db,
            uid="user-1",
            knowledge_base_id=knowledge_base.id,
            organization_job_id=previous_job.id,
        )

    model = _model(input_budget_tokens=4000)
    seeded = await organization_stages._create_stage(
        session_factory,
        snapshot=snapshot,
        work_key=organization_run._work_key(snapshot, organization_job_id=previous_job.id),
        stage_index=0,
        lower_stage_key=None,
        model=model,
        expected_fragment_count=1,
        purpose="initial",
    )
    assert seeded.status == KnowledgeOrganizationStageStatus.RUNNING

    async with session_factory() as db:
        assert await knowledge_job_crud.mark_failed(
            db,
            uid="user-1",
            job_id=previous_job.id,
            owner=previous_job.locked_by,
            error="simulated previous task failure",
            commit=False,
        )
        assert await coordinate_organization_terminal(
            db,
            uid="user-1",
            job_id=previous_job.id,
            error="simulated previous task failure",
            commit=False,
        )
        await db.commit()

    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(model,),
        model_caller=lambda _model_config, *, scope: _keep_plan_for_scope(scope),
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    assert len(result.plan.items) == 1

    async with session_factory() as db:
        stages = list((await db.execute(select(KnowledgeOrganizationStage).order_by(KnowledgeOrganizationStage.id))).scalars())
        assert len(stages) == 2
        assert stages[0].id == seeded.id
        assert stages[0].status == KnowledgeOrganizationStageStatus.RUNNING
        assert stages[1].status == KnowledgeOrganizationStageStatus.COMPLETED
        assert stages[1].work_key != stages[0].work_key


@pytest.mark.asyncio
async def test_new_execution_retries_failed_long_item_analysis(session_factory):
    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        await _add_item(db, knowledge_base_id=knowledge_base.id, key="long-retry", content=" ".join(f"fact-{index}" for index in range(500)))

    async def unavailable(_model_config, *, content):
        raise RuntimeError("temporary analysis failure")

    arguments = dict(
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(_model(input_budget_tokens=180),),
        model_caller=lambda _model_config, *, scope: _keep_plan_for_scope(scope),
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    with pytest.raises(executor_module.KnowledgeOrganizationModelFailedError):
        await execute_knowledge_organization(session_factory, **arguments, analysis_caller=unavailable)
    async with session_factory() as db:
        previous = list((await db.execute(select(KnowledgeOrganizationStage))).scalars())
        assert len(previous) == 2
        old_keys = {stage.stage_key for stage in previous}
        assert all(stage.status == KnowledgeOrganizationStageStatus.INVALIDATED for stage in previous)

    result = await execute_knowledge_organization(
        session_factory,
        **arguments,
        analysis_caller=lambda _model_config, *, content: "compact summary",
    )
    assert len(result.plan.items) == 1
    async with session_factory() as db:
        stages = list((await db.execute(select(KnowledgeOrganizationStage))).scalars())
        assert all(stage.status == KnowledgeOrganizationStageStatus.INVALIDATED for stage in stages if stage.stage_key in old_keys)
        assert any(stage.status == KnowledgeOrganizationStageStatus.COMPLETED and stage.model_snapshot["purpose"].startswith("analysis:") for stage in stages)
