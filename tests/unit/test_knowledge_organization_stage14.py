from __future__ import annotations

import asyncio
import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, select
from sqlalchemy.dialects import mysql, sqlite
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from sqlmodel import SQLModel

from app.core.constants import MANAGED_KNOWLEDGE_CONTENT_MAX_TOKENS
from app.core.knowledge import managed as managed_module
from app.core.knowledge import organization_executor as executor_module
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
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseType,
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
from scripts import migration_20260910_add_knowledge_organization_stage as organization_stage_migration
from scripts import migration_20260911_add_knowledge_organization_snapshot_items as organization_snapshot_item_migration

_TABLES = (
    PromptLibrary.__table__,
    ModelChannel.__table__,
    Profile.__table__,
    KnowledgeBase.__table__,
    ManagedKnowledgeItem.__table__,
    ManagedKnowledgeRevision.__table__,
    KnowledgeOrganizationSnapshot.__table__,
    KnowledgeOrganizationSnapshotItem.__table__,
    KnowledgeOrganizationStage.__table__,
    KnowledgeOrganizationFragment.__table__,
    LongTermMemoryStore.__table__,
)


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "knowledge-organization-stage14.db"
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


async def _create_managed_container(db: AsyncSession) -> KnowledgeBase:
    channel = ModelChannel(
        name="organization-stage14",
        api_key="test-key",
        base_url="https://example.invalid",
        model_ids=[],
    )
    db.add(channel)
    await db.flush()
    prompt = PromptLibrary(uid="user-1", name="organization-stage14", content="prompt")
    db.add(prompt)
    await db.flush()
    profile = Profile(uid="user-1", name="organization-stage14", prompt_id=prompt.id, configs={})
    db.add(profile)
    await db.flush()
    knowledge_base = KnowledgeBase(
        uid="user-1",
        name="managed-stage14",
        embedding_channel_id=channel.id,
        embedding_model_id="embedding-model",
        embedding_dimensions=1536,
        collection_name="managed-stage14-collection",
        knowledge_base_type=KnowledgeBaseType.LLM_MANAGED,
        managed_profile_id=profile.id,
        active_embedding_channel_id=channel.id,
        active_embedding_model_id="embedding-model",
        active_embedding_dimensions=1536,
        active_embedding_signature="embedding-signature",
        active_embedding_revision=3,
        active_collection_name="managed-stage14-active",
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


def test_stage14_managed_knowledge_limit_and_database_text_type(monkeypatch: pytest.MonkeyPatch):
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
async def test_stage14_snapshot_persists_items_as_rows_and_resolves_content_in_pages(session_factory, monkeypatch: pytest.MonkeyPatch):
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
async def test_stage14_snapshot_keeps_frozen_revision_after_current_item_changes(session_factory):
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


def test_stage14_semantic_groups_use_neighbors_source_and_budget():
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


def test_stage14_internal_analysis_split_covers_complete_content_without_truncation():
    content = " ".join(f"fact-{index}" for index in range(200))
    parts = split_content_for_analysis(content, max_tokens=40)
    assert len(parts) > 1
    assert "".join(parts) == content
    assert all(part for part in parts)


def test_stage14_plan_validation_enforces_scope_coverage_versions_and_target_conflicts():
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
async def test_stage14_model_candidates_are_resolved_from_current_config_without_persisting_connection_secrets(session_factory):
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
        assert [candidate.model_id for candidate in candidates] == ["primary", "fallback-1", "fallback-2"]
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
        stage_snapshot = executor_module._stage_model_snapshot(primary, purpose="initial")
        assert stage_snapshot == {
            "execution_model": {"channel_id": channel.id, "model_id": "primary", "protocol": primary.protocol},
            "purpose": "initial",
        }
        assert "secret-api-key" not in str(stage_snapshot)
        assert executor_module._model_key(transport_changed) == executor_module._model_key(primary)
        assert executor_module._model_key(runtime_changed) == executor_module._model_key(primary)
        assert executor_module._model_key(replace(primary, model_id="replacement-model")) != executor_module._model_key(primary)


@pytest.mark.asyncio
async def test_stage14_model_call_uses_full_candidate_scope_and_strict_json(monkeypatch: pytest.MonkeyPatch):
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
async def test_stage14_vector_neighbors_only_accept_snapshot_ids_and_versions(monkeypatch: pytest.MonkeyPatch):
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
        collection_name="managed-stage14-active",
    )
    assert neighbors == {1: frozenset({2}), 2: frozenset({1})}


@pytest.mark.asyncio
async def test_stage14_bounded_pipeline_persists_in_order_with_bounded_concurrency():
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
async def test_stage14_small_snapshot_calls_model_once_and_persists_one_completed_fragment(session_factory):
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
async def test_stage14_large_snapshot_uses_multiple_fragments_then_merges_to_one_final_plan(session_factory):
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
async def test_stage14_reduction_keep_preserves_lower_update_action(session_factory):
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
async def test_stage14_reduction_can_merge_multiple_lower_actions_without_new_action_types(session_factory):
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


