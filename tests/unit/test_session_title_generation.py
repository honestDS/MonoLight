from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.core.utils.session as session_module
from app.core.utils.auxiliary_request import AuxiliaryRequestOutputBudgetError
from app.models.message import InternalMessage, InternalResponse, MessageRole


class AsyncSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


@pytest.mark.asyncio
async def test_generate_session_title_uses_configured_reasoning_effort(monkeypatch):
    db = SimpleNamespace()
    generate = AsyncMock(
        return_value=InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="\u6807\u9898"),
            model="kimi-k2.6",
        )
    )
    create_or_update_title = AsyncMock()

    monkeypatch.setattr(session_module.LLMClient, "generate", generate)
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: AsyncSessionContext(db))
    monkeypatch.setattr(session_module.session_crud, "create_or_update_title", create_or_update_title)

    title = await session_module.generate_session_title(
        uid="user-1",
        session_id="session-1",
        first_message="A first message",
        api_key="api-key",
        base_url="https://example.invalid",
        model_id="kimi-k2.6",
        protocol="openai_responses",
        model_entry={"reasoning_effort": "max", "max_tokens": 128},
    )

    assert title == "\u6807\u9898"
    kwargs = generate.await_args.kwargs
    assert kwargs["reasoning_effort"] == "max"
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert kwargs["max_tokens"] == 128
    assert len(kwargs["messages"]) == 1
    assert kwargs["messages"][0].role == MessageRole.USER
    assert kwargs["messages"][0].content == session_module.SESSION_TITLE_PROMPT.format(message="A first message")
    assert kwargs["messages"][0].environment_prompt is None
    assert kwargs["messages"][0].guidance_prompt is None
    create_or_update_title.assert_awaited_once_with(
        db=db,
        session_id="session-1",
        uid="user-1",
        title="\u6807\u9898",
    )


@pytest.mark.parametrize(
    ("finish_reason", "completion_tokens", "max_tokens", "should_reject"),
    [
        pytest.param("length", 1, 32, True, id="length-despite-content-and-low-usage"),
        pytest.param(None, 32, 32, True, id="missing-finish-at-budget"),
        pytest.param("incomplete", 32, 32, True, id="incomplete-at-budget"),
        pytest.param("stop", 32, 32, False, id="stop-at-budget"),
        pytest.param("tool_calls", 32, 32, False, id="tool-calls-at-budget"),
        pytest.param("stop", None, 32, False, id="missing-usage"),
        pytest.param("stop", 1, 32, False, id="usage-below-budget"),
        pytest.param("stop", 0, 0, False, id="zero-budget-without-known-threshold"),
    ],
)
@pytest.mark.asyncio
async def test_generate_session_title_enforces_output_budget(
    monkeypatch,
    finish_reason,
    completion_tokens,
    max_tokens,
    should_reject,
):
    db = SimpleNamespace()
    generate = AsyncMock(
        return_value=InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="\u6807\u9898"),
            model="kimi-k2.6",
            finish_reason=finish_reason,
            usage={"completion_tokens": completion_tokens} if completion_tokens is not None else {},
        )
    )
    create_or_update_title = AsyncMock()

    monkeypatch.setattr(session_module.LLMClient, "generate", generate)
    monkeypatch.setattr(session_module, "AsyncSessionLocal", lambda: AsyncSessionContext(db))
    monkeypatch.setattr(session_module.session_crud, "create_or_update_title", create_or_update_title)

    generate_title = session_module.generate_session_title
    kwargs = {
        "uid": "user-1",
        "session_id": "session-1",
        "first_message": "A first message",
        "api_key": "api-key",
        "base_url": "https://example.invalid",
        "model_id": "kimi-k2.6",
        "protocol": "openai_responses",
        "model_entry": {"max_tokens": max_tokens},
        "raise_on_error": True,
    }

    if should_reject:
        with pytest.raises(AuxiliaryRequestOutputBudgetError):
            await generate_title(**kwargs)
        create_or_update_title.assert_not_awaited()
    else:
        title = await generate_title(**kwargs)
        assert title == "\u6807\u9898"
        create_or_update_title.assert_awaited_once_with(
            db=db,
            session_id="session-1",
            uid="user-1",
            title="\u6807\u9898",
        )


@pytest.mark.asyncio
async def test_selected_profile_title_passes_model_capability_to_task_generation(monkeypatch):
    db = SimpleNamespace()
    channel = SimpleNamespace(
        base_url="https://example.invalid",
        http_proxy=None,
        get_decrypted_api_key=lambda: "api-key",
    )
    model_entry = {
        "model_id": "kimi-k2.6",
        "protocol": "OPENAI_RESPONSES",
        "temperature": 1,
        "top_p": 0.8,
        "reasoning_effort": "max",
        "max_tokens": 128,
    }
    rule = SimpleNamespace(priority=1)
    generate_title = AsyncMock(return_value="\u6807\u9898")

    monkeypatch.setattr(session_module.session_crud, "get_by_session_id", AsyncMock(return_value=None))
    monkeypatch.setattr(
        session_module,
        "resolve_profile_for_session",
        AsyncMock(
            return_value=SimpleNamespace(
                id=7,
                configs={
                    "channel": {
                        "chat_channel": {
                            "rules": [
                                {
                                    "channel_id": 1,
                                    "model_id": "kimi-k2.6",
                                    "priority": 1,
                                    "weight": 1,
                                }
                            ]
                        }
                    }
                },
            )
        ),
    )
    monkeypatch.setattr(
        session_module,
        "select_channel",
        AsyncMock(return_value=(channel, model_entry, rule)),
    )
    monkeypatch.setattr(session_module, "generate_session_title", generate_title)

    title = await session_module.generate_session_title_for_selected_profile(
        db=db,
        uid="user-1",
        session_id="session-1",
        first_message="A first message",
    )

    assert title == "\u6807\u9898"
    kwargs = generate_title.await_args.kwargs
    assert kwargs["model_entry"] == model_entry
    assert kwargs["protocol"] == "openai_responses"
    assert "temperature" not in kwargs
    assert "top_p" not in kwargs
    assert "reasoning_effort" not in kwargs
    assert "max_tokens" not in kwargs
