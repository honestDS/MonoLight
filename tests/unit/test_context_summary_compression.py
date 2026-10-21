from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.constants import ERR_CONTEXT_SUMMARY_OUTPUT_BUDGET_EXHAUSTED
from app.core.exceptions import LLMException
from app.core.utils.context_summary import service as service_module
from app.core.utils.context_summary import stage as stage_module
from app.core.utils.context_summary.common import ContextSummaryOutputBudgetError
from app.core.utils.context_summary.model_call import CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.models.message import InternalMessage, InternalResponse, MessageRole
from app.providers.llm.client import LLMClient
from tests.unit.context_summary_service_test_support import (
    _patch_summary_dependencies,
    _patch_token_counter,
    _summary_cfg,
)


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
        force=True,
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
async def test_summary_refinement_stops_when_token_reduction_is_too_small(monkeypatch):
    _selected_calls, update_calls, generated_calls = _patch_summary_dependencies(monkeypatch)
    summaries = iter(
        [
            "initial summary",
            "refined summary one",
            "refined summary two",
            "must not be requested",
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
        "refined summary two": 296,
        "must not be requested": 100,
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

    with pytest.raises(LLMException):
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
            force=True,
        )

    assert len(generated_calls) == 3
    assert update_calls == []
    assert all("Further compress the summary below" in generated_calls[index]["messages"][0].content for index in (1, 2))


@pytest.mark.asyncio
@pytest.mark.parametrize("fallback_succeeds", [False, True])
async def test_generate_summary_text_falls_back_on_output_budget_exhaustion(monkeypatch, fallback_succeeds):
    primary = ContextSummaryModelSnapshot(
        channel_id=1,
        channel_name="primary",
        model_id="primary",
        protocol="openai",
        base_url=None,
        api_key="test-key",
        priority=1,
        context_window_tokens=64000,
        max_output_tokens=20480,
        temperature=None,
        top_p=None,
        safety_margin_tokens=256,
        input_budget_tokens=64000 - 20480 - 256,
    )
    fallback = replace(primary, channel_id=2, channel_name="fallback", model_id="fallback", priority=2)

    async def select_model(_db, **kwargs):
        excluded = set(kwargs["excluded_priorities"])
        if not excluded:
            return primary
        if excluded == {1}:
            return fallback
        return None

    monkeypatch.setattr(service_module, "select_context_summary_model", select_model)

    llm_calls = []

    async def generate(*, model_id, max_tokens, **kwargs):
        llm_calls.append({"model_id": model_id, "max_tokens": max_tokens})
        if model_id == "primary":
            message = InternalMessage(
                role=MessageRole.ASSISTANT,
                content=None,
                reasoning_content="primary reasoning test",
            )
            finish_reason = "length"
        else:
            message = InternalMessage(
                role=MessageRole.ASSISTANT,
                content="compressed summary" if fallback_succeeds else "partial summary",
            )
            finish_reason = "stop" if fallback_succeeds else "length"
        return InternalResponse(message=message, model=model_id, finish_reason=finish_reason)

    monkeypatch.setattr(LLMClient, "generate", generate)

    call_kwargs = {
        "db": object(),
        "profile": SimpleNamespace(id=1),
        "cfg": SimpleNamespace(channel=SimpleNamespace(context_summary_channel=object())),
        "prompt": "summary to compress",
        "safety_margin_tokens": 256,
        "uid": "user-1",
        "session_id": "session-1",
    }

    if fallback_succeeds:
        summary = await service_module.generate_summary_text(**call_kwargs)
        assert summary == "compressed summary"
    else:
        with pytest.raises(ContextSummaryOutputBudgetError) as exc_info:
            await service_module.generate_summary_text(**call_kwargs)
        assert exc_info.value.message == ERR_CONTEXT_SUMMARY_OUTPUT_BUDGET_EXHAUSTED

    assert llm_calls == [
        {"model_id": "primary", "max_tokens": 20480},
        {"model_id": "fallback", "max_tokens": 20480},
    ]
