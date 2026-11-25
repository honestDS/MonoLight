import json
from copy import deepcopy

import pytest

from app.core.constants import ERR_LLM_STREAM_TOOL_CALL_AMBIGUOUS
from app.core.exceptions import LLMException
from app.core.utils.dispatcher.save_message import _to_storable_content
from app.core.utils.request_token_baseline import extract_provider_token_metrics
from app.models.message import InternalMessage, InternalResponse, InternalToolCall, MessageRole, MessageType
from app.providers.llm.client import LLMClient
from app.transformers.openai import OpenAIChatCompletionsTransformer


@pytest.mark.asyncio
async def test_generate_with_stream_callback_emits_content_and_rebuilds_tool_calls(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"content": "你"}}]},
        {"choices": [{"delta": {"content": "好"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "vendor_tag": "initial",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"query":',
                                },
                            }
                        ]
                    }
                }
            ]
        },
        {
            "model": "model-final",
            "usage": {
                "prompt_tokens": 1,
                "completion_tokens": 2,
                "total_tokens": 3,
            },
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {
                                    "arguments": '"MonoLight"}',
                                },
                            }
                        ]
                    }
                }
            ],
        },
    ]
    emitted: list[str] = []

    async def generate_stream(cls, **kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(content: str) -> None:
        emitted.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
    )

    assert emitted == ["你", "好"]
    assert response.message.content == "你好"
    assert response.message.tool_calls is not None
    assert response.message.tool_calls[0].id.startswith("call_")
    assert response.message.tool_calls[0].id != "call-1"
    assert len(response.message.tool_calls[0].id) == 37
    int(response.message.tool_calls[0].id.removeprefix("call_"), 16)
    assert response.message.tool_calls[0].name == "search"
    assert response.message.tool_calls[0].arguments == {"query": "MonoLight"}
    assert response.message.tool_calls[0].provider_metadata == {
        "protocol": "openai_chat_completions",
        "tool_call": {
            "type": "function",
            "vendor_tag": "initial",
        },
    }
    assert response.model == "model-final"
    assert response.usage["total_tokens"] == 3


@pytest.mark.asyncio
async def test_generate_with_stream_callback_merges_partial_stream_usage(monkeypatch):
    raw_chunks = [
        {
            "choices": [{"delta": {"content": "answer"}}],
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 2,
                "total_tokens": 12,
                "prompt_tokens_details": {"cached_tokens": 4, "audio_tokens": 1},
            },
        },
        {"usage": None},
        {"usage": {}},
        {"usage": {"completion_tokens": 3}},
        {"usage": {"prompt_tokens_details": {}}},
        {"usage": {"prompt_tokens_details": {"cached_tokens": 0}}},
    ]
    chunks = []
    for raw_chunk in raw_chunks:
        normalized, _ = OpenAIChatCompletionsTransformer._normalize_stream_event(raw_chunk)
        assert normalized is not None
        chunks.append(normalized)

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(_content: str) -> None:
        return None

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
    )

    assert isinstance(response, InternalResponse)
    assert response.usage == {
        "prompt_tokens": 10,
        "completion_tokens": 3,
        "total_tokens": 12,
        "prompt_tokens_details": {"cached_tokens": 0, "audio_tokens": 1},
        "cached_tokens": 0,
    }
    assert extract_provider_token_metrics(response.usage)["input_tokens"] == 10


