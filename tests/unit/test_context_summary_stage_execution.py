from types import SimpleNamespace

import pytest

from app.core.utils.context_summary import stage as stage_module
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.core.utils.context_summary.snapshot import ContextSummarySnapshot
from app.models.message import InternalMessage, MessageRole

summary_module = stage_module


def _summary_cfg(threshold_percent: int = 90) -> SimpleNamespace:
    return SimpleNamespace(
        channel=SimpleNamespace(
            chat_channel=object(),
            context_summary_channel=object(),
        ),
        other=SimpleNamespace(context_summary_threshold_percent=threshold_percent),
    )


def _summary_history() -> list[InternalMessage]:
    return [
        InternalMessage(id=1, role=MessageRole.USER, content="u1" * 100),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="a1" * 100),
        InternalMessage(id=3, role=MessageRole.USER, content="u2" * 100),
        InternalMessage(id=4, role=MessageRole.ASSISTANT, content="a2" * 100),
        InternalMessage(id=5, role=MessageRole.USER, content="recent"),
    ]


def _patch_multifragment_completion_barrier(
    monkeypatch,
    *,
    completion_succeeds: bool,
    persisted_fragment_count: int = 0,
):
    events = []

    async def measure_history(*_args, **_kwargs):
        return 1000, 4

    async def select_model(*_args, **_kwargs):
        return ContextSummaryModelSnapshot(
            channel_id=1,
            channel_name="summary-channel",
            model_id="summary-model",
            protocol="openai",
            base_url="https://example.invalid",
            api_key="secret",
            priority=1,
            context_window_tokens=2048,
            max_output_tokens=256,
            safety_margin_tokens=0,
            input_budget_tokens=1792,
        )

    async def count_fragments(*_args, **_kwargs):
        return 2

    async def iter_fragments(*_args, **_kwargs):
        first_fragment_index = _kwargs.get("first_fragment_index", 0)
        for fragment_index, start_id, end_id in (
            (0, 1, 2),
            (1, 3, 4),
        ):
            if fragment_index >= first_fragment_index:
                yield stage_module.SummaryFragmentInput(
                    fragment_index=fragment_index,
                    message_start_id=start_id,
                    message_end_id=end_id,
                    token_count=500,
                    content=f"fragment-input-{fragment_index}",
                )

    async def call_model(*, model, prompt):
        return f"summary-{prompt[-1]}"

    async def create_stage(_db, *, stage):
        stage.succeeded_fragment_count = persisted_fragment_count
        return stage, persisted_fragment_count == 0

    async def write_fragment(*, stage, result):
        events.append(("write", result.fragment_index))

    async def complete_stage(*, stage):
        events.append(("complete", stage.stage_key))
        return completion_succeeds

    async def fail_stage(*, stage, error):
        events.append(("fail", stage.stage_key, error))

    async def invalidate_stage(*, stage):
        events.append(("invalidate", stage.stage_key))
        return True

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
    monkeypatch.setattr(stage_module.context_summary_stage_crud, "create_stage", create_stage)
    monkeypatch.setattr(summary_module, "write_summary_fragment", write_fragment)
    monkeypatch.setattr(summary_module, "mark_summary_stage_completed", complete_stage)
    monkeypatch.setattr(summary_module, "mark_summary_stage_failed", fail_stage)
    monkeypatch.setattr(summary_module, "invalidate_summary_stage", invalidate_stage)
    monkeypatch.setattr(summary_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        summary_module,
        "estimate_tokens",
        lambda content: 10 if str(content).startswith("summary-") else 100,
    )
    return events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completion_succeeds", "expects_failure"),
    [(True, False), (False, True)],
)
async def test_multifragment_stage_requires_completion_barrier_after_ordered_writes(
    monkeypatch,
    completion_succeeds: bool,
    expects_failure: bool,
):
    events = _patch_multifragment_completion_barrier(
        monkeypatch,
        completion_succeeds=completion_succeeds,
    )
    snapshot = ContextSummarySnapshot(
        expected_summary_message_id=None,
        snapshot_before_id=6,
        snapshot_max_message_id=5,
        persistent_summary_target_id=4,
        recent_round_start_ids=(5,),
        frozen_user_message_ids=(),
        recent_messages=(InternalMessage(id=5, role=MessageRole.USER, content="recent"),),
    )

    result, message_count = await stage_module.generate_snapshot_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(),
        snapshot=snapshot,
        existing_summary=None,
        existing_summary_revision=0,
        safety_margin_tokens=0,
    )

    assert result is None
    assert message_count == 4
    assert [event[0] for event in events[:3]] == ["write", "write", "complete"]
    assert [event[1] for event in events[:2]] == [0, 1]
    assert any(event[0] == "fail" for event in events) is expects_failure


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("completion_succeeds", "expects_failure"),
    [(True, False), (False, True)],
)
async def test_multifragment_stage_recovery_validates_fully_persisted_layer(
    monkeypatch,
    completion_succeeds: bool,
    expects_failure: bool,
):
    events = _patch_multifragment_completion_barrier(
        monkeypatch,
        completion_succeeds=completion_succeeds,
        persisted_fragment_count=2,
    )
    snapshot = ContextSummarySnapshot(
        expected_summary_message_id=None,
        snapshot_before_id=6,
        snapshot_max_message_id=5,
        persistent_summary_target_id=4,
        recent_round_start_ids=(5,),
        frozen_user_message_ids=(),
        recent_messages=(InternalMessage(id=5, role=MessageRole.USER, content="recent"),),
    )

    result, message_count = await stage_module.generate_snapshot_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(),
        snapshot=snapshot,
        existing_summary=None,
        existing_summary_revision=0,
        safety_margin_tokens=0,
    )

    assert result is None
    assert message_count == 4
    assert events[0][0] == "complete"
    assert all(event[0] != "write" for event in events)
    assert any(event[0] == "fail" for event in events) is expects_failure
