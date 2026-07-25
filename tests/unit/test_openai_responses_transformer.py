import json

import pytest

from app.core.constants import ERR_LLM_EMPTY_RESPONSE
from app.core.exceptions import LLMException
from app.models.message import (
    FilePart,
    ImagePart,
    InternalMessage,
    InternalToolCall,
    MessageRole,
    TextPart,
)
from app.providers.llm.client import LLMClient
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer
from app.transformers.openai import responses as openai_responses_module


class _AsyncBytesIterator:
    def __init__(self, chunks: list[bytes]):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


class _FakeAiohttpResponse:
    def __init__(self, *, text: str = "", chunks: list[bytes] | None = None):
        self.status = 200
        self._text = text
        self.content = self
        self._chunks = chunks or []

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def text(self) -> str:
        return self._text

    def iter_any(self):
        return _AsyncBytesIterator(self._chunks)


class _FakeClientSession:
    def __init__(self, response: _FakeAiohttpResponse):
        self._response = response

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def post(self, _url, **_kwargs):
        return self._response


def test_openai_usage_normalization() -> None:
    usage = OpenAIChatCompletionsTransformer._normalize_usage(
        {
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "total_tokens": 14,
            "prompt_tokens_details": {"cached_tokens": 3},
        }
    )

    assert {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")} == {
        "prompt_tokens": 10,
        "completion_tokens": 4,
        "total_tokens": 14,
        "cached_tokens": 3,
    }

    invalid_usage = OpenAIChatCompletionsTransformer._normalize_usage(
        {
            "prompt_tokens": True,
            "completion_tokens": -1,
            "total_tokens": False,
            "prompt_tokens_details": {"cached_tokens": -2},
        }
    )
    assert {key: invalid_usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")} == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }


def test_responses_usage_normalization() -> None:
    usage = OpenAIResponsesTransformer._normalize_responses_usage(
        {
            "input_tokens": 11,
            "input_tokens_details": {"cached_tokens": 5},
            "output_tokens": 7,
            "total_tokens": 18,
        }
    )

    assert {key: usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")} == {
        "prompt_tokens": 11,
        "completion_tokens": 7,
        "total_tokens": 18,
        "cached_tokens": 5,
    }


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

    monkeypatch.setattr(openai_responses_module.aiohttp, "TCPConnector", fake_tcp_connector)
    monkeypatch.setattr(openai_responses_module.aiohttp, "ClientSession", fake_client_session)

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

    monkeypatch.setattr(openai_responses_module.aiohttp, "TCPConnector", fake_tcp_connector)
    monkeypatch.setattr(openai_responses_module.aiohttp, "ClientSession", fake_client_session)

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


def test_responses_to_provider_preserves_content_and_tool_order() -> None:
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="Follow policy"),
        InternalMessage(
            role=MessageRole.USER,
            content=[
                TextPart.model_construct(type="text", text="Inspect this"),
                ImagePart.model_construct(
                    type="image_url",
                    image_url={"url": "https://example.com/image.png"},
                ),
                FilePart.model_construct(type="file", path="/tmp/report.txt"),
            ],
        ),
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content="I will look it up.",
            tool_calls=[
                InternalToolCall(
                    id="call_1",
                    name="lookup",
                    arguments={"query": "value"},
                )
            ],
        ),
        InternalMessage(
            role=MessageRole.TOOL,
            content="result",
            tool_call_id="call_1",
        ),
    ]

    assert OpenAIResponsesTransformer.to_provider(messages) == [
        {"role": "system", "content": "Follow policy"},
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Inspect this"},
                {
                    "type": "input_image",
                    "image_url": "https://example.com/image.png",
                    "detail": "auto",
                },
                {"type": "input_text", "text": "[Attached File: /tmp/report.txt]"},
            ],
        },
        {"role": "assistant", "content": "I will look it up."},
        {
            "type": "function_call",
            "call_id": "call_1",
            "name": "lookup",
            "arguments": '{"query":"value"}',
        },
        {
            "type": "function_call_output",
            "call_id": "call_1",
            "output": "result",
        },
    ]


