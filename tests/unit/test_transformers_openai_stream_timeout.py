import asyncio

import pytest

from app.core.exceptions import LLMException
from app.transformers.openai import OpenAITransformer


@pytest.fixture(autouse=True)
def restore_wait_for(monkeypatch):
    # conftest 的全局夹具把 asyncio.wait_for 打桩为抛错，用于拦截真实 Shell；
    # 此处的流式超时测试需要真实的 asyncio.wait_for，故恢复其原始实现。
    monkeypatch.setattr(asyncio, "wait_for", asyncio.tasks.wait_for)


class _MockResponse:
    def __init__(self, status, chunks, first_delay=0.0, later_delay=0.0):
        self.status = status
        self.content = _MockContent(chunks, first_delay, later_delay)

    async def text(self):
        return ""

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        pass


class _MockContent:
    """模拟 aiohttp resp.content，可对首块与后续块设置不同延迟。"""

    def __init__(self, chunks, first_delay, later_delay):
        self._chunks = chunks
        self._first_delay = first_delay
        self._later_delay = later_delay

    def iter_any(self):
        async def gen():
            for i, chunk in enumerate(self._chunks):
                await asyncio.sleep(self._first_delay if i == 0 else self._later_delay)
                yield chunk

        return gen()


def _build_session(response):
    class _MockSession:
        def __init__(self, *args, **kwargs):
            pass

        def post(self, url, **kwargs):
            return response

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    return _MockSession


def _sse(text):
    import json

    payload = {"choices": [{"delta": {"content": text}}]}
    return f"data: {json.dumps(payload)}\n".encode()


def _keepalive():
    # 模拟服务端在真正产出内容前发送的空行/keep-alive 块
    return b": keep-alive\n\n"


def _role_only():
    # 模拟 reasoning/agent 模型在真正输出文本前先发送的 role-only 空 delta 块
    import json

    payload = {"choices": [{"delta": {"role": "assistant", "content": ""}}]}
    return f"data: {json.dumps(payload)}\n".encode()


@pytest.mark.asyncio
async def test_generate_stream_first_token_timeout_raises(monkeypatch):
    import aiohttp

    # 首块延迟 0.3s，超过 0.05s 首字超时阈值，应抛 LLMException
    response = _MockResponse(200, [_sse("hi")], first_delay=0.3)
    monkeypatch.setattr(aiohttp, "ClientSession", _build_session(response))

    transformer = OpenAITransformer()
    with pytest.raises(LLMException):
        async for _ in transformer.generate_stream(
            api_key="fake-key",
            base_url="https://api.example.com/v1",
            model_id="gpt-4o",
            messages=[],
            timeout=0.05,
        ):
            pass


@pytest.mark.asyncio
async def test_generate_stream_role_only_chunk_does_not_reset_first_token_timeout(monkeypatch):
    import aiohttp

    # reasoning/agent 模型先发 role-only 空块（content 为空），随后真正内容块延迟超过首字阈值；
    # 空块不应解除首字超时，仍应判定首字超时。
    response = _MockResponse(200, [_role_only(), _sse("late")], first_delay=0.0, later_delay=0.3)
    monkeypatch.setattr(aiohttp, "ClientSession", _build_session(response))

    transformer = OpenAITransformer()
    with pytest.raises(LLMException):
        async for _ in transformer.generate_stream(
            api_key="fake-key",
            base_url="https://api.example.com/v1",
            model_id="gpt-4o",
            messages=[],
            timeout=0.05,
        ):
            pass


@pytest.mark.asyncio
async def test_generate_stream_no_timeout_after_first_token(monkeypatch):

    import aiohttp

    # 首块及时到达，后续块延迟 0.2s 远超 0.05s 首字阈值；生成中不应再判定超时
    response = _MockResponse(200, [_sse("hello"), _sse(" world")], first_delay=0.0, later_delay=0.2)
    monkeypatch.setattr(aiohttp, "ClientSession", _build_session(response))

    transformer = OpenAITransformer()
    contents = []
    async for chunk in transformer.generate_stream(
        api_key="fake-key",
        base_url="https://api.example.com/v1",
        model_id="gpt-4o",
        messages=[],
        timeout=0.05,
    ):
        contents.append(chunk["choices"][0]["delta"]["content"])

    assert contents == ["hello", " world"]


@pytest.mark.asyncio
async def test_generate_stream_keepalive_does_not_reset_first_token_timeout(monkeypatch):
    import aiohttp

    # 首个 keep-alive 空块及时到达，但真正的内容块延迟超过首字阈值；
    # 不应因 keep-alive 提前解除超时，仍应判定首字超时。
    response = _MockResponse(200, [_keepalive(), _sse("late")], first_delay=0.0, later_delay=0.3)
    monkeypatch.setattr(aiohttp, "ClientSession", _build_session(response))

    transformer = OpenAITransformer()
    with pytest.raises(LLMException):
        async for _ in transformer.generate_stream(
            api_key="fake-key",
            base_url="https://api.example.com/v1",
            model_id="gpt-4o",
            messages=[],
            timeout=0.05,
        ):
            pass
