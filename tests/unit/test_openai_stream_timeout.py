import asyncio
import json

import pytest

from app.core.constants import ERR_LLM_STREAM_TIMEOUT
from app.core.exceptions import LLMException
from app.models.message import InternalMessage, MessageRole
from app.transformers.openai import chat_completions as openai_module

STREAM_TIMEOUT = 0.08


def _sse_content(content: str) -> bytes:
    payload = {"choices": [{"delta": {"content": content}}]}
    return f"data: {json.dumps(payload)}\n".encode()


def _sse_role_only() -> bytes:
    payload = {"choices": [{"delta": {"role": "assistant"}}]}
    return f"data: {json.dumps(payload)}\n".encode()


class _FakeContent:
    def __init__(self, events: list[tuple[float, bytes]]):
        self._events = iter(events)

    def iter_any(self):
        return self

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            delay, chunk = next(self._events)
        except StopIteration:
            raise StopAsyncIteration from None
        if delay:
            await asyncio.sleep(delay)
        return chunk


class _FakeResponse:
    status = 200

    def __init__(self, events: list[tuple[float, bytes]]):
        self.content = _FakeContent(events)


class _FakeResponseContext:
    def __init__(self, response: _FakeResponse, header_delay: float = 0):
        self._response = response
        self._header_delay = header_delay
        self.exited = False

    async def __aenter__(self):
        if self._header_delay:
            await asyncio.sleep(self._header_delay)
        return self._response

    async def __aexit__(self, exc_type, exc_value, traceback):
        self.exited = True
        return False


class _FakeSession:
    def __init__(self, response_context: _FakeResponseContext):
        self._response_context = response_context

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        return False

    def post(self, *args, **kwargs):
        return self._response_context


class _FakeTCPConnector:
    def __init__(self, **kwargs):
        pass


def _patch_http(monkeypatch, response_context: _FakeResponseContext) -> None:
    def client_session(**kwargs):
        return _FakeSession(response_context)

    monkeypatch.setattr(openai_module.aiohttp, "ClientSession", client_session)
    monkeypatch.setattr(openai_module.aiohttp, "TCPConnector", _FakeTCPConnector)


def _request_messages() -> list[InternalMessage]:
    return [InternalMessage(role=MessageRole.USER, content="hello")]


async def _collect_stream(transformer: openai_module.OpenAIChatCompletionsTransformer, timeout: float = STREAM_TIMEOUT):
    chunks = []
    async for chunk in transformer.generate_stream(
        api_key="test-key",
        base_url="https://example.invalid",
        model_id="test-model",
        messages=_request_messages(),
        timeout=timeout,
    ):
        chunks.append(chunk)
    return chunks


@pytest.mark.asyncio
async def test_stream_timeout_resets_after_each_content_chunk(monkeypatch):
    first = _sse_content("first")
    second = _sse_content("second")
    response_context = _FakeResponseContext(
        _FakeResponse(
            [
                (0.02, first),
                (0.07, second),
                (0, b"data: [DONE]\n"),
            ]
        )
    )
    _patch_http(monkeypatch, response_context)

    chunks = await _collect_stream(openai_module.OpenAIChatCompletionsTransformer())

    assert chunks == [
        json.loads(first.decode().removeprefix("data: ")),
        json.loads(second.decode().removeprefix("data: ")),
    ]
    assert response_context.exited is True


@pytest.mark.asyncio
async def test_stream_timeout_after_first_content_preserves_first_chunk(monkeypatch):
    first = _sse_content("first")
    response_context = _FakeResponseContext(
        _FakeResponse(
            [
                (0.01, first),
                (0.12, b"data: [DONE]\n"),
            ]
        )
    )
    _patch_http(monkeypatch, response_context)

    chunks = []
    with pytest.raises(LLMException) as exc_info:
        async for chunk in openai_module.OpenAIChatCompletionsTransformer().generate_stream(
            api_key="test-key",
            base_url="https://example.invalid",
            model_id="test-model",
            messages=_request_messages(),
            timeout=STREAM_TIMEOUT,
        ):
            chunks.append(chunk)

    assert chunks == [json.loads(first.decode().removeprefix("data: "))]
    assert exc_info.value.message == ERR_LLM_STREAM_TIMEOUT
    assert response_context.exited is True


@pytest.mark.asyncio
async def test_stream_parses_chinese_character_split_across_raw_byte_chunks(monkeypatch):
    payload = {"choices": [{"delta": {"content": "中"}}]}
    sse_chunk = f"data: {json.dumps(payload, ensure_ascii=False)}\n".encode()
    character = "中".encode()
    split_at = sse_chunk.index(character)
    response_context = _FakeResponseContext(
        _FakeResponse(
            [
                (0, sse_chunk[: split_at + 1]),
                (0, sse_chunk[split_at + 1 : split_at + 2]),
                (0, sse_chunk[split_at + 2 :]),
                (0, b"data: [DONE]\n"),
            ]
        )
    )
    _patch_http(monkeypatch, response_context)

    chunks = await _collect_stream(openai_module.OpenAIChatCompletionsTransformer())

    assert chunks == [payload]
    assert response_context.exited is True


@pytest.mark.asyncio
async def test_stream_timeout_while_waiting_for_response_headers(monkeypatch):
    response_context = _FakeResponseContext(_FakeResponse([]), header_delay=0.12)
    _patch_http(monkeypatch, response_context)

    with pytest.raises(LLMException) as exc_info:
        await _collect_stream(openai_module.OpenAIChatCompletionsTransformer())

    assert exc_info.value.message == ERR_LLM_STREAM_TIMEOUT


@pytest.mark.asyncio
async def test_role_only_chunk_does_not_reset_stream_timeout(monkeypatch):
    response_context = _FakeResponseContext(
        _FakeResponse(
            [
                (0.01, _sse_role_only()),
                (0.08, _sse_content("first")),
            ]
        )
    )
    _patch_http(monkeypatch, response_context)

    with pytest.raises(LLMException) as exc_info:
        await _collect_stream(openai_module.OpenAIChatCompletionsTransformer())

    assert exc_info.value.message == ERR_LLM_STREAM_TIMEOUT
    assert response_context.exited is True
