import pytest

from app.api.v1 import channels
from app.models.channel import ModelProtocol
from app.models.message import InternalMessage, InternalResponse, MessageRole


def test_channel_chat_request_defaults_to_non_stream() -> None:
    payload = channels.ChannelChatTestRequest()

    assert payload.test_mode is channels.ChannelChatTestMode.NON_STREAM


@pytest.mark.asyncio
async def test_channel_chat_non_stream_returns_latency(monkeypatch) -> None:
    calls: dict = {}

    async def generate(**kwargs):
        calls.update(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="  hello  "),
            model="chat-model",
            usage={"total_tokens": 4},
        )

    monkeypatch.setattr(channels.LLMClient, "generate", generate)
    clock = iter((10.0, 10.1234))
    monkeypatch.setattr(channels, "perf_counter", lambda: next(clock))

    response = await channels.test_channel_chat(
        channels.ChannelChatTestRequest(
            protocol=ModelProtocol.OPENAI,
            api_key="key",
            base_url="https://example.invalid",
            model_id=" model ",
        ),
        _admin={},
    )

    assert calls["protocol"] == "openai"
    assert calls["api_key"] == "key"
    assert calls["base_url"] == "https://example.invalid"
    assert calls["model_id"] == "model"
    assert len(calls["messages"]) == 1
    message = calls["messages"][0]
    assert message.role == MessageRole.USER
    assert message.content == "你好"
    assert message.refusal is None
    assert message.tool_calls is None
    assert calls["temperature"] == 0.7
    assert calls["max_tokens"] == 0
    assert calls["top_p"] is None
    assert calls["timeout"] == 60.0
    assert calls["custom_headers"] == {}
    assert set(response.data) == {"model", "reply", "usage", "test_mode", "latency_ms"}
    assert response.data == {
        "model": "chat-model",
        "reply": "hello",
        "usage": {"total_tokens": 4},
        "test_mode": "non_stream",
        "latency_ms": 123.4,
    }


@pytest.mark.asyncio
async def test_channel_chat_stream_returns_first_char_and_total_latency(monkeypatch) -> None:
    calls: dict = {}

    async def generate(**_kwargs):
        raise AssertionError("non-stream generation must not be called")

    async def generate_with_stream_callback(**kwargs):
        calls.update(kwargs)
        await kwargs["on_content"]("")
        await kwargs["on_content"]("first")
        await kwargs["on_content"]("second")
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="stream reply"),
            model="stream-model",
            usage={"total_tokens": 6},
        )

    monkeypatch.setattr(channels.LLMClient, "generate", generate)
    monkeypatch.setattr(channels.LLMClient, "generate_with_stream_callback", generate_with_stream_callback)
    clock = iter((20.0, 20.0256, 20.1789))
    monkeypatch.setattr(channels, "perf_counter", lambda: next(clock))

    response = await channels.test_channel_chat(
        channels.ChannelChatTestRequest(
            protocol=ModelProtocol.OPENAI_RESPONSES,
            api_key="key",
            base_url="https://example.invalid",
            model_id="model",
            test_mode=channels.ChannelChatTestMode.STREAM,
        ),
        _admin={},
    )

    assert calls["protocol"] == "openai_responses"
    assert calls["api_key"] == "key"
    assert calls["base_url"] == "https://example.invalid"
    assert calls["model_id"] == "model"
    assert len(calls["messages"]) == 1
    message = calls["messages"][0]
    assert message.role == MessageRole.USER
    assert message.content == "你好"
    assert message.refusal is None
    assert message.tool_calls is None
    assert calls["temperature"] == 0.7
    assert calls["max_tokens"] == 0
    assert calls["top_p"] is None
    assert calls["timeout"] == 60.0
    assert calls["custom_headers"] == {}
    assert set(response.data) == {
        "model",
        "reply",
        "usage",
        "test_mode",
        "first_char_latency_ms",
        "total_latency_ms",
    }
    assert response.data == {
        "model": "stream-model",
        "reply": "stream reply",
        "usage": {"total_tokens": 6},
        "test_mode": "stream",
        "first_char_latency_ms": 25.6,
        "total_latency_ms": 178.9,
    }


@pytest.mark.asyncio
async def test_channel_image_generation_returns_latency(monkeypatch) -> None:
    calls: dict = {}

    async def generate_image(**kwargs):
        calls.update(kwargs)
        return {
            "model": "image-model",
            "data": [{"url": "https://example.invalid/apple.png"}],
        }

    monkeypatch.setattr(channels.ImageGenerationClient, "generate_image", generate_image)
    clock = iter((30.0, 30.4567))
    monkeypatch.setattr(channels, "perf_counter", lambda: next(clock))

    response = await channels.test_channel_image_generation(
        channels.ChannelImageGenerationTestRequest(
            protocol=ModelProtocol.OPENAI_IMAGE,
            api_key="key",
            base_url="https://example.invalid",
            model_id="model",
        ),
        _admin={},
    )

    assert calls["protocol"] == "openai_image"
    assert calls["api_key"] == "key"
    assert calls["base_url"] == "https://example.invalid"
    assert calls["model_id"] == "model"
    assert calls["prompt"] == "A simple red apple on a white background."
    assert calls["n"] == 1
    assert calls["timeout"] == 60.0
    assert calls["custom_headers"] == {}
    assert response.data == {
        "model": "image-model",
        "image": {"url": "https://example.invalid/apple.png"},
        "latency_ms": 456.7,
    }
