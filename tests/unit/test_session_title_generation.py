from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import app.core.utils.session as session_module
from app.models.message import InternalMessage, InternalResponse, MessageRole


class AsyncSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


@pytest.mark.asyncio
async def test_generate_session_title_passes_explicit_sampling_parameters(monkeypatch):
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
        protocol="openai",
        temperature=1,
        top_p=None,
    )

    assert title == "\u6807\u9898"
    kwargs = generate.await_args.kwargs
    assert kwargs["temperature"] == 1
    assert kwargs["top_p"] is None
    assert kwargs["max_tokens"] == 200
    create_or_update_title.assert_awaited_once_with(
        db=db,
        session_id="session-1",
        uid="user-1",
        title="\u6807\u9898",
    )


@pytest.mark.asyncio
async def test_selected_profile_title_uses_model_sampling_parameters(monkeypatch):
    db = SimpleNamespace()
    channel = SimpleNamespace(
        base_url="https://example.invalid",
        http_proxy=None,
        get_decrypted_api_key=lambda: "api-key",
    )
    model_entry = {
        "model_id": "kimi-k2.6",
        "protocol": "OPENAI",
        "temperature": 1,
        "top_p": 0.8,
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
                }
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
    assert kwargs["temperature"] == 1
    assert kwargs["top_p"] == 0.8
    assert kwargs["max_tokens"] == 128
