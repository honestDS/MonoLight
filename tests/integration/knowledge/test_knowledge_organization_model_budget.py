from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.audit.integrity import canonical_json_dumps
from app.core.constants import KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS
from app.core.exceptions import BaseBusinessException
from app.core.knowledge import (
    organization_analysis,
    organization_plan,
    organization_run,
    organization_scope,
    organization_stages,
)
from app.core.knowledge import organization_executor as executor_module
from app.core.knowledge.organization import create_knowledge_organization_snapshot
from app.core.knowledge.organization_executor import (
    KnowledgeOrganizationContextExceededError,
    KnowledgeOrganizationScopeItem,
    execute_knowledge_organization,
    run_bounded_knowledge_organization_pipeline,
)
from app.core.knowledge.organization_runtime import (
    build_semantic_fragment_groups,
    call_knowledge_organization_model,
    load_knowledge_organization_model_candidates,
    load_vector_semantic_neighbors,
    split_content_for_analysis,
    validate_knowledge_organization_plan,
)
from app.core.knowledge.organization_types import KnowledgeOrganizationPlan
from app.core.prompts import KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT, KNOWLEDGE_ORGANIZATION_SYSTEM_PROMPT
from app.core.utils.tokenizer import estimate_tokens
from app.models.channel import ModelChannel
from app.models.memory import LongTermMemoryStore
from app.models.message import InternalMessage, InternalResponse, MessageRole
from tests.integration.knowledge import test_knowledge_organization_execution as execution_module
from tests.integration.knowledge.test_knowledge_organization_execution import (
    _add_item,
    _candidate,
    _create_managed_container,
    _keep_plan_for_scope,
    _model,
)

session_factory = execution_module.session_factory


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
@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
@pytest.mark.parametrize("reasoning_effort", [None, "high", "none"])
async def test_model_call_uses_full_candidate_scope_and_strict_json(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    reasoning_effort: str | None,
):
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
    model = replace(_model(), protocol=protocol, reasoning_effort=reasoning_effort, top_p=0.65)
    plan = await call_knowledge_organization_model(model, candidates=candidates)
    assert len(plan.items) == 2
    assert captured["model_id"] == "model-a"
    assert captured["protocol"] == model.protocol
    assert captured["tools"] is None
    assert captured["max_tokens"] == 768
    assert "reasoning_effort" not in captured
    if reasoning_effort is None:
        assert captured["temperature"] == model.temperature
        assert captured["top_p"] == model.top_p
    else:
        assert "temperature" not in captured
        assert "top_p" not in captured
    request_text = captured["messages"][1].content
    assert isinstance(request_text, str)
    assert "alpha knowledge" in request_text
    assert "beta knowledge" in request_text


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
@pytest.mark.parametrize("reasoning_effort", [None, "high", "none"])
@pytest.mark.parametrize("max_output_tokens", [128, 768])
async def test_default_analysis_caller_cleans_generation_params(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    reasoning_effort: str | None,
    max_output_tokens: int,
):
    content = "complete analysis content"
    captured = {}
    model = replace(
        _model(input_budget_tokens=4000),
        protocol=protocol,
        reasoning_effort=reasoning_effort,
        top_p=0.65,
        max_output_tokens=max_output_tokens,
    )

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content='{"summary":"compact summary"}',
            ),
            model=model.model_id,
        )

    monkeypatch.setattr(organization_analysis.LLMClient, "generate", fake_generate)
    summary = await organization_analysis._default_analysis_caller(model, content=content)

    assert summary == "compact summary"
    assert captured["messages"][0].role == MessageRole.SYSTEM
    assert captured["messages"][0].content == KNOWLEDGE_ORGANIZATION_ANALYSIS_SYSTEM_PROMPT
    assert captured["messages"][1].role == MessageRole.USER
    assert captured["messages"][1].content == canonical_json_dumps({"content": content})
    assert captured["tools"] is None
    assert captured["protocol"] == model.protocol
    assert captured["max_tokens"] == min(max_output_tokens, KNOWLEDGE_ORGANIZATION_SUMMARY_MAX_TOKENS)
    assert "reasoning_effort" not in captured
    if reasoning_effort is None:
        assert captured["temperature"] == model.temperature
        assert captured["top_p"] == model.top_p
    else:
        assert "temperature" not in captured
        assert "top_p" not in captured
    assert captured["timeout"] == model.timeout
    assert captured["http_proxy"] == model.http_proxy
    assert captured["custom_headers"] == model.custom_headers


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["openai", "openai_responses"])
@pytest.mark.parametrize("reasoning_effort", [None, "high", "none"])
async def test_default_model_caller_cleans_generation_params(
    monkeypatch: pytest.MonkeyPatch,
    protocol: str,
    reasoning_effort: str | None,
):
    candidate = _candidate(1, "topic-a", "complete alpha knowledge")
    scope = (
        KnowledgeOrganizationScopeItem(
            sources=((candidate.knowledge_id, candidate.expected_version),),
            knowledge_key=candidate.knowledge_key,
            content=candidate.content,
            content_hash=candidate.content_hash,
            source_type=candidate.source_type,
        ),
    )
    captured = {}
    model = replace(
        _model(input_budget_tokens=4000),
        protocol=protocol,
        reasoning_effort=reasoning_effort,
        top_p=0.65,
    )

    async def fake_generate(**kwargs):
        captured.update(kwargs)
        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content='{"items":[{"action":"keep","source":{"knowledge_id":1,"expected_version":1},"summary":"alpha"}]}',
            ),
            model=model.model_id,
        )

    expected_scope_payload = canonical_json_dumps(
        [
            {
                "sources": [{"knowledge_id": 1, "expected_version": 1}],
                "knowledge_key": "topic-a",
                "content": "complete alpha knowledge",
                "content_hash": candidate.content_hash,
                "source_type": candidate.source_type,
                "source_reference": None,
            }
        ]
    )
    monkeypatch.setattr(organization_plan.LLMClient, "generate", fake_generate)
    plan = await organization_plan._default_model_caller(model, scope=scope)

    assert len(plan.items) == 1
    assert plan.items[0].action == "keep"
    assert plan.items[0].source.knowledge_id == candidate.knowledge_id
    assert plan.items[0].source.expected_version == candidate.expected_version
    assert captured["messages"][0].role == MessageRole.SYSTEM
    assert captured["messages"][0].content == KNOWLEDGE_ORGANIZATION_SYSTEM_PROMPT
    assert captured["messages"][1].role == MessageRole.USER
    assert captured["messages"][1].content == expected_scope_payload
    assert captured["tools"] is None
    assert captured["protocol"] == model.protocol
    assert captured["max_tokens"] == model.max_output_tokens
    assert "reasoning_effort" not in captured
    if reasoning_effort is None:
        assert captured["temperature"] == model.temperature
        assert captured["top_p"] == model.top_p
    else:
        assert "temperature" not in captured
        assert "top_p" not in captured
    assert captured["timeout"] == model.timeout
    assert captured["http_proxy"] == model.http_proxy
    assert captured["custom_headers"] == model.custom_headers


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