def test_responses_from_provider_parses_text_and_tool_calls() -> None:
    message = OpenAIResponsesTransformer.from_provider(
        {
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Answer"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_1",
                    "name": "lookup",
                    "arguments": '{"query":"value"}',
                },
                {
                    "type": "function_call",
                    "id": "call_2",
                    "name": "fallback",
                    "arguments": "not-json",
                },
            ]
        }
    )

    assert message.role == MessageRole.ASSISTANT
    assert message.content == "Answer"
    assert message.provider_metadata == {
        "protocol": "openai_responses",
        "output": [{"type": "message"}],
    }
    assert message.tool_calls == [
        InternalToolCall(
            id="call_1",
            name="lookup",
            arguments={"query": "value"},
            provider_metadata={
                "protocol": "openai_responses",
                "item": {"type": "function_call"},
            },
        ),
        InternalToolCall(
            id="call_2",
            name="fallback",
            arguments={},
            provider_metadata={
                "protocol": "openai_responses",
                "item": {"type": "function_call", "id": "call_2"},
            },
        ),
    ]


def test_responses_from_provider_rejects_empty_output() -> None:
    with pytest.raises(LLMException) as exc_info:
        OpenAIResponsesTransformer.from_provider({"output": []})

    assert exc_info.value.message == ERR_LLM_EMPTY_RESPONSE


def test_chat_completions_refusal_is_visible_and_preserves_provider_fields() -> None:
    response = OpenAIChatCompletionsTransformer.to_internal_response(
        {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "model": "gpt-test",
            "service_tier": "default",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "logprobs": {"content": []},
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "refusal": "I cannot help with that.",
                        "audio": {"id": "audio_1"},
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 5,
                "completion_tokens": 2,
                "total_tokens": 7,
            },
        },
        default_model="fallback-model",
    )

    assert response.message.content == "I cannot help with that."
    assert response.message.refusal == "I cannot help with that."
    assert response.finish_reason == "refusal"
    assert response.finish_details == {"raw_finish_reason": "stop"}
    assert response.provider_metadata == {
        "protocol": "openai_chat_completions",
        "response": {
            "id": "chatcmpl_1",
            "object": "chat.completion",
            "service_tier": "default",
        },
        "choice": {"index": 0, "logprobs": {"content": []}},
        "message": {"role": "assistant", "audio": {"id": "audio_1"}},
    }
    assert response.message.provider_metadata == {
        "protocol": "openai_chat_completions",
        "choice": {"index": 0, "logprobs": {"content": []}},
        "message": {"role": "assistant", "audio": {"id": "audio_1"}},
    }


def test_responses_refusal_is_visible_and_has_refusal_finish_reason() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_refusal",
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "refusal",
                            "refusal": "Request refused.",
                            "reason": "policy",
                        }
                    ],
                }
            ],
        },
        default_model="fallback-model",
    )

    assert response.message.content == "Request refused."
    assert response.message.refusal == "Request refused."
    assert response.finish_reason == "refusal"
    assert response.finish_details == {
        "raw_finish_reason": "refusal",
        "status": "completed",
    }
    assert response.provider_metadata == {
        "protocol": "openai_responses",
        "response": {"id": "resp_refusal"},
    }
    assert response.message.provider_metadata == {
        "protocol": "openai_responses",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "refusal", "reason": "policy"}],
            }
        ],
    }


def test_responses_incomplete_max_output_tokens_allows_empty_message() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_incomplete",
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "model": "gpt-test",
            "output": [],
        },
        default_model="fallback-model",
    )

    assert response.message.content is None
    assert response.message.refusal is None
    assert response.message.tool_calls is None
    assert response.message.provider_metadata is None
    assert response.finish_reason == "length"
    assert response.finish_details == {
        "raw_finish_reason": "max_output_tokens",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    assert response.provider_metadata == {
        "protocol": "openai_responses",
        "response": {"id": "resp_incomplete"},
    }


def test_responses_reasoning_round_trip_uses_normalized_tool_call_id() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_tool",
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": "encrypted-reasoning",
                    "summary": [],
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "provider-call",
                    "status": "completed",
                    "name": "lookup",
                    "arguments": '{"query":"value"}',
                },
            ],
        },
        default_model="fallback-model",
    )

    assert response.finish_reason == "tool_calls"
    assert response.message.provider_metadata == {
        "protocol": "openai_responses",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "encrypted-reasoning",
                "summary": [],
            }
        ],
    }
    assert response.message.tool_calls is not None
    assert response.message.tool_calls[0].provider_metadata == {
        "protocol": "openai_responses",
        "item": {
            "type": "function_call",
            "id": "fc_1",
            "status": "completed",
        },
    }

    normalized_tool_calls = LLMClient.normalize_tool_calls(response.message.tool_calls)
    assert normalized_tool_calls is not None
    normalized_message = response.message.model_copy(update={"tool_calls": normalized_tool_calls}, deep=True)
    provider_items = OpenAIResponsesTransformer.to_provider([normalized_message])

    assert provider_items[0] == {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "encrypted-reasoning",
        "summary": [],
    }
    assert provider_items[1] == {
        "type": "function_call",
        "id": "fc_1",
        "status": "completed",
        "call_id": normalized_tool_calls[0].id,
        "name": "lookup",
        "arguments": '{"query":"value"}',
    }
    assert normalized_tool_calls[0].id.startswith("call_")
    assert normalized_tool_calls[0].id != "provider-call"
    assert normalized_tool_calls[0].provider_metadata == response.message.tool_calls[0].provider_metadata


