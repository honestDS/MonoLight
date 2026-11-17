import json
from typing import Any

import pytest

from app.core.constants import ERR_LLM_EMPTY_RESPONSE
from app.core.exceptions import LLMException
from app.models.message import InternalMessage, InternalToolCall, MessageRole
from app.providers.llm.client import LLMClient
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer
from app.transformers.openai import base as openai_base_module
from tests.unit.llm.openai_transformer_support import (
    _FakeAiohttpResponse,
    _FakeClientSession,
    _responses_sse_chunks,
)
from tests.unit.llm.openai_transformer_support import _mock_llm_request_max_bytes as _mock_llm_request_max_bytes


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

    response.message.provider_metadata = {
        **(response.message.provider_metadata or {}),
        "source": {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai_responses"},
    }
    normalized_tool_calls = LLMClient.normalize_tool_calls(response.message.tool_calls)
    assert normalized_tool_calls is not None
    normalized_message = response.message.model_copy(update={"tool_calls": normalized_tool_calls}, deep=True)
    provider_items = OpenAIResponsesTransformer.to_provider([normalized_message], model_id="gpt-test", channel_id=1)

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


def test_chat_completions_exposes_reasoning_content_as_standard_message_field() -> None:
    response = OpenAIChatCompletionsTransformer.to_internal_response(
        {
            "model": "gpt-test",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Answer",
                        "reasoning_content": "Think first.",
                    },
                    "finish_reason": "stop",
                }
            ],
            "usage": {},
        },
        "gpt-test",
    )

    assert response.message.content == "Answer"
    assert response.message.reasoning_content == "Think first."


def test_responses_exposes_reasoning_text_as_standard_message_field() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_reasoning",
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "content": [{"type": "reasoning_text", "text": "Think carefully."}],
                    "summary": [{"type": "summary_text", "text": "Summary."}],
                },
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "Answer"}],
                },
            ],
            "usage": {},
        },
        "gpt-test",
    )

    assert response.message.content == "Answer"
    assert response.message.reasoning_content == "Think carefully."


def test_responses_stream_reasoning_delta_is_normalized() -> None:
    chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.reasoning_text.delta",
            "output_index": 0,
            "content_index": 0,
            "delta": "Think ",
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
    )

    assert chunk == {"choices": [{"delta": {"reasoning_content": "Think "}}]}
    assert has_payload is True


def test_responses_accepts_reasoning_only_output() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_reasoning_only",
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": "Only reasoning."}],
                }
            ],
            "usage": {},
        },
        "gpt-test",
    )

    assert response.message.content is None
    assert response.message.reasoning_content == "Only reasoning."


def test_responses_completed_event_uses_reasoning_summary_as_stream_fallback() -> None:
    chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_summary",
                "status": "completed",
                "model": "gpt-test",
                "output": [
                    {
                        "type": "reasoning",
                        "content": [],
                        "encrypted_content": "opaque",
                        "summary": [{"type": "summary_text", "text": "Readable summary."}],
                    }
                ],
                "usage": {},
            },
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
        reasoning_delta_indexes=set(),
        reasoning_fallback_indexes=set(),
    )

    assert chunk["choices"][0]["delta"]["reasoning_content"] == "Readable summary."
    assert has_payload is True


def test_responses_completed_event_does_not_repeat_reasoning_after_stream_delta() -> None:
    reasoning_delta_indexes = {("summary", 0, 0)}
    chunk, has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.completed",
            "response": {
                "id": "resp_summary",
                "status": "completed",
                "model": "gpt-test",
                "output": [
                    {
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "Readable summary."}],
                    }
                ],
                "usage": {},
            },
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
        reasoning_delta_indexes=reasoning_delta_indexes,
        reasoning_fallback_indexes=set(),
    )

    assert chunk["choices"][0]["delta"] == {}
    assert has_payload is False


