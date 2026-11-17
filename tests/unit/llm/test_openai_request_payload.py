import json
from typing import Any

import pytest

from app.core.utils.llm_request_params import (
    build_memory_recall_precheck_generation_params,
    build_session_title_generation_params,
)
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer
from app.transformers.openai import base as openai_base_module
from tests.unit.llm.openai_transformer_support import (
    _FakeAiohttpResponse,
    _FakeClientSession,
)
from tests.unit.llm.openai_transformer_support import _mock_llm_request_max_bytes as _mock_llm_request_max_bytes


@pytest.mark.asyncio
async def test_responses_generate_creates_connector_with_ssl_disabled(monkeypatch) -> None:
    connector_calls: list[dict] = []
    session_calls: list[dict] = []
    connector = object()
    response = _FakeAiohttpResponse(
        text=json.dumps(
            {
                "id": "resp_1",
                "status": "completed",
                "model": "gpt-test",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Answer"}],
                    }
                ],
                "usage": {
                    "input_tokens": 2,
                    "output_tokens": 1,
                    "total_tokens": 3,
                },
            }
        )
    )

    def fake_tcp_connector(**kwargs):
        connector_calls.append(kwargs)
        return connector

    def fake_client_session(**kwargs):
        session_calls.append(kwargs)
        return _FakeClientSession(response)

    monkeypatch.setattr(openai_base_module.aiohttp, "TCPConnector", fake_tcp_connector)
    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)

    result = await OpenAIResponsesTransformer().generate(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-test",
        messages=[InternalMessage(role=MessageRole.USER, content="Question")],
    )

    assert result["id"] == "resp_1"
    assert len(connector_calls) == 1
    assert connector_calls[0]["ssl"] is False
    assert session_calls[0]["connector"] is connector


@pytest.mark.asyncio
async def test_responses_generate_passes_normalized_http_proxy_kwargs(monkeypatch) -> None:
    response = _FakeAiohttpResponse(
        text=json.dumps(
            {
                "id": "resp_1",
                "status": "completed",
                "model": "gpt-test",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Answer"}],
                    }
                ],
            }
        )
    )
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)

    await OpenAIResponsesTransformer().generate(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-test",
        messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        http_proxy="http://user%40name:password%3Awith%2Fslash@proxy.example.com:8080",
        custom_headers={
            "User-Agent": "MyClient/1.0",
            "Accept-Language": "en-US",
        },
    )

    post_kwargs = sessions[0].post_calls[0]["kwargs"]
    assert post_kwargs["proxy"] == "http://user%40name:password%3Awith%2Fslash@proxy.example.com:8080"
    assert "proxy_headers" not in post_kwargs
    assert post_kwargs["headers"]["user-agent"] == "MyClient/1.0"
    assert post_kwargs["headers"]["accept-language"] == "en-US"


@pytest.mark.asyncio
async def test_chat_completions_generate_passes_reasoning_effort(monkeypatch) -> None:
    response = _FakeAiohttpResponse(
        text=json.dumps(
            {
                "id": "chatcmpl_1",
                "model": "gpt-test",
                "choices": [{"message": {"role": "assistant", "content": "Answer"}}],
            }
        )
    )
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)

    generation_params = build_session_title_generation_params(
        model_entry={"reasoning_effort": "high"},
        protocol="openai",
    )
    await LLMClient.generate(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-test",
        messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        **generation_params,
    )

    payload = json.loads(sessions[0].post_calls[0]["kwargs"]["data"])
    assert payload["reasoning_effort"] == "high"
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert payload["max_tokens"] == generation_params["max_tokens"]
    assert payload["max_tokens"] > 0


@pytest.mark.asyncio
async def test_responses_generate_maps_reasoning_effort_to_reasoning_object(monkeypatch) -> None:
    response = _FakeAiohttpResponse(
        text=json.dumps(
            {
                "id": "resp_1",
                "status": "completed",
                "model": "gpt-test",
                "output": [{"type": "message", "content": [{"type": "output_text", "text": "Answer"}]}],
            }
        )
    )
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)

    await LLMClient.generate(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-test",
        messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        protocol="openai_responses",
        reasoning_effort="xhigh",
        max_tokens=512,
    )

    payload = json.loads(sessions[0].post_calls[0]["kwargs"]["data"])
    assert payload["reasoning"] == {"effort": "xhigh", "summary": "auto"}
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert payload["max_output_tokens"] == 512


