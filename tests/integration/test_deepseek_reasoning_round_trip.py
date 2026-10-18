import json
from collections.abc import Callable
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import ContextManager
from app.core.utils import llm_request_policy
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.models.message import (
    InternalMessage,
    Message,
    MessageResponse,
    MessageRole,
    MessageType,
)
from app.models.session import ChatSession
from app.providers.llm.client import LLMClient
from app.transformers import http_transport

_UID = "deepseek-round-trip-user"
_SESSION_ID = "deepseek-round-trip-session"
_PROFILE_ID = 41
_CHANNEL_ID = 73
_MODEL_ALIAS = "deepseek-test-alias"
_PROVIDER_MODEL = "deepseek-real-model"
_BASE_URL = "https://deepseek.invalid/v1"
_TOOL_ID = "provider-call-weather-1"
_TOOL_NAME = "lookup_weather"
_TOOL_ARGUMENTS = {"城市": "杭州", "温度单位": "摄氏度"}
_PRIVATE_RESPONSE = "internal-response-marker"
_PRIVATE_MESSAGE = "internal-message-marker"
_PRIVATE_REASONING = "internal-reasoning-marker"
_PRIVATE_TOOL = "internal-tool-marker"


def _usage(protocol: str, request_index: int) -> dict[str, Any]:
    prompt_tokens = 31 + request_index
    completion_tokens = 14 + request_index
    if protocol == "deepseek":
        return {
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": prompt_tokens + completion_tokens,
            "prompt_tokens_details": {"cached_tokens": 11 + request_index},
        }
    return {
        "input_tokens": prompt_tokens,
        "output_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
        "input_tokens_details": {"cached_tokens": 11 + request_index},
    }