def test_responses_multiple_reasoning_parts_are_separated_by_blank_lines() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_reasoning_parts",
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "content": [],
                    "summary": [
                        {"type": "summary_text", "text": "First thought."},
                        {"type": "summary_text", "text": "Second thought."},
                    ],
                }
            ],
            "usage": {},
        },
        "gpt-test",
    )

    assert response.message.reasoning_content == "First thought.\n\nSecond thought."


def test_responses_stream_separates_distinct_reasoning_summary_parts() -> None:
    reasoning_delta_indexes: set[tuple[str, int | str | None, int | str | None]] = set()
    reasoning_fallback_indexes: set[tuple[str, int | str | None, int | str | None]] = set()

    first, first_has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "summary_index": 0,
            "delta": "First thought.",
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
        reasoning_delta_indexes=reasoning_delta_indexes,
        reasoning_fallback_indexes=reasoning_fallback_indexes,
    )
    second, second_has_payload = OpenAIResponsesTransformer._normalize_stream_event(
        {
            "type": "response.reasoning_summary_text.delta",
            "output_index": 0,
            "summary_index": 1,
            "delta": "Second thought.",
        },
        argument_delta_indexes=set(),
        argument_fallback_indexes=set(),
        reasoning_delta_indexes=reasoning_delta_indexes,
        reasoning_fallback_indexes=reasoning_fallback_indexes,
    )

    assert first_has_payload is True
    assert second_has_payload is True
    assert first["choices"][0]["delta"]["reasoning_content"] == "First thought."
    assert second["choices"][0]["delta"]["reasoning_content"] == "\n\nSecond thought."


@pytest.mark.parametrize(
    ("content", "reasoning_content", "tool_calls", "expected"),
    [
        (
            "Answer",
            "Think first.",
            None,
            {"role": "assistant", "content": "Answer", "reasoning_content": "Think first."},
        ),
        (
            None,
            "",
            [InternalToolCall(id="call_1", name="lookup", arguments={})],
            {
                "role": "assistant",
                "content": "[tool_call]",
                "reasoning_content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "lookup", "arguments": "{}"},
                    }
                ],
            },
        ),
        (
            "Answer",
            None,
            None,
            {"role": "assistant", "content": "Answer"},
        ),
    ],
)
def test_chat_completions_to_provider_sends_only_standard_reasoning_content(
    content: str | None,
    reasoning_content: str | None,
    tool_calls: list[InternalToolCall] | None,
    expected: dict[str, Any],
) -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=content,
        reasoning_content=reasoning_content,
        provider_metadata={"source": {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai"}},
        tool_calls=tool_calls,
    )
    original = message.model_copy(deep=True)

    assert OpenAIChatCompletionsTransformer.to_provider([message], channel_id=1, model_id="gpt-test") == [expected]
    assert message == original


@pytest.mark.parametrize("role", (MessageRole.USER, MessageRole.TOOL))
def test_chat_completions_to_provider_omits_reasoning_content_for_non_assistant(role: MessageRole) -> None:
    message = InternalMessage(
        role=role,
        content="Input",
        reasoning_content="Do not send",
        tool_call_id="call_1" if role == MessageRole.TOOL else None,
    )

    provider_message = OpenAIChatCompletionsTransformer.to_provider([message])[0]

    assert "reasoning_content" not in provider_message


@pytest.mark.parametrize(
    ("message_fields", "expected"),
    [
        ({"reasoning_content": "Standard", "reasoning": "Fallback"}, "Standard"),
        ({"reasoning_content": "", "reasoning": "Fallback"}, ""),
        ({"reasoning": "Fallback"}, "Fallback"),
        ({"reasoning_content": None, "reasoning": "Fallback"}, "Fallback"),
        ({"reasoning_content": 1, "reasoning": "Fallback"}, "Fallback"),
        ({"reasoning_content": [], "reasoning": {}}, None),
    ],
)
def test_chat_completions_from_provider_prefers_valid_reasoning_content(
    message_fields: dict[str, Any],
    expected: str | None,
) -> None:
    message = OpenAIChatCompletionsTransformer.from_provider(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Answer",
                        **message_fields,
                    }
                }
            ]
        }
    )

    assert message.reasoning_content == expected