@pytest.mark.asyncio
async def test_generate_with_stream_callback_replaces_later_complete_stream_usage(monkeypatch):
    raw_chunks = [
        {
            "choices": [{"delta": {"content": "answer"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 2, "total_tokens": 12},
        },
        {
            "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23},
        },
    ]
    chunks = []
    for raw_chunk in raw_chunks:
        normalized, _ = OpenAIChatCompletionsTransformer._normalize_stream_event(raw_chunk)
        assert normalized is not None
        chunks.append(normalized)

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(_content: str) -> None:
        return None

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
    )

    assert isinstance(response, InternalResponse)
    assert response.usage == {
        "prompt_tokens": 20,
        "completion_tokens": 3,
        "total_tokens": 23,
        "cached_tokens": None,
    }
    assert extract_provider_token_metrics(response.usage)["input_tokens"] == 20


@pytest.mark.asyncio
async def test_generate_with_stream_callback_does_not_share_usage_between_requests(monkeypatch):
    raw_streams = [
        [
            {
                "choices": [{"delta": {"content": "first"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 1, "total_tokens": 8},
            }
        ],
        [{"choices": [{"delta": {"content": "second"}}]}],
    ]

    async def generate_stream(cls, **_kwargs):
        for raw_chunk in raw_streams.pop(0):
            normalized, _ = OpenAIChatCompletionsTransformer._normalize_stream_event(raw_chunk)
            assert normalized is not None
            yield normalized

    async def on_content(_content: str) -> None:
        return None

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    first_response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="first")],
        on_content=on_content,
    )
    second_response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="second")],
        on_content=on_content,
    )

    assert isinstance(first_response, InternalResponse)
    assert isinstance(second_response, InternalResponse)
    assert first_response.usage["prompt_tokens"] == 7
    assert extract_provider_token_metrics(first_response.usage)["input_tokens"] == 7
    assert second_response.usage == {}
    assert extract_provider_token_metrics(second_response.usage) == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_call_placeholder", "expected_emitted", "expected_content"),
    [
        pytest.param(None, [], None, id="default"),
        pytest.param(False, [], None, id="disabled"),
        pytest.param(True, ["  [tool", "_call]  "], "  [tool_call]  ", id="enabled"),
    ],
)
async def test_generate_with_stream_callback_handles_split_tool_call_placeholder(
    monkeypatch,
    tool_call_placeholder,
    expected_emitted,
    expected_content,
):
    chunks = [
        {"choices": [{"delta": {"content": "  [tool"}}]},
        {"choices": [{"delta": {"content": "_call]  "}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"query":"MonoLight"}',
                                },
                            }
                        ]
                    }
                }
            ]
        },
    ]
    emitted: list[str] = []

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(content: str) -> None:
        emitted.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    placeholder_option = {} if tool_call_placeholder is None else {"tool_call_placeholder": tool_call_placeholder}
    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai",
        **placeholder_option,
    )

    assert emitted == expected_emitted
    assert response.message.content == expected_content
    assert response.message.tool_calls is not None
    assert len(response.message.tool_calls) == 1
    assert response.message.tool_calls[0].name == "search"
    assert response.message.tool_calls[0].arguments == {"query": "MonoLight"}
    stored_content = _to_storable_content(response.message, MessageType.TOOL_CALL)
    stored_payload = json.loads(stored_content)
    assert "tool_calls" in stored_payload
    if expected_content is None:
        assert "[tool_call]" not in stored_content
        assert "content" not in stored_payload
    else:
        assert stored_payload["content"] == "  [tool_call]  "


@pytest.mark.asyncio
async def test_generate_with_stream_callback_releases_placeholder_without_tool_call(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"content": "  [tool"}}]},
        {"choices": [{"delta": {"content": "_call]  "}}]},
    ]
    emitted: list[str] = []

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(content: str) -> None:
        emitted.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai",
    )

    assert emitted == ["  [tool", "_call]  "]
    assert response.message.content == "  [tool_call]  "
    assert response.message.tool_calls is None


@pytest.mark.asyncio
async def test_generate_with_stream_callback_keeps_tool_call_explanation_content(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"content": "  [tool"}}]},
        {"choices": [{"delta": {"content": "_call]  "}}]},
        {"choices": [{"delta": {"content": " explanation"}}]},
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "search",
                                    "arguments": '{"query":"MonoLight"}',
                                },
                            }
                        ]
                    }
                }
            ]
        },
    ]
    emitted: list[str] = []

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(content: str) -> None:
        emitted.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai",
    )

    assert emitted == ["  [tool", "_call]  ", " explanation"]
    assert response.message.content == "  [tool_call]   explanation"
    assert response.message.tool_calls is not None
    assert response.message.tool_calls[0].name == "search"
    assert response.message.tool_calls[0].arguments == {"query": "MonoLight"}