def test_responses_stream_event_normalization() -> None:
    argument_delta_indexes: set[int | str | None] = set()
    argument_fallback_indexes: set[int | str | None] = set()

    text_chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {"type": "response.output_text.delta", "delta": "Hello"},
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    assert text_chunk == {"choices": [{"delta": {"content": "Hello"}}]}
    assert has_payload is True

    added_chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
            },
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    assert added_chunk == {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 1,
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "lookup"},
                            "provider_metadata": {
                                "protocol": "openai_responses",
                                "item": {"type": "function_call"},
                            },
                        }
                    ]
                }
            }
        ]
    }
    assert has_payload is True

    delta_chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.function_call_arguments.delta",
            "output_index": 1,
            "delta": '{"query":',
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    assert delta_chunk == {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 1,
                            "type": "function",
                            "function": {"arguments": '{"query":'},
                        }
                    ]
                }
            }
        ]
    }
    assert has_payload is True

    repeated_chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_item.done",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "arguments": '{"query":"value"}',
            },
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    assert repeated_chunk == {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 1,
                            "type": "function",
                            "provider_metadata": {
                                "protocol": "openai_responses",
                                "item": {"type": "function_call"},
                            },
                        }
                    ]
                }
            }
        ]
    }
    assert has_payload is False

    fallback_chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_item.done",
            "output_index": 2,
            "item": {
                "type": "function_call",
                "arguments": '{"other":true}',
            },
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    assert fallback_chunk == {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 2,
                            "type": "function",
                            "provider_metadata": {
                                "protocol": "openai_responses",
                                "item": {"type": "function_call"},
                            },
                            "function": {"arguments": '{"other":true}'},
                        }
                    ]
                }
            }
        ]
    }
    assert has_payload is True

    arguments_done_chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.function_call_arguments.done",
            "output_index": 3,
            "arguments": '{"done":true}',
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    assert arguments_done_chunk == {
        "choices": [
            {
                "delta": {
                    "tool_calls": [
                        {
                            "index": 3,
                            "type": "function",
                            "function": {"arguments": '{"done":true}'},
                        }
                    ]
                }
            }
        ]
    }
    assert has_payload is True

    repeated_done_chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.function_call_arguments.done",
            "output_index": 3,
            "arguments": '{"done":true}',
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    assert repeated_done_chunk is None
    assert has_payload is False

    completed_chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "model": "gpt-test",
                "usage": {
                    "input_tokens": 8,
                    "input_tokens_details": {"cached_tokens": 2},
                    "output_tokens": 3,
                    "total_tokens": 11,
                },
            },
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    assert completed_chunk == {
        "choices": [{"delta": {}, "finish_reason": "stop"}],
        "model": "gpt-test",
        "usage": {
            "input_tokens": 8,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens": 3,
            "total_tokens": 11,
            "prompt_tokens": 8,
            "completion_tokens": 3,
            "cached_tokens": 2,
        },
        "finish_details": {
            "raw_finish_reason": "stop",
            "status": "completed",
        },
        "provider_metadata": {
            "protocol": "openai_responses",
            "response": {"id": "resp_1"},
        },
        "message_provider_metadata": None,
    }
    assert has_payload is False