@pytest.mark.parametrize(
    ("reasoning_content", "provider_metadata", "expected"),
    [
        (
            None,
            {
                "protocol": "openai_chat_completions",
                "message": {"reasoning": "Fallback"},
                "source": {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai"},
            },
            "Fallback",
        ),
        (
            None,
            {
                "protocol": "other",
                "message": {"reasoning": "Fallback"},
                "source": {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai"},
            },
            None,
        ),
        (
            "Standard",
            {
                "protocol": "openai_chat_completions",
                "message": {"reasoning": "Fallback"},
                "source": {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai"},
            },
            "Standard",
        ),
        (
            "",
            {
                "protocol": "openai_chat_completions",
                "message": {"reasoning": "Fallback"},
                "source": {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai"},
            },
            "",
        ),
        (
            None,
            {
                "protocol": "openai_chat_completions",
                "message": {"reasoning_content": "", "reasoning": "Fallback"},
                "source": {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai"},
            },
            "",
        ),
    ],
)
def test_chat_completions_to_provider_uses_only_same_protocol_reasoning_metadata(
    reasoning_content: str | None,
    provider_metadata: dict[str, Any],
    expected: str | None,
) -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        reasoning_content=reasoning_content,
        provider_metadata=provider_metadata,
    )

    provider_message = OpenAIChatCompletionsTransformer.to_provider([message], channel_id=1, model_id="gpt-test")[0]

    expected_message = {"role": "assistant", "content": "Answer"}
    if expected is not None:
        expected_message["reasoning_content"] = expected
    assert provider_message == expected_message


@pytest.mark.parametrize(
    ("provider_source", "request_kwargs"),
    [
        pytest.param(
            {"channel_id": 2, "model_id": "gpt-test", "protocol": "openai"},
            {"channel_id": 1, "model_id": "gpt-test"},
            id="channel-mismatch",
        ),
        pytest.param(
            {"channel_id": 1, "model_id": "other-model", "protocol": "openai"},
            {"channel_id": 1, "model_id": "gpt-test"},
            id="model-mismatch",
        ),
        pytest.param(
            {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai_responses"},
            {"channel_id": 1, "model_id": "gpt-test"},
            id="source-protocol-mismatch",
        ),
        pytest.param(
            None,
            {"channel_id": 1, "model_id": "gpt-test"},
            id="source-missing",
        ),
        pytest.param(
            {"channel_id": True, "model_id": "gpt-test", "protocol": "openai"},
            {"channel_id": 1, "model_id": "gpt-test"},
            id="channel-bool",
        ),
        pytest.param(
            {"channel_id": 0, "model_id": "gpt-test", "protocol": "openai"},
            {"channel_id": 1, "model_id": "gpt-test"},
            id="channel-zero",
        ),
        pytest.param(
            {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai"},
            {},
            id="request-source-missing",
        ),
    ],
)
def test_chat_completions_to_provider_discards_reasoning_for_mismatched_source(
    provider_source: dict[str, Any] | None,
    request_kwargs: dict[str, Any],
) -> None:
    provider_metadata: dict[str, Any] = {
        "protocol": "openai_chat_completions",
        "message": {"reasoning": "Fallback"},
    }
    tool_call_metadata: dict[str, Any] = {
        "protocol": "openai_chat_completions",
        "tool_call": {
            "id": "provider-call",
            "type": "function",
            "function": {"name": "provider_lookup", "arguments": "{}", "extra": "discard"},
        },
    }
    if provider_source is not None:
        provider_metadata["source"] = provider_source
        tool_call_metadata["source"] = provider_source

    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        reasoning_content="Standard",
        provider_metadata=provider_metadata,
        tool_calls=[
            InternalToolCall(
                id="call_1",
                name="lookup",
                arguments={"query": "value"},
                provider_metadata=tool_call_metadata,
            )
        ],
    )
    original = message.model_copy(deep=True)

    provider_message = OpenAIChatCompletionsTransformer.to_provider([message], **request_kwargs)[0]

    assert provider_message == {
        "role": "assistant",
        "content": "Answer",
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "lookup", "arguments": '{"query": "value"}'},
            }
        ],
    }
    assert "reasoning_content" not in provider_message
    assert message == original


@pytest.mark.parametrize(
    ("reasoning_deltas", "expected_reasoning", "expected_callbacks", "expected_provider_reasoning"),
    [
        (["Think ", "carefully."], "Think carefully.", ["Think ", "carefully."], "Think carefully."),
        ([""], None, [], ""),
    ],
)
@pytest.mark.asyncio
async def test_chat_completions_stream_reasoning_alias_round_trips_through_llm_client(
    monkeypatch,
    reasoning_deltas: list[str],
    expected_reasoning: str | None,
    expected_callbacks: list[str],
    expected_provider_reasoning: str,
) -> None:
    events = [
        {
            "id": "chatcmpl_reasoning",
            "model": "gpt-test",
            "choices": [{"index": 0, "delta": {"role": "assistant", "reasoning": reasoning_deltas[0]}, "finish_reason": None}],
        }
    ]
    events.extend(
        {
            "id": "chatcmpl_reasoning",
            "model": "gpt-test",
            "choices": [{"index": 0, "delta": {"reasoning": delta}, "finish_reason": None}],
        }
        for delta in reasoning_deltas[1:]
    )
    events.extend(
        [
            {
                "id": "chatcmpl_reasoning",
                "model": "gpt-test",
                "choices": [{"index": 0, "delta": {"content": "Answer"}, "finish_reason": None}],
            },
            {
                "id": "chatcmpl_reasoning",
                "model": "gpt-test",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            },
        ]
    )
    response = _FakeAiohttpResponse(chunks=_responses_sse_chunks(events))
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)
    content_callbacks: list[str] = []
    reasoning_callbacks: list[str] = []

    async def on_content(content: str) -> None:
        content_callbacks.append(content)

    async def on_reasoning(reasoning: str) -> None:
        reasoning_callbacks.append(reasoning)

    result = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-test",
        messages=[InternalMessage(role=MessageRole.USER, content="Answer")],
        on_content=on_content,
        on_reasoning=on_reasoning,
        protocol="openai",
        channel_id=1,
    )

    assert result.message.content == "Answer"
    assert result.message.reasoning_content == expected_reasoning
    assert content_callbacks == ["Answer"]
    assert reasoning_callbacks == expected_callbacks
    assert result.message.provider_metadata is not None
    assert result.message.provider_metadata["message"]["reasoning_content"] == expected_provider_reasoning
    assert OpenAIChatCompletionsTransformer.to_provider([result.message], channel_id=1, model_id="gpt-test") == [{"role": "assistant", "content": "Answer", "reasoning_content": expected_provider_reasoning}]
    assert json.loads(sessions[0].post_calls[0]["kwargs"]["data"])["stream"] is True


