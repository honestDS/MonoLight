import asyncio
import json

import pytest

from app.core.constants import (
    ERR_CHANNEL_MODEL_LIST_FORMAT_ERROR,
    ERR_LLM_API_RESPONSE_ERROR,
    ERR_LLM_FIRST_CHAR_TIMEOUT,
    ERR_LLM_REQUEST_TOO_LARGE,
    ERR_LLM_STREAM_TIMEOUT,
    ERR_LLM_UPSTREAM_REQUEST_TOO_LARGE,
)
from app.core.exceptions import LLMException
from app.core.utils import llm_request_policy
from app.transformers import http_transport


class FakeContent:
    def __init__(self, chunks=(), *, gate=None):
        self.chunks = chunks
        self.gate = gate
        self.started = asyncio.Event()
        self.yielded_chunks = 0

    def iter_any(self):
        return self

    def __aiter__(self):
        return self._iterate()

    async def _iterate(self):
        for item in self.chunks:
            self.started.set()
            if self.gate is not None:
                await self.gate.wait()

            if isinstance(item, tuple):
                delay, chunk = item
            else:
                delay, chunk = 0, item
            if delay:
                await asyncio.sleep(delay)

            self.yielded_chunks += 1
            yield chunk


class FakeResponse:
    def __init__(self, *, status=200, body="", chunks=(), enter_delay=0, text_error=None, gate=None):
        self.status = status
        self.body = body
        self.enter_delay = enter_delay
        self.text_error = text_error
        self.content = FakeContent(chunks, gate=gate)
        self.entered = False
        self.released = False

    async def __aenter__(self):
        if self.enter_delay:
            await asyncio.sleep(self.enter_delay)
        self.entered = True
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.released = True
        return False

    async def text(self):
        if self.text_error is not None:
            raise self.text_error
        return self.body


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.requests = []
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        self.closed = True
        return False

    def get(self, url, **kwargs):
        self.requests.append({"method": "GET", "url": url, **kwargs})
        return self.response

    def post(self, url, **kwargs):
        self.requests.append({"method": "POST", "url": url, **kwargs})
        return self.response


@pytest.fixture(autouse=True)
def request_size_limit(monkeypatch):
    limit = {"bytes": 1024 * 1024}

    async def load_limit():
        return limit["bytes"]

    monkeypatch.setattr(llm_request_policy, "load_llm_request_max_bytes", load_limit)
    return limit


@pytest.fixture
def install_fake_session(monkeypatch):
    sessions = []

    def install(response):
        def create_session(*args, **kwargs):
            session = FakeSession(response)
            sessions.append(session)
            return session

        monkeypatch.setattr(http_transport.aiohttp, "ClientSession", create_session)
        return sessions

    return install


def ignore_upstream_error(text, *, status):
    return None


async def collect_stream(stream):
    return [event async for event in stream]


def stream_kwargs(*, normalize_event, timeout=5, is_terminal=None):
    return {
        "url": "https://provider.invalid/chat/completions",
        "headers": {"Authorization": "Bearer test"},
        "payload": {"model": "test", "stream": True},
        "timeout": timeout,
        "http_proxy": None,
        "normalize_event": normalize_event,
        "raise_error": ignore_upstream_error,
        "is_terminal": is_terminal,
    }


def normalize_content(event):
    choices = event.get("choices") or []
    delta = choices[0].get("delta") or {} if choices else {}
    content = delta.get("content")
    if content:
        return {"content": content}, True
    return None, False


@pytest.mark.asyncio
async def test_list_models_returns_standard_fields_and_skips_invalid_items(install_fake_session):
    response = FakeResponse(
        body=json.dumps(
            {
                "data": [
                    {"id": "model-a", "owned_by": "provider", "created": 123},
                    {"id": "model-b"},
                    {"name": "missing-id"},
                    "invalid-item",
                ]
            }
        )
    )
    sessions = install_fake_session(response)

    models = await http_transport.list_models(
        api_key="secret",
        base_url="https://provider.invalid/",
        raise_error=ignore_upstream_error,
    )

    assert models == [
        {"id": "model-a", "owned_by": "provider", "created": 123},
        {"id": "model-b", "owned_by": None, "created": None},
    ]
    assert sessions[0].requests[0]["url"] == "https://provider.invalid/models"
    assert sessions[0].requests[0]["headers"]["Authorization"] == "Bearer secret"
    assert response.released
    assert sessions[0].closed


@pytest.mark.parametrize("body", ["{", '{"data":{}}'])
@pytest.mark.asyncio
async def test_list_models_rejects_malformed_response(install_fake_session, body):
    response = FakeResponse(body=body)
    install_fake_session(response)

    with pytest.raises(LLMException) as exc_info:
        await http_transport.list_models(
            api_key="secret",
            base_url="https://provider.invalid",
            raise_error=ignore_upstream_error,
        )

    assert exc_info.value.message == ERR_CHANNEL_MODEL_LIST_FORMAT_ERROR
    assert response.released


