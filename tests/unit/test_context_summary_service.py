from types import SimpleNamespace

import pytest

from app.core.utils.context_summary import service as service_module
from app.core.utils.context_summary import stage as stage_module
from app.core.utils.context_summary.common import (
    ContextSummaryState,
    ContextSummaryWorkInvalidError,
)
from app.core.utils.context_summary.model_call import CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
from tests.unit.context_summary_service_test_support import (
    _patch_summary_dependencies,
    _patch_token_counter,
    _summary_cfg,
)


@pytest.mark.asyncio
async def test_ensure_context_summary_triggers_persists_boundary_and_uses_isolated_cursor(monkeypatch):
    selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)
    cleanup_calls = []
    bound_fields = {}
    debug_calls = []

    async def cleanup_work(work_dedupe_key):
        cleanup_calls.append(work_dedupe_key)

    class CapturingLogger:
        def bind(self, **kwargs):
            bound_fields.update(kwargs)
            return self

        def debug(self, message, **kwargs):
            debug_calls.append((message, kwargs))

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        if "compressed history" in content:
            return 40
        return 40

    monkeypatch.setattr(service_module, "logger", CapturingLogger())
    monkeypatch.setattr(
        service_module,
        "cleanup_context_summary_work_safely",
        cleanup_work,
    )
    _patch_token_counter(monkeypatch, estimate_tokens)

    state = await service_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(
        content="compressed history",
        message_id=4,
        revision=1,
    )
    assert selected_calls[0]["profile_id"] == 9
    assert update_calls == [
        {
            "session_id": "session-1",
            "uid": "user-1",
            "expected_message_id": None,
            "expected_revision": 0,
            "summary": "compressed history",
            "message_id": 4,
        }
    ]
    assert len(generated_calls) == 1
    assert generated_calls[0]["timeout"] == CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
    assert debug_calls[0][0].startswith("Context summary check:")
    assert any(call[0].startswith("Context summary generated:") for call in debug_calls)
    assert bound_fields["uid"] == "user-1"
    assert bound_fields["session_id"] == "session-1"
    assert bound_fields["summarized_through_message_id"] == 4
    assert bound_fields["summarized_message_count"] == 4
    assert bound_fields["summary_tokens"] > 0
    assert bound_fields["compression_goal_tokens"] == bound_fields["summary_trigger_tokens"]
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].startswith("context-summary:")


@pytest.mark.asyncio
async def test_context_summary_triggers_only_after_configured_threshold(monkeypatch):
    selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        return 100

    _patch_token_counter(monkeypatch, estimate_tokens)

    below_ninety_state = await service_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(90),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert below_ninety_state == ContextSummaryState(content=None, message_id=None)
    assert selected_calls == []
    assert update_calls == []
    assert generated_calls == []

    at_fifty_state = await service_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert at_fifty_state == ContextSummaryState(
        content="compressed history",
        message_id=4,
        revision=1,
    )
    assert len(selected_calls) == 1
    assert len(update_calls) == 1
    assert len(generated_calls) == 1


@pytest.mark.asyncio
async def test_context_summary_threshold_includes_tool_definition_tokens(monkeypatch):
    selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content.startswith("["):
            return 150
        if content.startswith('{"role":'):
            return 100
        return 0

    _patch_token_counter(monkeypatch, estimate_tokens)

    state = await service_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=10,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        tools=[{"type": "function", "function": {"name": "search"}}],
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(
        content="compressed history",
        message_id=4,
        revision=1,
    )
    assert len(selected_calls) == 1
    assert len(update_calls) == 1
    assert len(generated_calls) == 1


