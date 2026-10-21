from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from app.core.constants import (
    DEFAULT_CHAT_MAX_TOKENS,
    ERR_CONTEXT_SUMMARY_MODEL_RESULT_EMPTY,
    ERR_CONTEXT_SUMMARY_OUTPUT_BUDGET_EXHAUSTED,
)
from app.core.i18n import t
from app.core.utils.context_summary import model_call as call_module
from app.core.utils.context_summary import selection as selection_module
from app.core.utils.context_summary import stage as stage_module
from app.core.utils.context_summary.common import ContextSummaryOutputBudgetError
from app.core.utils.context_summary.model_call import CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
from app.core.utils.context_summary.selection import ContextSummaryModelSnapshot
from app.models.channel import ChannelConfig
from app.models.message import InternalMessage, InternalResponse, MessageRole


class _SummaryChannel:
    id = 7
    name = "summary-channel"
    base_url = "https://example.invalid"
    http_proxy = "http://proxy.example.com:8080"

    def get_decrypted_api_key(self) -> str:
        return "secret"


@pytest.fixture
def summary_model_snapshot_factory():
    def build(max_output_tokens: int = 256) -> ContextSummaryModelSnapshot:
        return ContextSummaryModelSnapshot(
            channel_id=7,
            channel_name="summary-channel",
            model_id="summary-model",
            protocol="openai",
            base_url=None,
            api_key="secret",
            priority=1,
            context_window_tokens=4096,
            max_output_tokens=max_output_tokens,
            temperature=None,
            top_p=None,
            safety_margin_tokens=256,
            input_budget_tokens=4096 - max_output_tokens - 256,
        )

    return build


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("configured_max_tokens", "expected_max_output_tokens"),
    [
        (400, 400),
        (20480, 20480),
        (0, 0),
        (None, DEFAULT_CHAT_MAX_TOKENS),
    ],
)
async def test_summary_model_selection_builds_fixed_capability_snapshot(
    monkeypatch,
    configured_max_tokens: int | None,
    expected_max_output_tokens: int,
):
    channel_config = ChannelConfig()
    selection_calls = []

    async def select_channel(*_args, **kwargs):
        selection_calls.append(kwargs)
        return (
            _SummaryChannel(),
            {
                "model_id": "summary-model",
                "context_window_k": 64,
                "max_tokens": configured_max_tokens,
                "temperature": 0.35,
                "top_p": 0.8,
                "reasoning_effort": "max",
                "usage": "CHAT",
                "protocol": "OPENAI_RESPONSES",
                "advanced_settings": {
                    "custom_headers": {"User-Agent": "SummaryClient/1.0"},
                },
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
        protocol="openai_responses",
        base_url="https://example.invalid",
        api_key="secret",
        priority=3,
        context_window_tokens=64000,
        max_output_tokens=expected_max_output_tokens,
        temperature=None,
        top_p=None,
        reasoning_effort="max",
        safety_margin_tokens=256,
        input_budget_tokens=64000 - expected_max_output_tokens - 256,
        http_proxy="http://proxy.example.com:8080",
        custom_headers={"user-agent": "SummaryClient/1.0"},
    )
    assert selection_calls == [
        {
            "call_context": "context_summary_retry",
            "excluded_priorities": {1, 2},
            "cursor_key": "9:CHAT:CONTEXT_SUMMARY",
        }
    ]
    assert snapshot.accepts_prompt_tokens(snapshot.input_budget_tokens)
    assert not snapshot.accepts_prompt_tokens(snapshot.input_budget_tokens + 1)
    with pytest.raises(FrozenInstanceError):
        snapshot.model_id = "other-model"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("max_output_tokens", "content", "finish_reason", "usage", "expected_content", "raises_budget_error"),
    [
        (256, None, "length", {}, None, True),
        (256, "partial summary", "length", {"completion_tokens": 1}, None, True),
        (256, "summary", None, {"completion_tokens": 256}, None, True),
        (256, "summary", "incomplete", {"completion_tokens": 256}, None, True),
        (256, "summary", "stop", {"completion_tokens": 256}, "summary", False),
        (256, "summary", None, {"completion_tokens": 255}, "summary", False),
        (256, "summary", None, {}, "summary", False),
        (256, "summary", None, {"completion_tokens": "256"}, "summary", False),
        (0, "summary", None, {"completion_tokens": 1024}, "summary", False),
        (0, None, "length", {"completion_tokens": 1024}, None, True),
    ],
)
async def test_summary_response_output_budget_detection(
    monkeypatch,
    summary_model_snapshot_factory,
    max_output_tokens: int,
    content: str | None,
    finish_reason: str | None,
    usage: dict,
    expected_content: str | None,
    raises_budget_error: bool,
):
    response = InternalResponse(
        message=InternalMessage(role=MessageRole.ASSISTANT, content=content),
        model="summary-model",
        usage=usage,
        finish_reason=finish_reason,
    )

    async def generate(**_kwargs):
        return response

    monkeypatch.setattr(call_module.LLMClient, "generate", generate)

    if raises_budget_error:
        with pytest.raises(ContextSummaryOutputBudgetError) as error:
            await call_module.call_context_summary_model(
                model=summary_model_snapshot_factory(max_output_tokens),
                prompt="summarize this history",
            )
        assert error.value.message == ERR_CONTEXT_SUMMARY_OUTPUT_BUDGET_EXHAUSTED
    else:
        assert (
            await call_module.call_context_summary_model(
                model=summary_model_snapshot_factory(max_output_tokens),
                prompt="summarize this history",
            )
            == expected_content
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "reasoning_content"),
    [(None, "thinking only"), ("partial summary", None)],
)
async def test_fixed_summary_model_does_not_retry_length_response(
    monkeypatch,
    summary_model_snapshot_factory,
    content: str | None,
    reasoning_content: str | None,
):
    generated_calls = []
    response = InternalResponse(
        message=InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            reasoning_content=reasoning_content,
        ),
        model="summary-model",
        usage={"completion_tokens": 1},
        finish_reason="length",
    )

    async def generate(**kwargs):
        generated_calls.append(kwargs)
        return response

    monkeypatch.setattr(call_module.LLMClient, "generate", generate)

    with pytest.raises(ContextSummaryOutputBudgetError) as error:
        await stage_module.call_fixed_summary_model(
            model=summary_model_snapshot_factory(),
            prompt="summarize this history",
            input_tokens=256,
        )

    assert error.value.message == ERR_CONTEXT_SUMMARY_OUTPUT_BUDGET_EXHAUSTED
    assert len(generated_calls) == 1