def _split_text(value: str | None) -> list[str]:
    if not value:
        return []
    midpoint = max(1, len(value) // 2)
    return [part for part in (value[:midpoint], value[midpoint:]) if part]


class _FakeSSEContent:
    def __init__(self, body: bytes):
        self.body = body

    async def iter_any(self):
        yield self.body


class _FakeResponse:
    def __init__(self, body: str | bytes):
        self.status = 200
        self.body = body
        self.content = _FakeSSEContent(body if isinstance(body, bytes) else body.encode("utf-8"))

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def text(self) -> str:
        return self.body.decode("utf-8") if isinstance(self.body, bytes) else self.body


class _FakeSession:
    def __init__(self, boundary: "_FakeHTTPBoundary"):
        self.boundary = boundary

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    def post(self, url: str, *, data: str | bytes, **kwargs) -> _FakeResponse:
        payload = json.loads(data)
        self.boundary.requests.append({"url": url, "payload": payload})
        request_index = len(self.boundary.requests) - 1
        if self.boundary.stream:
            events = self.boundary.stream_events(request_index, url)
            body = b"".join(f"data: {json.dumps(event, ensure_ascii=False, separators=(',', ':'))}\n\n".encode() for event in events)
            return _FakeResponse(body)

        response = self.boundary.json_response(request_index, url)
        return _FakeResponse(json.dumps(response, ensure_ascii=False, separators=(",", ":")))


class _FakeHTTPBoundary:
    def __init__(self):
        self.protocol = ""
        self.stream = False
        self.with_tools = False
        self.reasoning: str | None = ""
        self.requests: list[dict[str, Any]] = []

    def client_session(self, **kwargs) -> _FakeSession:
        return _FakeSession(self)

    def _content(self, request_index: int) -> str:
        if request_index == 0:
            return "正在查询天气" if self.with_tools else "首轮生成正文"
        return "二次出站回答"

    def _reasoning_item(self) -> dict[str, Any]:
        return {
            "id": "reasoning-item-1",
            "type": "reasoning",
            "status": "completed",
            "content": [{"type": "reasoning_text", "text": self.reasoning}],
            "private_reasoning": _PRIVATE_REASONING,
        }

    def _responses_output(self, request_index: int) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        if request_index == 0 and self.reasoning is not None:
            output.append(self._reasoning_item())
        output.append(
            {
                "id": f"message-{request_index}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": self._content(request_index)}],
            }
        )
        if request_index == 0 and self.with_tools:
            output.append(
                {
                    "id": "function-item-weather-1",
                    "call_id": _TOOL_ID,
                    "type": "function_call",
                    "status": "completed",
                    "name": _TOOL_NAME,
                    "arguments": json.dumps(_TOOL_ARGUMENTS, ensure_ascii=False, separators=(",", ":")),
                    "private_tool": _PRIVATE_TOOL,
                }
            )
        return output

    def json_response(self, request_index: int, url: str) -> dict[str, Any]:
        if url.endswith("/responses"):
            return {
                "id": f"response-{request_index}",
                "status": "completed",
                "model": _PROVIDER_MODEL,
                "output": self._responses_output(request_index),
                "usage": _usage(self.protocol, request_index),
                "private_response": _PRIVATE_RESPONSE,
            }

        message: dict[str, Any] = {
            "role": "assistant",
            "content": self._content(request_index),
            "private_message": _PRIVATE_MESSAGE,
        }
        if request_index == 0 and self.reasoning is not None:
            message["reasoning_content"] = self.reasoning
        if request_index == 0 and self.with_tools:
            message["tool_calls"] = [
                {
                    "id": _TOOL_ID,
                    "type": "function",
                    "private_tool": _PRIVATE_TOOL,
                    "function": {
                        "name": _TOOL_NAME,
                        "arguments": json.dumps(_TOOL_ARGUMENTS, ensure_ascii=False, separators=(",", ":")),
                    },
                }
            ]
        return {
            "id": f"chatcmpl-{request_index}",
            "model": _PROVIDER_MODEL,
            "choices": [
                {
                    "index": 0,
                    "message": message,
                    "finish_reason": "tool_calls" if request_index == 0 and self.with_tools else "stop",
                }
            ],
            "usage": _usage(self.protocol, request_index),
            "private_response": _PRIVATE_RESPONSE,
        }

    def _responses_event(self, request_index: int) -> dict[str, Any]:
        return {"type": "response.completed", "response": self.json_response(request_index, "/responses")}

    def _chat_events(self, request_index: int) -> list[dict[str, Any]]:
        content = self._content(request_index)
        role_delta: dict[str, Any] = {"role": "assistant"}
        if request_index == 0 and self.reasoning is None:
            role_delta["private_message"] = _PRIVATE_MESSAGE
        events: list[dict[str, Any]] = [{"model": _PROVIDER_MODEL, "choices": [{"index": 0, "delta": role_delta}]}]
        if request_index == 0:
            if self.reasoning is not None:
                reasoning_chunks = _split_text(self.reasoning)
                if not reasoning_chunks:
                    reasoning_chunks = [""]
                for index, chunk in enumerate(reasoning_chunks):
                    delta: dict[str, Any] = {"reasoning_content": chunk}
                    if index == 0:
                        delta["private_message"] = _PRIVATE_MESSAGE
                    events.append({"model": _PROVIDER_MODEL, "choices": [{"index": 0, "delta": delta}]})

        for chunk in _split_text(content):
            events.append({"model": _PROVIDER_MODEL, "choices": [{"index": 0, "delta": {"content": chunk}}]})

        if request_index == 0 and self.with_tools:
            arguments = json.dumps(_TOOL_ARGUMENTS, ensure_ascii=False, separators=(",", ":"))
            split_at = max(1, len(arguments) // 2)
            events.append(
                {
                    "model": _PROVIDER_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": _TOOL_ID,
                                        "type": "function",
                                        "function": {"name": _TOOL_NAME, "arguments": arguments[:split_at]},
                                    }
                                ]
                            },
                        }
                    ],
                }
            )
            events.append(
                {
                    "model": _PROVIDER_MODEL,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"tool_calls": [{"index": 0, "function": {"arguments": arguments[split_at:]}}]},
                        }
                    ],
                }
            )

        events.append(
            {
                "model": _PROVIDER_MODEL,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "tool_calls" if request_index == 0 and self.with_tools else "stop",
                    }
                ],
                "usage": _usage(self.protocol, request_index),
                "private_response": _PRIVATE_RESPONSE,
            }
        )
        return events

    def stream_events(self, request_index: int, url: str) -> list[dict[str, Any]]:
        if url.endswith("/responses"):
            return [self._responses_event(request_index)]
        return self._chat_events(request_index)


