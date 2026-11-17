import pytest

from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient
from app.transformers.openai import OpenAIResponsesTransformer
from tests.unit.llm.openai_transformer_support import _mock_llm_request_max_bytes as _mock_llm_request_max_bytes


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


@pytest.mark.asyncio
async def test_responses_stream_argument_deltas_without_output_index_use_item_id(monkeypatch) -> None:
    argument_delta_indexes: set[tuple[str, int | str | None]] = set()
    argument_fallback_indexes: set[tuple[str, int | str | None]] = set()

    first_added_chunk, _ = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_item.added",
            "output_index": 1,
            "item": {
                "type": "function_call",
                "id": "item-1",
                "call_id": "call-1",
                "name": "lookup_a",
            },
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    first_arguments_chunk, _ = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-1",
            "delta": '{"value":"a"}',
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    second_added_chunk, _ = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_item.added",
            "output_index": 2,
            "item": {
                "type": "function_call",
                "id": "item-2",
                "call_id": "call-2",
                "name": "lookup_b",
            },
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    second_arguments_chunk, _ = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.function_call_arguments.delta",
            "item_id": "item-2",
            "delta": '{"value":"b"}',
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )

    assert first_added_chunk is not None
    assert first_arguments_chunk is not None
    assert second_added_chunk is not None
    assert second_arguments_chunk is not None
    argument_deltas = [first_arguments_chunk, second_arguments_chunk]
    assert [chunk["choices"][0]["delta"]["tool_calls"][0]["index"] for chunk in argument_deltas] == [None, None]
    assert [chunk["choices"][0]["delta"]["tool_calls"][0]["id"] for chunk in argument_deltas] == ["item-1", "item-2"]

    chunks = [first_added_chunk, first_arguments_chunk, second_added_chunk, second_arguments_chunk]

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
        protocol="openai_responses",
    )

    assert response.message.tool_calls is not None
    assert [(tool_call.name, tool_call.arguments) for tool_call in response.message.tool_calls] == [
        ("lookup_a", {"value": "a"}),
        ("lookup_b", {"value": "b"}),
    ]

    first_done_chunk, _ = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "item-1",
                "call_id": "call-1",
                "arguments": '{"value":"a"}',
            },
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )
    second_done_chunk, _ = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.output_item.done",
            "item": {
                "type": "function_call",
                "id": "item-2",
                "call_id": "call-2",
                "arguments": '{"value":"b"}',
            },
        },
        argument_delta_indexes=argument_delta_indexes,
        argument_fallback_indexes=argument_fallback_indexes,
    )

    assert first_done_chunk is not None
    assert second_done_chunk is not None
    chunks.extend([first_done_chunk, second_done_chunk])

    response = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="model",
        messages=[InternalMessage(role=MessageRole.USER, content="test")],
        on_content=on_content,
        protocol="openai_responses",
    )

    assert response.message.tool_calls is not None
    assert [(tool_call.name, tool_call.arguments) for tool_call in response.message.tool_calls] == [
        ("lookup_a", {"value": "a"}),
        ("lookup_b", {"value": "b"}),
    ]