def test_stage14_reduction_preserves_existing_merge_without_new_action_type():
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

    effective_plan, output_scope = executor_module._compose_scope_plan(upper_plan, scope=scope)

    assert len(effective_plan.items) == 1
    assert effective_plan.items[0].action == "merge"
    assert effective_plan.items[0].target.content == "canonical merged alpha beta content"
    assert output_scope[0].effective_item == effective_plan.items[0]
    assert {item.action for item in effective_plan.items} <= {"keep", "update", "merge", "conflict"}


@pytest.mark.asyncio
async def test_stage14_each_reduction_layer_resolves_current_model_config(session_factory, monkeypatch: pytest.MonkeyPatch):
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

    monkeypatch.setattr(executor_module, "load_knowledge_organization_model_candidates", resolve_current_models)
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
async def test_stage14_rejects_and_logs_when_current_model_config_is_unavailable(session_factory, monkeypatch: pytest.MonkeyPatch):
    async def unavailable(_db, *, uid):
        assert uid == "user-1"
        raise ValueError("organization primary model unavailable")

    warnings: list[str] = []

    class _Logger:
        def bind(self, **_kwargs):
            return self

        def warning(self, message):
            warnings.append(str(message))

    monkeypatch.setattr(executor_module, "load_knowledge_organization_model_candidates", unavailable)
    monkeypatch.setattr(executor_module, "logger", _Logger())

    with pytest.raises(executor_module.KnowledgeOrganizationModelFailedError):
        await execute_knowledge_organization(
            session_factory,
            uid="user-1",
            knowledge_base_id=1,
        )

    assert len(warnings) == 1
    assert "organization primary model unavailable" in warnings[0]


@pytest.mark.asyncio
async def test_stage14_primary_model_fails_three_times_then_invalidates_and_uses_fallback(session_factory):
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

    result = await execute_knowledge_organization(
        session_factory,
        uid="user-1",
        knowledge_base_id=knowledge_base.id,
        model_candidates=(primary, fallback),
        model_caller=model_caller,
        semantic_neighbor_loader=lambda _scope, _collection: {},
    )
    assert attempts == ["model-a", "model-a", "model-a", "model-b"]
    assert result.model_id == "model-b"

    async with session_factory() as db:
        stages = list((await db.execute(select(KnowledgeOrganizationStage).order_by(KnowledgeOrganizationStage.id))).scalars().all())
        assert [stage.status for stage in stages] == [
            KnowledgeOrganizationStageStatus.INVALIDATED,
            KnowledgeOrganizationStageStatus.COMPLETED,
        ]
        assert all("frozen_candidates" not in stage.model_snapshot for stage in stages)
        assert [stage.model_snapshot["execution_model"]["model_id"] for stage in stages] == ["model-a", "model-b"]


@pytest.mark.asyncio
async def test_stage14_long_single_item_is_fully_analyzed_before_organization(session_factory):
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
async def test_stage14_followup_migration_creates_snapshot_item_table(tmp_path: Path):
    database_path = tmp_path / "stage14-migration.db"
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
async def test_stage14_context_exceeded_is_only_used_when_minimum_analysis_input_cannot_fit(session_factory):
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
    assert exc_info.value.code == "organization_context_exceeded"


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_content", ["   ", "x " * 20000], ids=["blank", "oversized"])
async def test_stage14_executor_uses_canonical_target_validation(session_factory, invalid_content: str):
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
async def test_stage14_completed_reduction_stage_revalidates_strict_decrease(monkeypatch: pytest.MonkeyPatch):
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

    monkeypatch.setattr(executor_module, "_count_groups", fake_count_groups)
    monkeypatch.setattr(executor_module, "_create_stage", fake_create_stage)
    monkeypatch.setattr(executor_module, "_measure_stage_output_tokens", fake_output_tokens)
    monkeypatch.setattr(executor_module, "_iter_compact_scope_items", fake_compact_items)
    monkeypatch.setattr(executor_module, "_fail_and_invalidate_stage", fake_invalidate)

    with pytest.raises(executor_module.KnowledgeOrganizationNotConvergedError):
        await executor_module._execute_plan_stage_for_model(
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
async def test_stage14_completed_analysis_stage_revalidates_strict_decrease(monkeypatch: pytest.MonkeyPatch):
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

    monkeypatch.setattr(executor_module, "_create_stage", fake_create_stage)
    monkeypatch.setattr(executor_module, "_read_analysis_stage_text", fake_read_analysis)
    monkeypatch.setattr(executor_module, "_fail_and_invalidate_stage", fake_invalidate)

    with pytest.raises(executor_module.KnowledgeOrganizationNotConvergedError):
        await executor_module._execute_analysis_stage(
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