def test_responses_refusal_done_does_not_repeat_delta_and_can_fallback() -> None:
    argument_delta_indexes: set[int | str | None] = set()
    argument_fallback_indexes: set[int | str | None] = set()
    refusal_delta_indexes: set[tuple[int | str | None, int | str | None]] = set()
    refusal_fallback_indexes: set[tuple[int | str | None, int | str | None]] = set()

    delta_chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.refusal.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": "Request ",
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
        refusal_delta_indexes=refusal_delta_indexes,
        refusal_fallback_indexes=refusal_fallback_indexes,
    )
    done_after_delta, done_has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.refusal.done",
            "output_index": 0,
            "content_index": 0,
            "refusal": "Request refused.",
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
        refusal_delta_indexes=refusal_delta_indexes,
        refusal_fallback_indexes=refusal_fallback_indexes,
    )

    assert delta_chunk == {"choices": [{"delta": {"refusal": "Request "}}]}
    assert has_payload is True
    assert done_after_delta is None
    assert done_has_payload is False

    fallback_chunk, fallback_has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.refusal.done",
            "output_index": 1,
            "content_index": 0,
            "refusal": "Fallback refusal.",
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
        refusal_delta_indexes=refusal_delta_indexes,
        refusal_fallback_indexes=refusal_fallback_indexes,
    )

    assert fallback_chunk == {"choices": [{"delta": {"refusal": "Fallback refusal."}}]}
    assert fallback_has_payload is True


def test_responses_output_text_done_does_not_repeat_delta() -> None:
    text_delta_indexes: set[tuple[int | str | None, int | str | None]] = set()
    text_fallback_indexes: set[tuple[int | str | None, int | str | None]] = set()

    delta_chunk, delta_has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": "Answer",
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
        text_delta_indexes=text_delta_indexes,
        text_fallback_indexes=text_fallback_indexes,
    )
    done_chunk, done_has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_text.done",
            "output_index": 0,
            "content_index": 0,
            "text": "Answer",
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
        text_delta_indexes=text_delta_indexes,
        text_fallback_indexes=text_fallback_indexes,
    )

    assert delta_chunk == {"choices": [{"delta": {"content": "Answer"}}]}
    assert delta_has_payload is True
    assert done_chunk is None
    assert done_has_payload is False


def test_responses_output_text_done_is_used_when_delta_is_missing() -> None:
    chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_text.done",
            "output_index": 0,
            "content_index": 0,
            "text": "Complete answer",
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
        text_delta_indexes=set(),
        text_fallback_indexes=set(),
    )

    assert chunk == {"choices": [{"delta": {"content": "Complete answer"}}]}
    assert has_payload is True


def test_responses_incomplete_stream_event_returns_length_terminal_chunk() -> None:
    chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.incomplete",
            "response": {
                "id": "resp_incomplete",
                "status": "incomplete",
                "model": "gpt-test",
                "output": [],
                "incomplete_details": {"reason": "max_output_tokens"},
                "usage": {
                    "input_tokens": 4,
                    "output_tokens": 6,
                    "total_tokens": 10,
                },
            },
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
    )

    assert chunk is not None
    assert chunk["choices"] == [{"delta": {}, "finish_reason": "length"}]
    assert chunk["finish_details"] == {
        "raw_finish_reason": "max_output_tokens",
        "status": "incomplete",
        "incomplete_details": {"reason": "max_output_tokens"},
    }
    assert chunk["provider_metadata"] == {
        "protocol": "openai_responses",
        "response": {"id": "resp_incomplete"},
    }
    assert chunk["message_provider_metadata"] is None
    assert chunk["usage"]["completion_tokens"] == 6
    assert has_payload is False


@pytest.mark.parametrize(
    ("output", "expected_finish_reason"),
    [
        (
            [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Answer"}],
                }
            ],
            "stop",
        ),
        (
            [
                {
                    "type": "message",
                    "content": [{"type": "refusal", "refusal": "Request refused."}],
                }
            ],
            "refusal",
        ),
        (
            [
                {
                    "type": "function_call",
                    "call_id": "provider-call",
                    "name": "lookup",
                    "arguments": "{}",
                }
            ],
            "tool_calls",
        ),
    ],
)
def test_responses_completed_stream_event_reports_output_finish_reason(
    output: list[dict],
    expected_finish_reason: str,
) -> None:
    chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.completed",
            "response": {
                "id": f"resp_{expected_finish_reason}",
                "status": "completed",
                "model": "gpt-test",
                "output": output,
            },
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
    )

    assert chunk is not None
    assert chunk["choices"] == [{"delta": {}, "finish_reason": expected_finish_reason}]
    assert chunk["finish_details"] == {
        "raw_finish_reason": expected_finish_reason,
        "status": "completed",
    }
    assert chunk["provider_metadata"] == {
        "protocol": "openai_responses",
        "response": {"id": f"resp_{expected_finish_reason}"},
    }
    if expected_finish_reason == "tool_calls":
        assert chunk["message_provider_metadata"] is None
    else:
        assert chunk["message_provider_metadata"] == {
            "protocol": "openai_responses",
            "output": [{"type": "message"}],
        }
    assert has_payload is False