@pytest.mark.asyncio
async def test_responses_generate_stream_creates_connector_with_ssl_disabled(monkeypatch) -> None:
    connector_calls: list[dict] = []
    session_calls: list[dict] = []
    connector = object()
    response = _FakeAiohttpResponse(chunks=[b"data: [DONE]\n"])

    def fake_tcp_connector(**kwargs):
        connector_calls.append(kwargs)
        return connector

    def fake_client_session(**kwargs):
        session_calls.append(kwargs)
        return _FakeClientSession(response)

    monkeypatch.setattr(openai_base_module.aiohttp, "TCPConnector", fake_tcp_connector)
    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)

    chunks = [
        chunk
        async for chunk in OpenAIResponsesTransformer().generate_stream(
            api_key="key",
            base_url="https://example.invalid",
            model_id="gpt-test",
            messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        )
    ]

    assert chunks == []
    assert len(connector_calls) == 1
    assert connector_calls[0]["ssl"] is False
    assert session_calls[0]["connector"] is connector


@pytest.mark.parametrize("protocol", ("openai", "openai_responses"))
@pytest.mark.asyncio
async def test_generate_omits_unspecified_sampling_and_output_token_params(monkeypatch, protocol) -> None:
    if protocol == "openai":
        response_body = {
            "id": "chatcmpl_1",
            "model": "gpt-test",
            "choices": [{"message": {"role": "assistant", "content": "Answer"}}],
        }
    else:
        response_body = {
            "id": "resp_1",
            "status": "completed",
            "model": "gpt-test",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "Answer"}]}],
        }
    response = _FakeAiohttpResponse(text=json.dumps(response_body))
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)

    await LLMClient.generate(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-test",
        messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        protocol=protocol,
    )

    payload = json.loads(sessions[0].post_calls[0]["kwargs"]["data"])
    for parameter in ("temperature", "top_p", "max_tokens", "max_output_tokens"):
        assert parameter not in payload


@pytest.mark.parametrize(
    "protocol",
    [
        pytest.param("openai", id="chat-completions"),
        pytest.param("openai_responses", id="responses"),
    ],
)
@pytest.mark.parametrize(
    "streaming",
    [
        pytest.param(False, id="non-stream"),
        pytest.param(True, id="stream"),
    ],
)
@pytest.mark.parametrize(
    "tools",
    [
        pytest.param(None, id="tools-omitted"),
        pytest.param([], id="tools-empty"),
        pytest.param(
            [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}],
            id="tools-lookup",
        ),
    ],
)
@pytest.mark.parametrize(
    "tool_choice",
    [
        pytest.param("default", id="choice-default"),
        pytest.param("auto", id="choice-auto"),
        pytest.param("none", id="choice-none"),
        pytest.param("required", id="choice-required"),
        pytest.param("function", id="choice-function"),
    ],
)
@pytest.mark.asyncio
async def test_llm_client_generate_sends_actual_tool_request_parameters(monkeypatch, protocol, streaming, tools, tool_choice) -> None:
    if protocol == "openai":
        response_body = {
            "id": "chatcmpl_regression",
            "model": "gpt-test",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": "Answer"}, "finish_reason": "stop"}],
        }
        stream_events = [
            {
                "id": "chatcmpl_regression",
                "object": "chat.completion.chunk",
                "model": "gpt-test",
                "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Answer"}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl_regression",
                "object": "chat.completion.chunk",
                "model": "gpt-test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            },
        ]
    else:
        response_body = {
            "id": "resp_regression",
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Answer"}],
                }
            ],
        }
        stream_events = [
            {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "Answer"},
            {"type": "response.completed", "response": response_body},
        ]

    if streaming:
        response = _FakeAiohttpResponse(chunks=[f"data: {json.dumps(event)}\n\n".encode() for event in stream_events] + [b"data: [DONE]\n\n"])
    else:
        response = _FakeAiohttpResponse(text=json.dumps(response_body))

    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)

    request_kwargs: dict[str, Any] = {
        "api_key": "key",
        "base_url": "https://example.invalid",
        "model_id": "gpt-test",
        "messages": [InternalMessage(role=MessageRole.USER, content="Question")],
        "temperature": 0,
        "top_p": 0,
        "max_tokens": 256,
        "reasoning_effort": "low",
        "protocol": protocol,
        "tools": tools,
    }
    expected_tool_choice: str | dict[str, Any]
    if tool_choice == "function":
        expected_tool_choice = {"type": "function", "function": {"name": "lookup"}} if protocol == "openai" else {"type": "function", "name": "lookup"}
    else:
        expected_tool_choice = tool_choice
    if tool_choice != "default":
        request_kwargs["tool_choice"] = expected_tool_choice

    if streaming:

        async def on_content(_content: str) -> None:
            return None

        result = await LLMClient.generate_with_stream_callback(on_content=on_content, **request_kwargs)
    else:
        result = await LLMClient.generate(**request_kwargs)

    assert len(sessions) == 1
    assert len(sessions[0].post_calls) == 1
    post_call = sessions[0].post_calls[0]
    expected_endpoint = "/chat/completions" if protocol == "openai" else "/responses"
    assert post_call["url"].endswith(expected_endpoint)
    payload = json.loads(post_call["kwargs"]["data"])
    assert payload["stream"] is streaming
    assert payload["temperature"] == 0
    assert payload["top_p"] == 0
    if protocol == "openai":
        assert payload["max_tokens"] == 256
        assert payload["reasoning_effort"] == "low"
        if streaming:
            assert payload["stream_options"] == {"include_usage": True}
        else:
            assert "stream_options" not in payload
    else:
        assert payload["max_output_tokens"] == 256
        assert payload["reasoning"] == {"effort": "low", "summary": "auto"}

    if tools is None or tools == []:
        assert payload["tool_choice"] == "none"
        assert "tools" not in payload
    else:
        if protocol == "openai":
            assert payload["tools"] == tools
        else:
            assert payload["tools"] == [
                {
                    "type": "function",
                    "name": "lookup",
                    "parameters": {"type": "object"},
                    "strict": False,
                }
            ]
        assert payload["tool_choice"] == ("auto" if tool_choice == "default" else expected_tool_choice)

    assert result.message.content == "Answer"
    assert result.message.tool_calls is None
    assert result.message.generated_images is None


