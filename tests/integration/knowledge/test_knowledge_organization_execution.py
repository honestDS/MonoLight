from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from dataclasses import replace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.exceptions import (
    LLMException,
    LLMRequestRejectedException,
    ParameterException,
    ServerException,
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
)
from app.core.knowledge.organization_executor import (
    KnowledgeOrganizationScopeItem,
    execute_knowledge_organization,
)
from app.core.knowledge.organization_lifecycle import coordinate_organization_terminal
from app.core.knowledge.organization_runtime import (
    KnowledgeOrganizationCandidate,
    KnowledgeOrganizationModelConfig,
)
from app.core.knowledge.organization_types import KnowledgeOrganizationPlan
from app.core.utils.llm_request_policy import build_upstream_rejection, serialize_llm_json_request
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
async def session_factory(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(tables=_TABLES, foreign_keys=True, poolclass=NullPool) as factory:
        yield factory


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


@pytest.mark.asyncio
@pytest.mark.parametrize("rejection_kind", ["local", "upstream413"], ids=["local", "upstream-413"])
@pytest.mark.parametrize("stage_kind", ["plan", "analysis"], ids=["plan", "analysis"])
async def test_knowledge_organization_request_rejection_is_terminal_without_retry(
    session_factory,
    rejection_kind: str,
    stage_kind: str,
):
    private_response = "<html>private organization response</html>"
    if stage_kind == "analysis":
        content = " ".join(f"fact-{index}" for index in range(500))
        # Keep the scope budget at 180 while leaving the analysis payload room.
        model = replace(
            _model(input_budget_tokens=180),
            context_window_tokens=10000,
            system_prompt_tokens=10000 - 768 - 256 - 180,
        )
    else:
        content = "stable organization content"
        model = _model(input_budget_tokens=4000)

    async with session_factory() as db:
        knowledge_base = await _create_managed_container(db)
        item, revision = await _add_item(
            db,
            knowledge_base_id=knowledge_base.id,
            key=f"rejection-{stage_kind}",
            content=content,
        )
        assert knowledge_base.id is not None
        assert item.id is not None
        assert revision.id is not None
        knowledge_base_id = knowledge_base.id
        knowledge_id = item.id
        revision_id = revision.id
        before_item = (item.content, item.version, item.content_hash)

    calls = 0

    async def reject_request(payload):
        nonlocal calls
        calls += 1
        if rejection_kind == "local":
            serialize_llm_json_request(payload, max_size_bytes=0)
            raise AssertionError("serialize_llm_json_request should have rejected the request")

        rejection = build_upstream_rejection(status=413, detail=private_response)
        assert rejection is not None
        raise rejection

    async def rejecting_model_caller(_model_config, *, scope):
        await reject_request({"scope": [scope_item.content for scope_item in scope]})

    async def rejecting_analysis_caller(_model_config, *, content):
        await reject_request({"content": content})

    with pytest.raises(LLMRequestRejectedException) as exc_info:
        await execute_knowledge_organization(
            session_factory,
            uid="user-1",
            knowledge_base_id=knowledge_base_id,
            model_candidates=(model,),
            model_caller=rejecting_model_caller if stage_kind == "plan" else lambda _model_config, *, scope: _keep_plan_for_scope(scope),
            analysis_caller=rejecting_analysis_caller,
            semantic_neighbor_loader=lambda _scope, _collection: {},
        )

    rejection = exc_info.value
    assert type(rejection) is LLMRequestRejectedException
    assert not isinstance(rejection, executor_module.KnowledgeOrganizationModelFailedError)
    assert calls == 1
    assert rejection.source == ("local" if rejection_kind == "local" else "upstream")
    assert rejection.reason == "request_too_large"
    if rejection_kind == "upstream413":
        assert rejection.upstream_status == 413
    friendly_error = rejection.render_message()
    assert friendly_error
    assert type(rejection).__name__ not in friendly_error
    assert executor_module.KnowledgeOrganizationModelFailedError.__name__ not in friendly_error
    assert private_response not in friendly_error

    async with session_factory() as db:
        stages = list((await db.execute(select(KnowledgeOrganizationStage).order_by(KnowledgeOrganizationStage.id))).scalars().all())
        jobs = list((await db.execute(select(KnowledgeJob).order_by(KnowledgeJob.id))).scalars().all())
        current_item = await db.get(ManagedKnowledgeItem, knowledge_id)
        revisions = list((await db.execute(select(ManagedKnowledgeRevision).where(ManagedKnowledgeRevision.knowledge_id == knowledge_id).order_by(ManagedKnowledgeRevision.id))).scalars().all())
        current_knowledge_base = await db.get(KnowledgeBase, knowledge_base_id)

    assert stages
    assert all(stage.status == KnowledgeOrganizationStageStatus.INVALIDATED for stage in stages)
    assert all(stage.error == friendly_error for stage in stages)
    purposes = [stage.model_snapshot["purpose"] for stage in stages]
    if stage_kind == "analysis":
        assert any(purpose == "initial" for purpose in purposes)
        assert any(purpose.startswith("analysis:") for purpose in purposes)
    else:
        assert purposes == ["initial"]

    assert len(jobs) == 1
    job = jobs[0]
    assert job.status == KnowledgeJobStatus.FAILED
    assert job.error == friendly_error
    assert job.locked_by is None
    assert job.lock_until is None
    assert current_knowledge_base is not None
    assert current_knowledge_base.organization_error == friendly_error
    persisted_errors = [friendly_error, job.error, current_knowledge_base.organization_error, *(stage.error for stage in stages)]
    assert all(error == friendly_error for error in persisted_errors)
    assert all(type(rejection).__name__ not in error for error in persisted_errors)
    assert all(executor_module.KnowledgeOrganizationModelFailedError.__name__ not in error for error in persisted_errors)
    assert all(private_response not in error for error in persisted_errors)

    assert current_item is not None
    assert (current_item.content, current_item.version, current_item.content_hash) == before_item
    assert current_item.organization_lock_token is None
    assert [revision.id for revision in revisions] == [revision_id]
