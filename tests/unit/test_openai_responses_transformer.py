import pytest

from app.core.constants import ERR_LLM_EMPTY_RESPONSE
from app.core.exceptions import LLMException
from app.models.channel import ChannelType, ModelChannel
from app.models.message import (
    FilePart,
    ImagePart,
    InternalMessage,
    InternalToolCall,
    MessageRole,
    TextPart,
)
from app.transformers.openai import OpenAITransformer
from app.transformers.openai_responses import OpenAIResponsesTransformer


def test_openai_usage_normalization() -> None:
    usage = OpenAITransformer._normalize_usage(
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

    invalid_usage = OpenAITransformer._normalize_usage(
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
    assert message.tool_calls == [
        InternalToolCall(id="call_1", name="lookup", arguments={"query": "value"}),
        InternalToolCall(id="call_2", name="fallback", arguments={}),
    ]


def test_responses_from_provider_rejects_empty_output() -> None:
    with pytest.raises(LLMException) as exc_info:
        OpenAIResponsesTransformer.from_provider({"output": []})

    assert exc_info.value.message == ERR_LLM_EMPTY_RESPONSE


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
    assert repeated_chunk is None
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
        "choices": [],
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
    }
    assert has_payload is False


@pytest.mark.parametrize(
    ("channel_type", "expected_protocol"),
    [
        (ChannelType.OPENAI_RESPONSES, "openai_responses"),
        (ChannelType.OPENAI, "openai"),
    ],
)
def test_model_channel_protocol(
    channel_type: ChannelType,
    expected_protocol: str,
) -> None:
    channel = ModelChannel(
        name=f"channel-{expected_protocol}",
        channel_type=channel_type,
        api_key="test-key",
    )

    assert channel.protocol == expected_protocol
