from types import SimpleNamespace

import pytest

from app.core.utils.context_summary import service as service_module
from app.core.utils.context_summary import stage as stage_module
from app.core.utils.context_summary.model_call import CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
from app.models.message import InternalMessage, MessageRole
from tests.unit.context_summary_service_test_support import (
    _patch_summary_dependencies,
    _patch_token_counter,
    _summary_cfg,
)


@pytest.mark.asyncio
async def test_first_summary_prompt_excludes_recent_protected_rounds(monkeypatch):
    _selected_calls, _update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        if "Recent dialogue for task context only" in content:
            return 50
        if "Further compress the summary below" in content:
            return 50
        return 100

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

    assert state.content == "compressed history"
    prompt = generated_calls[0]["messages"][0].content
    assert "Recent dialogue for task context only" in prompt
    assert '"content":"recent"' not in prompt
    assert "(none)" in prompt
    assert "## Goal" in prompt


@pytest.mark.asyncio
async def test_summary_recompresses_until_configured_threshold_goal(monkeypatch):
    selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)
    summaries = iter(
        [
            "long summary " * 40,
            "medium summary " * 10,
            "short summary",
        ]
    )

    async def call_model(*, model, prompt):
        generated_calls.append(
            {
                "model": model,
                "prompt": prompt,
                "messages": [InternalMessage(role=MessageRole.USER, content=prompt)],
                "timeout": CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS,
            }
        )
        return next(summaries)

    monkeypatch.setattr(stage_module, "call_context_summary_model", call_model)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 200
        if "long summary" in content:
            return 450
        if "medium summary" in content:
            return 400
        if "short summary" in content:
            return 20
        if content.startswith("<conversation_summary>"):
            if "long summary" in content:
                return 450
            if "medium summary" in content:
                return 400
            if "short summary" in content:
                return 20
            return 100
        return 50

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

    assert state.content == "short summary"
    assert state.message_id == 4
    assert len(generated_calls) == 3
    assert len(update_calls) == 1
    assert update_calls[0]["summary"] == "short summary"
    assert "Further compress the summary below" in generated_calls[1]["messages"][0].content
    assert "Further compress the summary below" in generated_calls[2]["messages"][0].content
    assert "Conversation segment to compress" not in generated_calls[1]["messages"][0].content
    assert len(selected_calls) >= 1


@pytest.mark.asyncio
async def test_summary_refinement_stops_after_two_attempts(monkeypatch):
    _selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)
    summaries = iter(
        [
            "initial summary",
            "refined summary one",
            "refined summary two",
        ]
    )

    async def call_model(*, model, prompt):
        generated_calls.append(
            {
                "model": model,
                "prompt": prompt,
                "messages": [InternalMessage(role=MessageRole.USER, content=prompt)],
                "timeout": CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS,
            }
        )
        return next(summaries)

    monkeypatch.setattr(stage_module, "call_context_summary_model", call_model)

    token_counts = {
        "initial summary": 400,
        "refined summary one": 300,
        "refined summary two": 200,
    }

    def estimate_tokens(content):
        if content == "current":
            return 400
        if content.startswith('{"role":'):
            return 200
        for summary, token_count in token_counts.items():
            if summary in content:
                return token_count
        return 50

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

    assert state.content == "refined summary two"
    assert len(generated_calls) == 3
    assert len(update_calls) == 1
    assert update_calls[0]["summary"] == "refined summary two"
    assert all("Further compress the summary below" in generated_calls[index]["messages"][0].content for index in (1, 2))


@pytest.mark.asyncio
async def test_context_summary_uses_dedicated_timeout_not_chat_timeout(monkeypatch):
    _selected_calls, _update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)

    def estimate_tokens(content):
        if content == "current":
            return 10
        if content.startswith('{"role":'):
            return 100
        return 40

    _patch_token_counter(monkeypatch, estimate_tokens)
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
    )

    assert generated_calls
    assert generated_calls[0]["timeout"] == CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
    assert generated_calls[0]["timeout"] != 1