@pytest.mark.asyncio
async def test_ensure_context_summary_failure_returns_previous_state(monkeypatch):
    selected_calls, update_calls, generated_calls = _patch_summary_dependencies(
        monkeypatch,
        generation_error=RuntimeError("provider unavailable"),
    )
    cleanup_calls = []

    async def cleanup_work(work_dedupe_key):
        cleanup_calls.append(work_dedupe_key)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        return 40

    monkeypatch.setattr(
        service_module,
        "cleanup_context_summary_work_safely",
        cleanup_work,
    )
    _patch_token_counter(monkeypatch, estimate_tokens)

    state = await service_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=None,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(content=None, message_id=None)
    assert update_calls == []
    assert len(generated_calls) == stage_module.CONTEXT_SUMMARY_MODEL_ATTEMPTS
    assert len(selected_calls) == 2
    assert selected_calls[0]["excluded_priorities"] == set()
    assert selected_calls[1]["excluded_priorities"] == {1}
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].startswith("context-summary:")


@pytest.mark.asyncio
async def test_ensure_context_summary_concurrent_update_returns_winning_state(monkeypatch):
    selected_calls, update_calls, _generated_calls = _patch_summary_dependencies(
        monkeypatch,
        update_result=False,
    )
    states = iter(
        [
            ContextSummaryState(content="old summary", message_id=8, revision=3),
            ContextSummaryState(
                content="newer concurrent summary",
                message_id=12,
                revision=4,
            ),
        ]
    )

    async def get_state(_db, *, session_id, uid):
        return next(states)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        return 40

    monkeypatch.setattr(service_module, "get_context_summary_state", get_state)
    _patch_token_counter(monkeypatch, estimate_tokens)

    state = await service_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(50),
        before_id=None,
        current_message="current",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=0,
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(
        content="newer concurrent summary",
        message_id=12,
        revision=4,
    )
    assert update_calls[0]["expected_message_id"] == 8
    assert update_calls[0]["expected_revision"] == 3
    assert selected_calls[0]["profile_id"] == 9


@pytest.mark.asyncio
async def test_context_summary_work_invalid_before_persist_discards_candidate(
    monkeypatch,
):
    _selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)
    cleanup_calls = []
    validity_checks = 0

    async def cleanup_work(work_dedupe_key):
        cleanup_calls.append(work_dedupe_key)

    async def check_work_validity():
        nonlocal validity_checks
        validity_checks += 1
        return validity_checks < 3

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        if "compressed history" in content:
            return 40
        return 40

    monkeypatch.setattr(
        service_module,
        "cleanup_context_summary_work_safely",
        cleanup_work,
    )
    _patch_token_counter(monkeypatch, estimate_tokens)

    with pytest.raises(
        ContextSummaryWorkInvalidError,
        match="no longer valid",
    ):
        await service_module.ensure_context_summary(
            object(),
            session_id="session-1",
            uid="user-1",
            profile=SimpleNamespace(id=9),
            cfg=_summary_cfg(50),
            before_id=10,
            current_message="current",
            context_window_k=1,
            max_tokens=24,
            reserved_tokens=0,
            safety_margin_tokens=0,
            work_validity_checker=check_work_validity,
        )

    assert validity_checks == 3
    assert len(generated_calls) == 1
    assert update_calls == []
    assert len(cleanup_calls) == 1
    assert cleanup_calls[0].startswith("context-summary:")


@pytest.mark.asyncio
async def test_context_summary_trigger_includes_reserved_and_current_message_tokens(monkeypatch):
    selected_calls, _update_calls, _generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content == "large current input":
            return 250
        if content.startswith('{"role":'):
            return 100
        return 0

    _patch_token_counter(monkeypatch, estimate_tokens)

    state = await service_module.ensure_context_summary(
        object(),
        session_id="session-1",
        uid="user-1",
        profile=SimpleNamespace(id=9),
        cfg=_summary_cfg(90),
        before_id=10,
        current_message="large current input",
        context_window_k=1,
        max_tokens=24,
        reserved_tokens=300,
        safety_margin_tokens=0,
    )

    assert state == ContextSummaryState(
        content="compressed history",
        message_id=4,
        revision=1,
    )
    assert len(selected_calls) >= 1