@pytest.mark.parametrize(
    ("delta", "expected_reasoning", "expected_payload"),
    [
        ({"reasoning_content": "", "reasoning": "Fallback"}, "", False),
        ({"reasoning": "Think"}, "Think", True),
    ],
)
def test_chat_completions_reasoning_stream_normalization_preserves_event_and_empty_payload_boundary(
    delta: dict[str, Any],
    expected_reasoning: str,
    expected_payload: bool,
) -> None:
    event = {"choices": [{"delta": delta}]}
    original = json.loads(json.dumps(event))

    normalized, has_payload = OpenAIChatCompletionsTransformer._normalize_stream_event(event)

    assert event == original
    assert normalized is not None
    assert normalized["choices"][0]["delta"]["reasoning_content"] == expected_reasoning
    assert has_payload is expected_payload


def test_responses_reasoning_items_round_trip_without_replaying_other_metadata() -> None:
    reasoning_items = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "status": "completed",
            "encrypted_content": "sealed-1",
            "summary": [{"type": "summary_text", "text": "First summary", "opaque": "keep-1"}],
            "content": [{"type": "reasoning_text", "text": "First thought", "opaque": {"source": "provider"}}],
            "vendor_field": {"trace": [1, 2]},
        },
        {
            "type": "reasoning",
            "id": "rs_2",
            "status": "completed",
            "encrypted_content": "sealed-2",
            "summary": [],
            "content": [{"type": "reasoning_text", "text": "Second thought"}],
            "opaque_value": "preserve-me",
        },
    ]
    provider_response = {
        "status": "completed",
        "model": "gpt-test",
        "output": [
            {
                "type": "message",
                "id": "msg_1",
                "status": "completed",
                "content": [{"type": "output_text", "text": "Answer"}],
                "message_opaque": {"do_not_replay": True},
            },
            reasoning_items[0],
            {
                "type": "image_generation_call",
                "id": "img_1",
                "status": "completed",
                "result": "QUJD",
                "output_format": "png",
            },
            {"type": "provider_specific_item", "opaque": "do-not-replay"},
            reasoning_items[1],
        ],
    }
    original_response = json.loads(json.dumps(provider_response))

    message = OpenAIResponsesTransformer.from_provider(provider_response)
    message.provider_metadata = {
        **(message.provider_metadata or {}),
        "source": {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai_responses"},
    }
    original_message = message.model_copy(deep=True)
    provider_items = OpenAIResponsesTransformer.to_provider([message], model_id="gpt-test", channel_id=1)

    assert provider_items == [*reasoning_items, {"role": "assistant", "content": "Answer"}]
    assert OpenAIResponsesTransformer.build_input_token_payload(
        model_id="gpt-test",
        channel_id=1,
        messages=[message],
        tools=None,
    ) == {"input": provider_items}
    assert message == original_message
    assert provider_response == original_response


@pytest.mark.parametrize("content", [None, "", []])
def test_responses_encrypted_only_reasoning_does_not_create_empty_assistant(content: Any) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_encrypted",
        "encrypted_content": "sealed-reasoning",
        "summary": [],
    }
    message = OpenAIResponsesTransformer.from_provider(
        {
            "status": "completed",
            "output": [reasoning_item],
        }
    )

    message.provider_metadata = {
        **(message.provider_metadata or {}),
        "source": {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai_responses"},
    }
    candidate = message.model_copy(update={"content": content}, deep=True)
    original_candidate = candidate.model_copy(deep=True)

    assert message.content is None
    assert message.reasoning_content is None
    assert OpenAIResponsesTransformer.to_provider([candidate], model_id="gpt-test", channel_id=1) == [reasoning_item]
    assert OpenAIChatCompletionsTransformer.to_provider([candidate], model_id="gpt-test", channel_id=1) == []
    assert candidate == original_candidate


@pytest.mark.parametrize(
    ("provider_source", "request_kwargs", "encrypted_only"),
    [
        pytest.param(
            {"channel_id": 2, "model_id": "gpt-test", "protocol": "openai_responses"},
            {"channel_id": 1, "model_id": "gpt-test"},
            False,
            id="channel-mismatch",
        ),
        pytest.param(
            {"channel_id": 1, "model_id": "other-model", "protocol": "openai_responses"},
            {"channel_id": 1, "model_id": "gpt-test"},
            False,
            id="model-mismatch",
        ),
        pytest.param(
            {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai"},
            {"channel_id": 1, "model_id": "gpt-test"},
            False,
            id="source-protocol-mismatch",
        ),
        pytest.param(
            None,
            {"channel_id": 1, "model_id": "gpt-test"},
            False,
            id="source-missing",
        ),
        pytest.param(
            {"channel_id": True, "model_id": "gpt-test", "protocol": "openai_responses"},
            {"channel_id": 1, "model_id": "gpt-test"},
            False,
            id="channel-bool",
        ),
        pytest.param(
            {"channel_id": 0, "model_id": "gpt-test", "protocol": "openai_responses"},
            {"channel_id": 1, "model_id": "gpt-test"},
            False,
            id="channel-zero",
        ),
        pytest.param(
            {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai_responses"},
            {},
            False,
            id="request-source-missing",
        ),
        pytest.param(
            {"channel_id": 1, "model_id": "gpt-test", "protocol": "openai_responses"},
            {},
            True,
            id="encrypted-only",
        ),
    ],
)
def test_responses_to_provider_discards_mismatched_source_state(
    provider_source: dict[str, Any] | None,
    request_kwargs: dict[str, Any],
    encrypted_only: bool,
) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_isolated",
        "encrypted_content": "sealed-reasoning",
        "summary": [],
    }
    provider_metadata: dict[str, Any] = {
        "protocol": "openai_responses",
        "output": [reasoning_item],
    }
    tool_call_metadata: dict[str, Any] = {
        "protocol": "openai_responses",
        "item": {
            "type": "function_call",
            "id": "fc_provider",
            "status": "completed",
            "extra": "discard",
        },
    }
    if provider_source is not None:
        provider_metadata["source"] = provider_source
        tool_call_metadata["source"] = provider_source

    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=None if encrypted_only else "Answer",
        reasoning_content=None if encrypted_only else "Standard reasoning",
        provider_metadata=provider_metadata,
        tool_calls=(
            None
            if encrypted_only
            else [
                InternalToolCall(
                    id="call_1",
                    name="lookup",
                    arguments={"query": "value"},
                    provider_metadata=tool_call_metadata,
                )
            ]
        ),
    )
    original = message.model_copy(deep=True)

    provider_items = OpenAIResponsesTransformer.to_provider([message], **request_kwargs)

    assert provider_items == (
        []
        if encrypted_only
        else [
            {"role": "assistant", "content": "Answer"},
            {
                "type": "function_call",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"query":"value"}',
            },
        ]
    )
    assert message == original