@pytest.mark.asyncio
async def test_fixed_summary_model_retries_normal_empty_stop_response(
    monkeypatch,
    summary_model_snapshot_factory,
):
    generated_calls = []
    response = InternalResponse(
        message=InternalMessage(role=MessageRole.ASSISTANT, content=" \n "),
        model="summary-model",
        usage={"completion_tokens": 0},
        finish_reason="stop",
    )

    async def generate(**kwargs):
        generated_calls.append(kwargs)
        return response

    monkeypatch.setattr(call_module.LLMClient, "generate", generate)

    with pytest.raises(RuntimeError) as error:
        await stage_module.call_fixed_summary_model(
            model=summary_model_snapshot_factory(),
            prompt="summarize this history",
            input_tokens=256,
        )

    assert t(ERR_CONTEXT_SUMMARY_MODEL_RESULT_EMPTY) in str(error.value)
    assert len(generated_calls) == 2


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
        protocol="openai_responses",
        base_url="https://example.invalid",
        api_key="secret",
        priority=3,
        context_window_tokens=8192,
        max_output_tokens=512,
        temperature=None,
        top_p=None,
        reasoning_effort=None,
        safety_margin_tokens=256,
        input_budget_tokens=7424,
        http_proxy="http://proxy.example.com:8080",
        custom_headers={"user-agent": "SummaryClient/1.0"},
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
    assert "reasoning_effort" not in request
    assert "temperature" not in request
    assert "top_p" not in request
    assert request["max_tokens"] == 512
    assert request["protocol"] == "openai_responses"
    assert request["timeout"] == CONTEXT_SUMMARY_LLM_TIMEOUT_SECONDS
    assert request["http_proxy"] == "http://proxy.example.com:8080"
    assert request["custom_headers"] == {"user-agent": "SummaryClient/1.0"}


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
        temperature=0.7,
        top_p=None,
        safety_margin_tokens=256,
        input_budget_tokens=3584,
    )

    assert await call_module.call_context_summary_model(model=snapshot, prompt="history") is None