@pytest.mark.asyncio
async def test_post_json_sends_real_compact_utf8_serialization(install_fake_session):
    response = FakeResponse(body='{"id":"response-1"}')
    sessions = install_fake_session(response)
    headers = {"Content-Type": "application/json"}

    result = await http_transport.post_json(
        url="https://provider.invalid/generate",
        headers=headers,
        payload={"prompt": "你好", "count": 2},
        timeout=3,
        http_proxy=None,
        raise_error=ignore_upstream_error,
    )

    assert result == {"id": "response-1"}
    request = sessions[0].requests[0]
    assert request["method"] == "POST"
    assert request["url"] == "https://provider.invalid/generate"
    assert request["data"] == '{"prompt":"你好","count":2}'.encode()
    assert response.released
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_post_json_rejects_oversized_serialized_payload_before_session_creation(install_fake_session, request_size_limit):
    request_size_limit["bytes"] = 1
    sessions = install_fake_session(FakeResponse(body="{}"))

    with pytest.raises(LLMException) as exc_info:
        await http_transport.post_json(
            url="https://provider.invalid/generate",
            headers={},
            payload={"prompt": "payload"},
            timeout=3,
            http_proxy=None,
            raise_error=ignore_upstream_error,
        )

    assert exc_info.value.message == ERR_LLM_REQUEST_TOO_LARGE
    assert sessions == []


@pytest.mark.asyncio
async def test_post_json_allows_413_callback_to_raise_upstream_error(install_fake_session):
    response = FakeResponse(status=413, body="request too large")
    install_fake_session(response)

    def raise_upstream_error(text, *, status):
        rejection = llm_request_policy.build_upstream_rejection(status=status, detail=text)
        if rejection is not None:
            raise rejection

    with pytest.raises(LLMException) as exc_info:
        await http_transport.post_json(
            url="https://provider.invalid/generate",
            headers={},
            payload={"prompt": "payload"},
            timeout=3,
            http_proxy=None,
            raise_error=raise_upstream_error,
        )

    assert exc_info.value.message == ERR_LLM_UPSTREAM_REQUEST_TOO_LARGE
    assert exc_info.value.upstream_status == 413
    assert response.released


@pytest.mark.asyncio
async def test_post_json_preserves_other_http_status(install_fake_session):
    response = FakeResponse(status=429, body="rate limited")
    install_fake_session(response)

    with pytest.raises(LLMException) as exc_info:
        await http_transport.post_json(
            url="https://provider.invalid/generate",
            headers={},
            payload={"prompt": "payload"},
            timeout=3,
            http_proxy=None,
            raise_error=ignore_upstream_error,
        )

    assert exc_info.value.message == ERR_LLM_API_RESPONSE_ERROR
    assert exc_info.value.kwargs["status"] == 429


@pytest.mark.asyncio
async def test_post_json_maps_timeout_to_first_character_timeout(install_fake_session):
    response = FakeResponse(text_error=TimeoutError())
    install_fake_session(response)

    with pytest.raises(LLMException) as exc_info:
        await http_transport.post_json(
            url="https://provider.invalid/generate",
            headers={},
            payload={"prompt": "payload"},
            timeout=3,
            http_proxy=None,
            raise_error=ignore_upstream_error,
        )

    assert exc_info.value.message == ERR_LLM_FIRST_CHAR_TIMEOUT


