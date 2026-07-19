from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.core.constants import ERR_INTERNAL_SERVER_ERROR, ERR_VALIDATION_FAILED
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.utils.dispatcher import channel_call, helpers
from app.models.message import InternalMessage, InternalResponse, MessageResponse, MessageRole, MessageType


class CapturingLogger:
    def __init__(self):
        self.bindings = []
        self.exception = None
        self.errors = []

    def bind(self, **kwargs):
        self.bindings.append(kwargs)
        return self

    def opt(self, *, exception):
        self.exception = exception
        return self

    def error(self, message):
        self.errors.append(message)


def _build_message_response(content: str) -> MessageResponse:
    return MessageResponse(
        id=1,
        profile_id=1,
        session_id="session-1",
        uid="user-1",
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content=content,
        created_at=datetime(2026, 7, 11, tzinfo=UTC),
    )


def test_format_exception_message_preserves_business_error(monkeypatch):
    log = CapturingLogger()
    monkeypatch.setattr(helpers, "logger", log)
    exc = ParameterException(message=ERR_VALIDATION_FAILED)

    message = helpers.format_exception_message(exc)

    assert message == exc.render_message()
    assert log.errors == []


def test_format_exception_message_hides_unknown_error_and_logs_details(monkeypatch):
    log = CapturingLogger()
    monkeypatch.setattr(helpers, "logger", log)
    exc = RuntimeError("sensitive implementation detail")

    message = helpers.format_exception_message(exc)

    assert message == t(ERR_INTERNAL_SERVER_ERROR)
    assert "sensitive implementation detail" not in message
    assert log.bindings == [
        {
            "exception_type": "RuntimeError",
            "exception_message": "sensitive implementation detail",
        }
    ]
    assert log.exception is exc
    assert log.errors == [t("LOG_DISPATCHER_UNKNOWN_EXCEPTION")]


def test_message_response_keeps_plain_json_object_as_text():
    response = _build_message_response('{"answer": 42}')

    assert response.content == '{"answer": 42}'


def test_message_response_restores_structured_content_list():
    response = _build_message_response('[{"type": "text", "text": "hello"}]')

    assert response.content == [{"type": "text", "text": "hello"}]


class _TrackingSession:
    def __init__(self):
        self.commit_count = 0

    async def commit(self):
        self.commit_count += 1


@pytest.mark.asyncio
async def test_channel_call_releases_connection_before_model_request(monkeypatch):
    db = _TrackingSession()
    model_call_commit_counts = []
    channel = SimpleNamespace(
        base_url="https://example.invalid",
        protocol="openai",
        get_decrypted_api_key=lambda: "secret",
    )

    async def select_channel(*_args, **_kwargs):
        return channel, {"model_id": "model-1"}, SimpleNamespace(priority=1)

    async def generate(**_kwargs):
        model_call_commit_counts.append(db.commit_count)
        return InternalResponse(message=InternalMessage(role=MessageRole.ASSISTANT, content="ok"), model="model-1")

    monkeypatch.setattr(channel_call, "select_channel", select_channel)
    monkeypatch.setattr(channel_call.LLMClient, "generate", generate)

    await channel_call.generate_chat_with_fallback(
        db,
        chat_channel=SimpleNamespace(rules=[], chat_timeout=30),
        request_builder=lambda _params: [InternalMessage(role=MessageRole.USER, content="hello")],
        call_context="test",
        cursor_key="profile:CHAT",
        uid="user-1",
        session_id="session-1",
    )

    assert model_call_commit_counts == [1]
