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
async def test_channel_call_releases_connection_and_reports_empty_response_usage_before_fallback(monkeypatch):
    db = _TrackingSession()
    model_call_commit_counts = []
    request_metadata = []
    channel = SimpleNamespace(
        id=1,
        name="channel-1",
        base_url="https://example.invalid",
        get_decrypted_api_key=lambda: "secret",
    )

    async def select_channel(*_args, **kwargs):
        if "excluded_priorities" in kwargs:
            assert kwargs["excluded_priorities"] == {1}
            return channel, {"model_id": "model-2", "protocol": "OPENAI"}, SimpleNamespace(priority=2)
        return channel, {"model_id": "model-1", "protocol": "OPENAI"}, SimpleNamespace(priority=1)

    responses = [
        InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content=""),
            model="model-1",
            usage={"prompt_tokens": 100, "cached_tokens": 100, "completion_tokens": 0},
        ),
        InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="ok"),
            model="model-2",
            usage={"prompt_tokens": 100, "cached_tokens": 0, "completion_tokens": 10},
        ),
    ]

    async def generate(**_kwargs):
        model_call_commit_counts.append(db.commit_count)
        return responses.pop(0)

    async def request_metadata_callback(metadata):
        request_metadata.append(metadata)

    monkeypatch.setattr(channel_call, "select_channel", select_channel)
    monkeypatch.setattr(channel_call.LLMClient, "generate", generate)

    response, *_ = await channel_call.generate_chat_with_fallback(
        db,
        chat_channel=SimpleNamespace(rules=[], chat_timeout=30),
        request_builder=lambda _params: [InternalMessage(role=MessageRole.USER, content="hello")],
        call_context="test",
        cursor_key="profile:CHAT",
        uid="user-1",
        session_id="session-1",
        request_metadata_callback=request_metadata_callback,
    )

    assert model_call_commit_counts == [1, 2]
    assert response.model == "model-2"
    assert [metadata["input_tokens"] for metadata in request_metadata] == [100, 100]
    assert [metadata["cached_tokens"] for metadata in request_metadata] == [100, 0]
    provider_request_ids = [metadata["_provider_request_id"] for metadata in request_metadata]
    assert all(isinstance(request_id, str) and request_id for request_id in provider_request_ids)
    assert provider_request_ids[0] != provider_request_ids[1]
    assert [
        (
            metadata["_provider_input_tokens"],
            metadata["_provider_cached_tokens"],
            metadata["_provider_output_tokens"],
        )
        for metadata in request_metadata
    ] == [(100, 100, 0), (100, 0, 10)]