@pytest.mark.asyncio
async def test_chat_stream_concatenates_reasoning_content_metadata(monkeypatch):
    chunks = [
        {"choices": [{"delta": {"reasoning_content": "First reasoning. "}}]},
        {"choices": [{"delta": {"reasoning_content": "Second reasoning."}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    emitted_reasoning: list[str] = []

    async def on_content(_content: str) -> None:
        return None

    async def on_reasoning(content: str) -> None:
        emitted_reasoning.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        on_reasoning=on_reasoning,
        protocol="openai",
    )

    assert emitted_reasoning == ["First reasoning. ", "Second reasoning."]
    assert response.message.reasoning_content == "First reasoning. Second reasoning."


@pytest.mark.asyncio
async def test_chat_stream_refusal_is_visible_and_normalizes_stop(monkeypatch):
    chunks = [
        {
            "id": "chatcmpl_refusal",
            "object": "chat.completion.chunk",
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        "role": "assistant",
                        "refusal": "Request ",
                    },
                }
            ],
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"refusal": "refused."},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "stop",
                }
            ]
        },
    ]
    emitted: list[str] = []

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(content: str) -> None:
        emitted.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai",
    )

    assert emitted == ["Request ", "refused."]
    assert response.message.content == "Request refused."
    assert response.message.refusal == "Request refused."
    assert response.finish_reason == "refusal"
    assert response.finish_details == {"raw_finish_reason": "stop"}
    assert response.provider_metadata == {
        "protocol": "openai_chat_completions",
        "response": {
            "id": "chatcmpl_refusal",
            "object": "chat.completion.chunk",
        },
        "choice": {"index": 0},
        "message": {"role": "assistant"},
    }
    assert response.message.provider_metadata == {
        "protocol": "openai_chat_completions",
        "choice": {"index": 0},
        "message": {"role": "assistant"},
    }


@pytest.mark.asyncio
async def test_chat_stream_length_returns_partial_content_and_raw_reason(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": "Partial answer"},
                }
            ]
        },
        {
            "choices": [
                {
                    "index": 0,
                    "delta": {},
                    "finish_reason": "length",
                }
            ]
        },
    ]
    emitted: list[str] = []

    async def generate_stream(cls, **_kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(content: str) -> None:
        emitted.append(content)

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai",
    )

    assert emitted == ["Partial answer"]
    assert response.message.content == "Partial answer"
    assert response.message.refusal is None
    assert response.finish_reason == "length"
    assert response.finish_details == {"raw_finish_reason": "length"}


async def _collect_stream_tool_calls(monkeypatch, chunks):
    async def generate_stream(cls, **kwargs):
        for chunk in chunks:
            yield chunk

    async def on_content(_content):
        return None

    monkeypatch.setattr(LLMClient, "generate_stream", classmethod(generate_stream))
    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
    )
    return response.message.tool_calls


@pytest.mark.asyncio
async def test_stream_tool_call_replay_on_another_index_is_assembled_once(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-X-0",
                                "function": {"name": "execute_shell", "arguments": ""},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "call-X-0",
                                "function": {"arguments": '{"command":"python test.py"}'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 1,
                                "id": "call-X-0",
                                "function": {
                                    "name": "execute_shell",
                                    "arguments": '{"command":"python test.py"}',
                                },
                            }
                        ]
                    }
                }
            ]
        },
    ]
    original_chunks = deepcopy(chunks)

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "execute_shell"
    assert tool_calls[0].arguments == {"command": "python test.py"}
    assert chunks == original_chunks


@pytest.mark.asyncio
@pytest.mark.parametrize("include_provider_ids", [True, False])
async def test_stream_tool_calls_preserve_identical_batch_calls(monkeypatch, include_provider_ids):
    first = {
        "index": 0,
        "function": {"name": "search", "arguments": '{"query":"MonoLight"}'},
    }
    second = {
        "index": 1,
        "function": {"name": "search", "arguments": '{"query":"MonoLight"}'},
    }
    if include_provider_ids:
        first["id"] = "provider-call-1"
        second["id"] = "provider-call-2"
    chunks = [{"choices": [{"delta": {"tool_calls": [first, second]}}]}]

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 2
    assert [tool_call.name for tool_call in tool_calls] == ["search", "search"]
    assert [tool_call.arguments for tool_call in tool_calls] == [
        {"query": "MonoLight"},
        {"query": "MonoLight"},
    ]


@pytest.mark.asyncio
async def test_stream_tool_call_binds_late_id_and_keeps_argument_fragments(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "function": {"name": "search", "arguments": '{"query":"'},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "provider-call",
                                "function": {"arguments": "Mono"},
                            }
                        ]
                    }
                }
            ]
        },
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "provider-call",
                                "function": {"arguments": 'Light"}'},
                            }
                        ]
                    }
                }
            ]
        },
    ]

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0].name == "search"
    assert tool_calls[0].arguments == {"query": "MonoLight"}


