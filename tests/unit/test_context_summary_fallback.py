from types import SimpleNamespace

import pytest

from app.core.utils.context_summary import reduction as reduction_module
from app.core.utils.context_summary import stage as summary_module
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.models.message import InternalMessage, MessageRole


def _model(*, priority: int, input_budget_tokens: int) -> ContextSummaryModelSnapshot:
    return ContextSummaryModelSnapshot(
        channel_id=1,
        channel_name=f"summary-priority-{priority}",
        model_id="summary-model",
        protocol="openai",
        base_url="https://example.invalid",
        api_key="secret",
        priority=priority,
        context_window_tokens=input_budget_tokens + 256,
        max_output_tokens=256,
        safety_margin_tokens=0,
        input_budget_tokens=input_budget_tokens,
    )


@pytest.mark.asyncio
async def test_failed_layer_is_invalidated_and_resplit_for_fallback_model(monkeypatch):
    primary_model = _model(priority=1, input_budget_tokens=700)
    fallback_model = _model(priority=2, input_budget_tokens=400)
    selection_calls: list[set[int]] = []
    count_calls: list[tuple[int, int]] = []
    created_stages = []
    failed_stage_keys: list[str] = []
    invalidated_stage_keys: list[str] = []
    completed_stage_keys: list[str] = []
    persisted_by_stage: dict[str, list[int]] = {}
    model_calls: list[tuple[int, int]] = []

    async def measure_history(*_args, **_kwargs):
        return 1000, 8

    async def select_model(*_args, **kwargs):
        excluded = set(kwargs["excluded_priorities"])
        selection_calls.append(excluded)
        if not excluded:
            return primary_model
        if excluded == {1}:
            return fallback_model
        return None

    async def count_fragments(*_args, **kwargs):
        max_fragment_tokens = kwargs["max_fragment_tokens"]
        fragment_target_tokens = kwargs["fragment_target_tokens"]
        count_calls.append((max_fragment_tokens, fragment_target_tokens))
        return 2 if max_fragment_tokens > 400 else 4

    async def iter_fragments(*_args, **kwargs):
        fragment_target_tokens = kwargs["fragment_target_tokens"]
        first_fragment_index = kwargs.get("first_fragment_index", 0)
        fragment_count = 2 if fragment_target_tokens >= 500 else 4
        for fragment_index in range(first_fragment_index, fragment_count):
            yield summary_module.SummaryFragmentInput(
                fragment_index=fragment_index,
                message_start_id=fragment_index * 2 + 1,
                message_end_id=fragment_index * 2 + 2,
                token_count=100,
                content=f"fragment-{fragment_index}",
            )

    async def call_model(*, model, prompt):
        model_calls.append((model.priority, id(model)))
        if model.priority == 1:
            raise RuntimeError("primary model unavailable")
        return "short summary"

    async def create_stage(_db, *, stage):
        created_stages.append(stage)
        persisted_by_stage[stage.stage_key] = []
        return stage, True

    async def write_fragment(*, stage, result):
        persisted_by_stage[stage.stage_key].append(result.fragment_index)

    async def mark_completed(*, stage):
        completed_stage_keys.append(stage.stage_key)
        return True

    async def mark_failed(*, stage, error):
        failed_stage_keys.append(stage.stage_key)

    async def invalidate_stage(*, stage):
        invalidated_stage_keys.append(stage.stage_key)
        return True

    async def reduce_stage(_db, *, initial_stage, **_kwargs):
        assert initial_stage.stage_key == created_stages[-1].stage_key
        return reduction_module.CompletedSummaryResult(
            content="final summary",
            stage=initial_stage,
        )

    class SessionContext:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    monkeypatch.setattr(summary_module, "measure_persistent_history", measure_history)
    monkeypatch.setattr(summary_module, "select_context_summary_model", select_model)
    monkeypatch.setattr(summary_module, "count_summary_fragments", count_fragments)
    monkeypatch.setattr(summary_module, "iter_summary_fragments", iter_fragments)
    monkeypatch.setattr(summary_module, "call_context_summary_model", call_model)
    monkeypatch.setattr(summary_module.context_summary_stage_crud, "create_stage", create_stage)
    monkeypatch.setattr(summary_module, "write_summary_fragment", write_fragment)
    monkeypatch.setattr(summary_module, "mark_summary_stage_completed", mark_completed)
    monkeypatch.setattr(summary_module, "mark_summary_stage_failed", mark_failed)
    monkeypatch.setattr(summary_module, "invalidate_summary_stage", invalidate_stage)
    monkeypatch.setattr(
        reduction_module,
        "reduce_completed_summary_stage_result",
        reduce_stage,
    )
    monkeypatch.setattr(summary_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        summary_module,
        "estimate_tokens",
        lambda content: 10 if content == "short summary" else 100,
    )

    snapshot = summary_module.ContextSummarySnapshot(
        expected_summary_message_id=None,
        snapshot_before_id=10,
        snapshot_max_message_id=9,
        persistent_summary_target_id=8,
        recent_round_start_ids=(9,),
        frozen_user_message_ids=(),
        recent_messages=(
            InternalMessage(
                id=9,
                role=MessageRole.USER,
                content="recent",
            ),
        ),
    )

    result, message_count = await summary_module.generate_snapshot_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=SimpleNamespace(
            channel=SimpleNamespace(context_summary_channel=object()),
        ),
        snapshot=snapshot,
        existing_summary=None,
        existing_summary_revision=0,
        safety_margin_tokens=0,
    )

    assert result == "final summary"
    assert message_count == 8
    assert selection_calls == [set(), {1}]
    assert count_calls == [(568, 500), (268, 250)]
    assert len(created_stages) == 2

    primary_stage, fallback_stage = created_stages
    assert primary_stage.model_id == fallback_stage.model_id
    assert primary_stage.model_key != fallback_stage.model_key
    assert primary_stage.stage_key != fallback_stage.stage_key
    assert primary_stage.expected_fragment_count == 2
    assert fallback_stage.expected_fragment_count == 4

    assert failed_stage_keys == [primary_stage.stage_key]
    assert invalidated_stage_keys == [primary_stage.stage_key]
    assert completed_stage_keys == [fallback_stage.stage_key]
    assert persisted_by_stage[primary_stage.stage_key] == []
    assert persisted_by_stage[fallback_stage.stage_key] == [0, 1, 2, 3]

    primary_call_ids = {model_object_id for priority, model_object_id in model_calls if priority == primary_model.priority}
    fallback_call_ids = {model_object_id for priority, model_object_id in model_calls if priority == fallback_model.priority}
    assert primary_call_ids == {id(primary_model)}
    assert fallback_call_ids == {id(fallback_model)}
    assert len([priority for priority, _model_object_id in model_calls if priority == primary_model.priority]) >= summary_module.CONTEXT_SUMMARY_MODEL_ATTEMPTS