@pytest.mark.parametrize("protocol", ("openai", "openai_responses"))
@pytest.mark.asyncio
async def test_memory_recall_precheck_stream_does_not_backfill_sampling_params(monkeypatch, protocol) -> None:
    response = _FakeAiohttpResponse(chunks=[b"data: [DONE]\n"])
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)

    generation_params = build_memory_recall_precheck_generation_params(
        model_entry={"reasoning_effort": "high"},
        protocol=protocol,
    )

    async def discard_content(_content: str) -> None:
        return None

    await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-test",
        messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        on_content=discard_content,
        protocol=protocol,
        **generation_params,
    )

    payload = json.loads(sessions[0].post_calls[0]["kwargs"]["data"])
    assert "temperature" not in payload
    assert "top_p" not in payload
    if protocol == "openai":
        assert payload["reasoning_effort"] == "high"
        assert payload["max_tokens"] == generation_params["max_tokens"] > 0
    else:
        assert payload["reasoning"] == {"effort": "high", "summary": "auto"}
        assert payload["max_output_tokens"] == generation_params["max_tokens"] > 0


@pytest.mark.asyncio
async def test_chat_completions_keeps_tools_when_tool_choice_none(monkeypatch) -> None:
    response = _FakeAiohttpResponse(
        text=json.dumps(
            {
                "id": "chatcmpl_1",
                "model": "gpt-test",
                "choices": [{"message": {"role": "assistant", "content": "Summary"}}],
            }
        )
    )
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)
    tools = [{"type": "function", "function": {"name": "lookup", "parameters": {"type": "object"}}}]

    await OpenAIChatCompletionsTransformer().generate(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-test",
        messages=[InternalMessage(role=MessageRole.USER, content="Summarize")],
        tools=tools,
        tool_choice="none",
    )

    payload = json.loads(sessions[0].post_calls[0]["kwargs"]["data"])
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "none"


def test_responses_keeps_tools_when_tool_choice_none() -> None:
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "parameters": {"type": "object"},
            },
        }
    ]
    payload = OpenAIResponsesTransformer._request_payload(
        model_id="gpt-test",
        messages=[InternalMessage(role=MessageRole.USER, content="Summarize")],
        stream=False,
        temperature=None,
        max_tokens=256,
        tools=tools,
        tool_choice="none",
        top_p=None,
    )

    assert payload["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "parameters": {"type": "object"},
            "strict": False,
        }
    ]
    assert payload["tool_choice"] == "none"


def test_responses_request_payload() -> None:
    messages = [InternalMessage(role=MessageRole.SYSTEM, content="Follow policy")]
    payload = OpenAIResponsesTransformer._request_payload(
        model_id="gpt-test",
        messages=messages,
        stream=False,
        temperature=None,
        max_tokens=256,
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "lookup",
                    "description": "Look up a value",
                    "parameters": {
                        "type": "object",
                        "properties": {"query": {"type": "string"}},
                    },
                },
            }
        ],
        tool_choice="required",
        top_p=None,
    )

    assert payload["input"] == [{"role": "system", "content": "Follow policy"}]
    assert payload["store"] is False
    assert payload["include"] == ["reasoning.encrypted_content"]
    assert payload["max_output_tokens"] == 256
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Look up a value",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
            },
            "strict": False,
        }
    ]
    assert payload["tool_choice"] == "required"