@pytest.mark.asyncio
async def test_stream_tool_call_keeps_identical_fragments_on_same_index(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "provider-call",
                                "function": {"name": "search", "arguments": '{"query":"'},
                            }
                        ]
                    }
                }
            ]
        },
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "same"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "same"}}]}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '"}'}}]}}]},
    ]

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 1
    assert tool_calls[0].arguments == {"query": "samesame"}


@pytest.mark.asyncio
async def test_stream_tool_calls_with_same_id_but_different_content_stay_separate(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {
                                "index": 0,
                                "id": "reused-provider-id",
                                "function": {"name": "search", "arguments": '{"query":"one"}'},
                            },
                            {
                                "index": 1,
                                "id": "reused-provider-id",
                                "function": {"name": "search", "arguments": '{"query":"two"}'},
                            },
                        ]
                    }
                }
            ]
        }
    ]

    tool_calls = await _collect_stream_tool_calls(monkeypatch, chunks)

    assert tool_calls is not None
    assert len(tool_calls) == 2
    assert [(tool_call.name, tool_call.arguments) for tool_call in tool_calls] == [
        ("search", {"query": "one"}),
        ("search", {"query": "two"}),
    ]


@pytest.mark.asyncio
async def test_stream_tool_calls_without_identity_in_same_delta_raise_llm_exception(monkeypatch):
    chunks = [
        {
            "choices": [
                {
                    "delta": {
                        "tool_calls": [
                            {"function": {"name": "search", "arguments": "{}"}},
                            {"function": {"name": "search", "arguments": "{}"}},
                        ]
                    }
                }
            ]
        }
    ]

    with pytest.raises(LLMException) as exc_info:
        await _collect_stream_tool_calls(monkeypatch, chunks)

    assert exc_info.value.message == ERR_LLM_STREAM_TOOL_CALL_AMBIGUOUS


def test_normalize_tool_calls_replaces_upstream_ids_and_preserves_identical_calls():
    upstream_calls = [
        InternalToolCall(id="upstream-1", name="search", arguments={"query": "MonoLight"}),
        InternalToolCall(id="upstream-2", name="search", arguments={"query": "MonoLight"}),
        InternalToolCall(id="upstream-1", name="search", arguments={"query": "Kilo"}),
    ]

    normalized_calls = LLMClient.normalize_tool_calls(upstream_calls)

    assert normalized_calls is not None
    assert len(normalized_calls) == 3
    assert [tool_call.arguments for tool_call in normalized_calls] == [{"query": "MonoLight"}, {"query": "MonoLight"}, {"query": "Kilo"}]
    assert len({tool_call.id for tool_call in normalized_calls}) == 3
    assert all(tool_call.id.startswith("call_") and len(tool_call.id) == 37 for tool_call in normalized_calls)
    assert [tool_call.id for tool_call in upstream_calls] == ["upstream-1", "upstream-2", "upstream-1"]


@pytest.mark.asyncio
async def test_generate_replaces_provider_tool_call_ids(monkeypatch):
    class Transformer:
        async def generate(self, **_kwargs):
            return {"model": "model-final"}

        def to_internal_response(self, _raw_response, default_model, **_kwargs):
            return InternalResponse(
                message=InternalMessage(
                    role=MessageRole.ASSISTANT,
                    tool_calls=[
                        InternalToolCall(
                            id="provider-call",
                            name="search",
                            arguments={"query": "MonoLight"},
                            provider_metadata={
                                "protocol": "test",
                                "tool_call": {
                                    "type": "function",
                                    "vendor_tag": "preserved",
                                },
                            },
                        )
                    ],
                ),
                model=_raw_response.get("model", default_model),
                finish_reason="tool_calls",
                provider_metadata={
                    "protocol": "test",
                    "response": {"trace_id": "trace-1"},
                },
            )

    monkeypatch.setitem(LLMClient._transformers, "test", Transformer())

    response = await LLMClient.generate(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        protocol="test",
    )

    assert response.message.tool_calls is not None
    assert response.message.tool_calls[0].id.startswith("call_")
    assert response.message.tool_calls[0].id != "provider-call"
    assert response.message.tool_calls[0].provider_metadata == {
        "protocol": "test",
        "tool_call": {
            "type": "function",
            "vendor_tag": "preserved",
        },
    }
    assert response.finish_reason == "tool_calls"
    assert response.model == "model-final"
    assert response.provider_metadata == {
        "protocol": "test",
        "response": {"trace_id": "trace-1"},
    }