@pytest.mark.parametrize(
    ("role", "content", "reasoning_content", "metadata_protocol"),
    [
        (MessageRole.ASSISTANT, "Answer", None, "openai_chat_completions"),
        (MessageRole.USER, "Question", None, "openai_responses"),
        (MessageRole.TOOL, "Result", None, "openai_responses"),
        (MessageRole.ASSISTANT, "Answer", "Plain reasoning", None),
    ],
)
def test_responses_to_provider_does_not_invent_reasoning_for_other_protocol_or_roles(
    role: MessageRole,
    content: str,
    reasoning_content: str | None,
    metadata_protocol: str | None,
) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_1",
        "encrypted_content": "sealed-reasoning",
        "summary": [],
    }
    provider_metadata = {"protocol": metadata_protocol, "output": [reasoning_item]} if metadata_protocol is not None else None
    message = InternalMessage(
        role=role,
        content=content,
        reasoning_content=reasoning_content,
        provider_metadata=provider_metadata,
        tool_call_id="call_1" if role == MessageRole.TOOL else None,
    )

    if role == MessageRole.TOOL:
        expected = [{"type": "function_call_output", "call_id": "call_1", "output": "Result"}]
    else:
        expected = [{"role": role.value, "content": content}]
    assert OpenAIResponsesTransformer.to_provider([message]) == expected


@pytest.mark.parametrize(
    "reasoning_item",
    [
        {"type": "reasoning", "summary": []},
        {"type": "reasoning", "encrypted_content": "", "summary": []},
        {"type": "reasoning", "encrypted_content": None, "summary": []},
        {"type": "reasoning", "encrypted_content": 0, "summary": []},
    ],
)
def test_responses_empty_reasoning_without_valid_encrypted_content_raises(reasoning_item: dict[str, Any]) -> None:
    with pytest.raises(LLMException) as exc_info:
        OpenAIResponsesTransformer.from_provider({"status": "completed", "output": [reasoning_item]})

    assert exc_info.value.message == ERR_LLM_EMPTY_RESPONSE