@pytest.fixture
def fake_http_boundary(monkeypatch: pytest.MonkeyPatch) -> _FakeHTTPBoundary:
    boundary = _FakeHTTPBoundary()

    async def load_llm_request_max_bytes() -> int:
        return 32 * 1024 * 1024

    monkeypatch.setattr(http_transport.aiohttp, "ClientSession", boundary.client_session)
    monkeypatch.setattr(http_transport.aiohttp, "TCPConnector", lambda **kwargs: object())
    monkeypatch.setattr(llm_request_policy, "load_llm_request_max_bytes", load_llm_request_max_bytes)
    return boundary


async def _generate(
    *,
    protocol: str,
    stream: bool,
    messages: list[InternalMessage],
    tools: list[dict[str, Any]] | None,
    on_content: Callable[[str], Any] | None = None,
    on_reasoning: Callable[[str], Any] | None = None,
):
    arguments = {
        "api_key": "test-api-key",
        "base_url": _BASE_URL,
        "model_id": _MODEL_ALIAS,
        "messages": messages,
        "max_tokens": 128,
        "tools": tools,
        "protocol": protocol,
        "channel_id": _CHANNEL_ID,
    }
    if stream:
        assert on_content is not None
        return await LLMClient.generate_with_stream_callback(
            **arguments,
            on_content=on_content,
            on_reasoning=on_reasoning,
        )
    return await LLMClient.generate(**arguments)


