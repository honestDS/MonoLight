from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from app.core.utils.context_summary import model_call as call_module
from app.core.utils.context_summary import selection as selection_module
from app.core.utils.context_summary.model_call import CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.models.channel import ChannelConfig
from app.models.message import InternalMessage, InternalResponse, MessageRole


class _SummaryChannel:
    id = 7
    name = "summary-channel"
    base_url = "https://example.invalid"

    def get_decrypted_api_key(self) -> str:
        return "secret"


@pytest.mark.asyncio
async def test_summary_model_selection_builds_fixed_capability_snapshot(monkeypatch):
    channel_config = ChannelConfig()
    selection_calls = []

    async def select_channel(*_args, **kwargs):
        selection_calls.append(kwargs)
        return (
            _SummaryChannel(),
            {
                "model_id": "summary-model",
                "context_window_k": 8,
                "max_tokens": 4096,
                "usage": "CHAT",
                "protocol": "OPENAI",
            },
            SimpleNamespace(priority=3),
        )

    monkeypatch.setattr(selection_module, "select_channel", select_channel)

    snapshot = await selection_module.select_context_summary_model(
        object(),
        profile_id=9,
        channel_config=channel_config,
        safety_margin_tokens=256,
        excluded_priorities={1, 2},
        call_context="context_summary_retry",
    )

    assert snapshot == ContextSummaryModelSnapshot(
        channel_id=7,
        channel_name="summary-channel",
        model_id="summary-model",
        protocol="openai",
        base_url="https://example.invalid",
        api_key="secret",
        priority=3,
        context_window_tokens=8000,
        max_output_tokens=500,
        safety_margin_tokens=256,
        input_budget_tokens=7244,
    )
    assert selection_calls == [
        {
            "call_context": "context_summary_retry",
            "excluded_priorities": {1, 2},
            "cursor_key": "9:CHAT:CONTEXT_SUMMARY",
        }
    ]
    assert snapshot.accepts_prompt_tokens(7244)
    assert not snapshot.accepts_prompt_tokens(7245)
    with pytest.raises(FrozenInstanceError):
        snapshot.model_id = "other-model"


@pytest.mark.asyncio
async def test_single_summary_call_only_uses_selected_snapshot(monkeypatch):
    generated_calls = []

    async def generate(**kwargs):
        generated_calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="  compact summary  "),
            model="summary-model",
        )

    monkeypatch.setattr(call_module.LLMClient, "generate", generate)
    snapshot = ContextSummaryModelSnapshot(
        channel_id=7,
        channel_name="summary-channel",
        model_id="summary-model",
        protocol="openai",
        base_url="https://example.invalid",
        api_key="secret",
        priority=3,
        context_window_tokens=8192,
        max_output_tokens=512,
        safety_margin_tokens=256,
        input_budget_tokens=7424,
    )

    result = await call_module.call_context_summary_model(
        model=snapshot,
        prompt="summarize this history",
    )

    assert result == "compact summary"
    assert len(generated_calls) == 1
    request = generated_calls[0]
    assert request["api_key"] == "secret"
    assert request["base_url"] == "https://example.invalid"
    assert request["model_id"] == "summary-model"
    assert len(request["messages"]) == 1
    assert request["messages"][0].role == MessageRole.USER
    assert request["messages"][0].content == "summarize this history"
    assert request["temperature"] == 0.2
    assert request["max_tokens"] == 512
    assert request["protocol"] == "openai"
    assert request["timeout"] == CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_single_summary_call_returns_none_for_empty_content(monkeypatch):
    async def generate(**_kwargs):
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=" \n "),
            model="summary-model",
        )

    monkeypatch.setattr(call_module.LLMClient, "generate", generate)
    snapshot = ContextSummaryModelSnapshot(
        channel_id=7,
        channel_name="summary-channel",
        model_id="summary-model",
        protocol="openai",
        base_url=None,
        api_key="secret",
        priority=1,
        context_window_tokens=4096,
        max_output_tokens=256,
        safety_margin_tokens=256,
        input_budget_tokens=3584,
    )

    assert await call_module.call_context_summary_model(model=snapshot, prompt="history") is None