@pytest.mark.asyncio
async def test_stream_parses_crlf_multiline_utf8_and_final_line_without_newline(install_fake_session):
    body = 'data: {"choices":[\r\ndata: {"delta":{"content":"你好"},"finish_reason":null}]}'.encode()
    chinese_byte = body.index("你".encode()) + 1
    chunks = [body[:chinese_byte], body[chinese_byte : chinese_byte + 1], body[chinese_byte + 1 :]]
    response = FakeResponse(chunks=chunks)
    sessions = install_fake_session(response)

    events = await collect_stream(http_transport.stream_sse_json(**stream_kwargs(normalize_event=normalize_content)))

    assert events == [{"content": "你好"}]
    assert response.content.yielded_chunks == 3
    assert response.released
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_stream_done_marker_ends_normally_without_reading_following_chunk(install_fake_session):
    response = FakeResponse(chunks=[b"data: [DONE]\n\n", b'data: {"choices":[]}\n\n'])
    sessions = install_fake_session(response)

    events = await collect_stream(http_transport.stream_sse_json(**stream_kwargs(normalize_event=normalize_content)))

    assert events == []
    assert response.content.yielded_chunks == 1
    assert response.released
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_stream_yields_normalized_terminal_usage_and_metadata_without_reading_after_it(
    install_fake_session,
):
    terminal_event = {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 4, "completion_tokens": 2},
        "metadata": {"request_id": "provider-request"},
    }
    terminal_chunk = f"data: {json.dumps(terminal_event)}\n\n".encode()
    response = FakeResponse(chunks=[terminal_chunk, b'data: {"choices":[]}\n\n'])
    sessions = install_fake_session(response)

    def normalize_terminal(event):
        return {"usage": event.get("usage"), "metadata": event.get("metadata")}, False

    events = await collect_stream(
        http_transport.stream_sse_json(
            **stream_kwargs(
                normalize_event=normalize_terminal,
                is_terminal=lambda event: event["choices"][0]["finish_reason"] == "stop",
            )
        )
    )

    assert events == [{"usage": terminal_event["usage"], "metadata": terminal_event["metadata"]}]
    assert response.content.yielded_chunks == 1
    assert response.released
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_stream_reports_malformed_json_and_releases_context(install_fake_session):
    response = FakeResponse(chunks=[b"data: {not-json}\n\n"])
    sessions = install_fake_session(response)

    with pytest.raises(LLMException) as exc_info:
        await collect_stream(http_transport.stream_sse_json(**stream_kwargs(normalize_event=normalize_content)))

    assert exc_info.value.message == ERR_LLM_API_RESPONSE_ERROR
    assert response.released
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_stream_times_out_while_waiting_for_response_headers(install_fake_session):
    response = FakeResponse(enter_delay=0.8)
    sessions = install_fake_session(response)

    with pytest.raises(LLMException) as exc_info:
        await collect_stream(http_transport.stream_sse_json(**stream_kwargs(normalize_event=normalize_content, timeout=0.5)))

    assert exc_info.value.message == ERR_LLM_STREAM_TIMEOUT
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_stream_times_out_before_first_substantive_output(install_fake_session):
    response = FakeResponse(chunks=[(0.9, b'data: {"choices":[{"delta":{"content":"late"}}]}\n\n')])
    sessions = install_fake_session(response)

    with pytest.raises(LLMException) as exc_info:
        await collect_stream(http_transport.stream_sse_json(**stream_kwargs(normalize_event=normalize_content, timeout=0.6)))

    assert exc_info.value.message == ERR_LLM_STREAM_TIMEOUT
    assert response.released
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_stream_valid_output_refreshes_deadline(install_fake_session):
    chunks = [
        (0.5, b'data: {"choices":[{"delta":{"content":"first"}}]}\n\n'),
        (0.5, b'data: {"choices":[{"delta":{"content":"second"}}]}\n\n'),
    ]
    response = FakeResponse(chunks=chunks)
    sessions = install_fake_session(response)

    events = await collect_stream(http_transport.stream_sse_json(**stream_kwargs(normalize_event=normalize_content, timeout=0.8)))

    assert events == [{"content": "first"}, {"content": "second"}]
    assert response.content.yielded_chunks == 2
    assert response.released
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_stream_non_output_events_and_comment_heartbeat_do_not_refresh_deadline(install_fake_session):
    role = b'data: {"choices":[{"delta":{"role":"assistant"}}]}\n\n'
    usage = b'data: {"usage":{"total_tokens":1}}\n\n'
    empty_delta = b'data: {"choices":[{"delta":{}}]}\n\n'
    response = FakeResponse(
        chunks=[
            (0.25, b": keep-alive\n\n"),
            (0.25, role),
            (0.25, usage),
            (0.25, empty_delta),
            (0.7, b'data: {"choices":[{"delta":{"content":"late"}}]}\n\n'),
        ]
    )
    sessions = install_fake_session(response)

    with pytest.raises(LLMException) as exc_info:
        await collect_stream(http_transport.stream_sse_json(**stream_kwargs(normalize_event=normalize_content, timeout=1.35)))

    assert exc_info.value.message == ERR_LLM_STREAM_TIMEOUT
    assert response.content.yielded_chunks == 4
    assert response.released
    assert sessions[0].closed


@pytest.mark.asyncio
async def test_cancelling_active_stream_releases_response_and_session(install_fake_session):
    response = FakeResponse(chunks=[b"data: [DONE]\n\n"], gate=asyncio.Event())
    sessions = install_fake_session(response)
    task = asyncio.create_task(collect_stream(http_transport.stream_sse_json(**stream_kwargs(normalize_event=normalize_content))))

    await asyncio.wait_for(response.content.started.wait(), timeout=2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert response.released
    assert sessions[0].closed