@pytest.mark.asyncio
@pytest.mark.parametrize("protocol", ["deepseek", "deepseek_responses"])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("with_tools", [False, True])
@pytest.mark.parametrize(
    "reasoning",
    [
        pytest.param("先核对杭州天气", id="reasoning"),
        pytest.param(" \t\n", id="whitespace-reasoning"),
        pytest.param(" \n先核对杭州天气\t ", id="padded-reasoning"),
        pytest.param("", id="empty-reasoning"),
        pytest.param(None, id="missing-reasoning"),
    ],
)
async def test_deepseek_reasoning_round_trip(
    db_session: AsyncSession,
    fake_http_boundary: _FakeHTTPBoundary,
    protocol: str,
    stream: bool,
    with_tools: bool,
    reasoning: str | None,
) -> None:
    fake_http_boundary.protocol = protocol
    fake_http_boundary.stream = stream
    fake_http_boundary.with_tools = with_tools
    fake_http_boundary.reasoning = reasoning

    db_session.add(ChatSession(session_id=_SESSION_ID, uid=_UID, profile_id=_PROFILE_ID))
    await db_session.commit()
    await save_message(
        db_session,
        _SESSION_ID,
        _UID,
        MessageRole.USER,
        MessageType.TEXT,
        "请查询杭州天气。",
        _PROFILE_ID,
    )

    tool_specs = (
        [
            {
                "type": "function",
                "function": {
                    "name": _TOOL_NAME,
                    "description": "查询指定城市天气",
                    "parameters": {
                        "type": "object",
                        "properties": {"城市": {"type": "string"}, "温度单位": {"type": "string"}},
                        "required": ["城市", "温度单位"],
                    },
                },
            }
        ]
        if with_tools
        else None
    )

    async def load_history() -> list[InternalMessage]:
        return await ContextManager.get_messages(
            db_session,
            _SESSION_ID,
            _UID,
            profile=None,
            current_message="",
        )

    async def collect_generation(request_messages: list[InternalMessage]):
        content_callbacks: list[str] = []
        reasoning_callbacks: list[str] = []

        async def on_content(value: str) -> None:
            content_callbacks.append(value)

        async def on_reasoning(value: str) -> None:
            reasoning_callbacks.append(value)

        response = await _generate(
            protocol=protocol,
            stream=stream,
            messages=request_messages,
            tools=tool_specs,
            on_content=on_content if stream else None,
            on_reasoning=on_reasoning if stream else None,
        )
        return response, content_callbacks, reasoning_callbacks

    first_history = await load_history()
    first_response, first_content_callbacks, first_reasoning_callbacks = await collect_generation(first_history)
    assert first_response.model == _PROVIDER_MODEL
    assert first_response.message.provider_metadata["source"] == {
        "channel_id": _CHANNEL_ID,
        "model_id": _MODEL_ALIAS,
        "protocol": protocol,
    }
    assert first_response.message.content == ("正在查询天气" if with_tools else "首轮生成正文")
    assert not reasoning or reasoning not in first_response.message.content
    assert first_response.usage == {
        "prompt_tokens": 31,
        "completion_tokens": 14,
        "total_tokens": 45,
        "cached_tokens": 11,
        **_usage(protocol, 0),
    }
    assert first_response.provider_metadata["response"]["private_response"] == _PRIVATE_RESPONSE
    if protocol == "deepseek_responses":
        if reasoning is None:
            assert first_response.message.provider_metadata["output"] == []
        else:
            assert first_response.message.provider_metadata["output"][0] == fake_http_boundary._reasoning_item()
    else:
        assert first_response.message.provider_metadata["message"]["private_message"] == _PRIVATE_MESSAGE
        if reasoning is None:
            assert "reasoning_content" not in first_response.message.provider_metadata["message"]

    if stream:
        first_content = "正在查询天气" if with_tools else "首轮生成正文"
        expected_content_callbacks = _split_text(first_content) if protocol == "deepseek" else [first_content]
        assert first_content_callbacks == expected_content_callbacks
        expected_reasoning_callbacks = _split_text(reasoning) if protocol == "deepseek" else ([reasoning] if reasoning else [])
        assert first_reasoning_callbacks == expected_reasoning_callbacks
        if reasoning is None:
            assert first_response.message.reasoning_content is None
        elif not reasoning:
            assert first_response.message.reasoning_content is None
            if protocol == "deepseek":
                assert first_response.message.provider_metadata["message"]["reasoning_content"] == ""
            else:
                assert first_response.message.provider_metadata["output"][0]["content"] == [{"type": "reasoning_text", "text": ""}]
        else:
            assert first_response.message.reasoning_content == reasoning
    else:
        assert first_response.message.reasoning_content == reasoning

    tool_calls = first_response.message.tool_calls or []
    assert len(tool_calls) == int(with_tools)
    normalized_tool_id = tool_calls[0].id if tool_calls else None
    if with_tools:
        assert normalized_tool_id != _TOOL_ID
        assert tool_calls[0].name == _TOOL_NAME
        assert tool_calls[0].arguments == _TOOL_ARGUMENTS

    saved_assistant = await save_assistant_message(
        db_session,
        _SESSION_ID,
        _UID,
        _PROFILE_ID,
        first_response.message,
    )
    if normalized_tool_id is not None:
        await save_tool_response(
            db_session,
            _SESSION_ID,
            _UID,
            _PROFILE_ID,
            InternalMessage(
                role=MessageRole.TOOL,
                content="杭州 24 摄氏度",
                tool_call_id=normalized_tool_id,
            ),
            messages=[],
            turn_messages=[],
        )
    await save_message(
        db_session,
        _SESSION_ID,
        _UID,
        MessageRole.USER,
        MessageType.TEXT,
        "请根据结果给出简短答复。",
        _PROFILE_ID,
    )

    db_session.expunge_all()
    restored_history = await load_history()
    restored_assistant = next(message for message in restored_history if message.role == MessageRole.ASSISTANT)
    assert restored_assistant.reasoning_content == first_response.message.reasoning_content
    assert restored_assistant.provider_metadata == first_response.message.provider_metadata
    if reasoning is None:
        assert restored_assistant.reasoning_content is None
        if protocol == "deepseek":
            assert "reasoning_content" not in restored_assistant.provider_metadata["message"]
        else:
            assert restored_assistant.provider_metadata["output"] == []
    assert restored_history[-1].role == MessageRole.USER
    assert restored_history[-1].content == "请根据结果给出简短答复。"

    assistant_row_result = await db_session.execute(select(Message).where(Message.id == saved_assistant.id).execution_options(populate_existing=True))
    assistant_row = assistant_row_result.scalar_one()
    assert assistant_row.reasoning_content == first_response.message.reasoning_content
    assert assistant_row.provider_metadata == first_response.message.provider_metadata
    public_message = MessageResponse.model_validate(assistant_row)
    assert "provider_metadata" not in public_message.model_dump()

    if with_tools:
        restored_call = restored_assistant.tool_calls[0]
        restored_tool = next(message for message in restored_history if message.role == MessageRole.TOOL)
        assert restored_call.id == normalized_tool_id
        assert restored_tool.tool_call_id == normalized_tool_id

    second_response, second_content_callbacks, second_reasoning_callbacks = await collect_generation(restored_history)
    assert restored_assistant.reasoning_content == first_response.message.reasoning_content
    assert restored_assistant.provider_metadata == first_response.message.provider_metadata
    assistant_row_result = await db_session.execute(select(Message).where(Message.id == saved_assistant.id).execution_options(populate_existing=True))
    assistant_row = assistant_row_result.scalar_one()
    assert assistant_row.reasoning_content == first_response.message.reasoning_content
    assert assistant_row.provider_metadata == first_response.message.provider_metadata
    assert second_response.model == _PROVIDER_MODEL
    assert second_response.message.content == "二次出站回答"
    assert second_response.usage == {
        "prompt_tokens": 32,
        "completion_tokens": 15,
        "total_tokens": 47,
        "cached_tokens": 12,
        **_usage(protocol, 1),
    }
    if stream:
        if protocol == "deepseek":
            assert second_content_callbacks == _split_text("二次出站回答")
        else:
            assert second_content_callbacks == ["二次出站回答"]
        assert second_reasoning_callbacks == []

    assert len(fake_http_boundary.requests) == 2
    first_payload, second_payload = (request["payload"] for request in fake_http_boundary.requests)
    assert first_payload["model"] == _MODEL_ALIAS
    assert second_payload["model"] == _MODEL_ALIAS

    if protocol == "deepseek":
        outbound_messages = second_payload["messages"]
        reasoning_messages = [message for message in outbound_messages if message.get("role") == "assistant" and isinstance(message.get("reasoning_content"), str)]
        assert len(reasoning_messages) == 1
        restored_reasoning_message = reasoning_messages[0]
        assert restored_reasoning_message["reasoning_content"] == (reasoning or "")
        if with_tools:
            outbound_calls = restored_reasoning_message["tool_calls"]
            assert len(outbound_calls) == 1
            assert outbound_calls[0]["id"] == normalized_tool_id
            assert outbound_calls[0]["function"]["name"] == _TOOL_NAME
            assert json.loads(outbound_calls[0]["function"]["arguments"]) == _TOOL_ARGUMENTS
            outbound_tool_results = [message for message in outbound_messages if message.get("role") == "tool"]
            assert len(outbound_tool_results) == 1
            assert outbound_tool_results[0]["tool_call_id"] == normalized_tool_id
            assert outbound_messages.index(restored_reasoning_message) < outbound_messages.index(outbound_tool_results[0])
        else:
            assert "tool_calls" not in restored_reasoning_message
            assert not any(message.get("role") == "tool" for message in outbound_messages)
    else:
        assert not {"store", "include", "previous_response_id"}.intersection(second_payload)
        outbound_input = second_payload["input"]
        reasoning_items = [item for item in outbound_input if item.get("type") == "reasoning"]
        assert len(reasoning_items) == 1
        expected_reasoning_item = {
            "type": "reasoning",
            "content": [
                {
                    "type": "reasoning_text",
                    "text": reasoning if isinstance(reasoning, str) and reasoning.strip() else "(thinking unavailable)",
                }
            ],
        }
        if reasoning is not None:
            expected_reasoning_item.update({"id": "reasoning-item-1", "status": "completed"})
        assert reasoning_items[0] == expected_reasoning_item
        if with_tools:
            outbound_calls = [item for item in outbound_input if item.get("type") == "function_call"]
            assert len(outbound_calls) == 1
            assert outbound_calls[0]["call_id"] == normalized_tool_id
            assert outbound_calls[0]["name"] == _TOOL_NAME
            assert json.loads(outbound_calls[0]["arguments"]) == _TOOL_ARGUMENTS
            outbound_tool_results = [item for item in outbound_input if item.get("type") == "function_call_output"]
            assert len(outbound_tool_results) == 1
            assert outbound_tool_results[0]["call_id"] == normalized_tool_id
            assert outbound_input.index(reasoning_items[0]) < outbound_input.index(outbound_calls[0])
        else:
            assert not any(item.get("type") in {"function_call", "function_call_output"} for item in outbound_input)

    serialized_payload = json.dumps(second_payload, ensure_ascii=False)
    for private_value in (_PRIVATE_RESPONSE, _PRIVATE_MESSAGE, _PRIVATE_REASONING, _PRIVATE_TOOL):
        assert private_value not in serialized_payload
    assert "provider_metadata" not in serialized_payload
