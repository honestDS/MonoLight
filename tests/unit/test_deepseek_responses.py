import json

import pytest

from app.core.constants import (
    ERR_LLM_API_RESPONSE_ERROR,
    ERR_LLM_EMPTY_RESPONSE,
    ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED,
)
from app.core.exceptions import LLMContextLengthException, LLMException
from app.models.message import AudioPart, ImagePart, InternalMessage, InternalToolCall, MessageRole, TextPart
from app.transformers import http_transport
from app.transformers.deepseek.responses import DeepSeekResponsesTransformer


def _source(protocol: str, *, channel_id: int = 17, model_id: str = "deepseek-model") -> dict[str, object]:
    return {"channel_id": channel_id, "model_id": model_id, "protocol": protocol}


@pytest.mark.parametrize("reasoning_text", ["private reasoning", ""], ids=["text", "empty-text"])
def test_to_provider_replays_sanitized_native_deepseek_reasoning(reasoning_text: str) -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        provider_metadata={
            "protocol": "deepseek_responses",
            "source": _source("deepseek_responses"),
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_123",
                    "status": "completed",
                    "content": [{"type": "reasoning_text", "text": reasoning_text}],
                    "summary": [{"type": "summary_text", "text": "summary is not reasoning"}],
                    "encrypted_content": "sealed",
                    "provider_extension": "discard",
                }
            ],
        },
    )
    original_message = message.model_dump(mode="python")

    provider_items = DeepSeekResponsesTransformer.to_provider([message], model_id="deepseek-model", channel_id=17)

    assert provider_items == [
        {
            "type": "reasoning",
            "id": "rs_123",
            "status": "completed",
            "content": [{"type": "reasoning_text", "text": "(thinking unavailable)" if reasoning_text == "" else reasoning_text}],
        },
        {"role": "assistant", "content": "Answer"},
    ]
    assert message.model_dump(mode="python") == original_message


@pytest.mark.parametrize(
    ("source_protocol", "reasoning_content", "metadata_reasoning", "expected_reasoning"),
    [
        ("deepseek", "DeepSeek reasoning", "metadata fallback", "DeepSeek reasoning"),
        ("deepseek", "", "metadata fallback", "metadata fallback"),
        ("deepseek", None, "metadata reasoning", "metadata reasoning"),
        ("deepseek", " \t\n", " \nrecoverable metadata\t ", " \nrecoverable metadata\t "),
        ("deepseek", "", " \t\n", "(thinking unavailable)"),
        ("openai", "OpenAI reasoning", "metadata fallback", "OpenAI reasoning"),
        ("openai", "", "metadata fallback", "metadata fallback"),
        ("openai", None, "metadata reasoning", "metadata reasoning"),
        ("openai", " \t\n", " \nrecoverable metadata\t ", " \nrecoverable metadata\t "),
        ("openai", "", " \t\n", "(thinking unavailable)"),
    ],
    ids=[
        "deepseek-content",
        "deepseek-empty-content",
        "deepseek-metadata",
        "deepseek-whitespace-fallback",
        "deepseek-unavailable",
        "openai-content",
        "openai-empty-content",
        "openai-metadata",
        "openai-whitespace-fallback",
        "openai-unavailable",
    ],
)
def test_to_provider_maps_matching_chat_reasoning_to_reasoning_text(
    source_protocol: str,
    reasoning_content: str | None,
    metadata_reasoning: str,
    expected_reasoning: str,
) -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        reasoning_content=reasoning_content,
        provider_metadata={
            "protocol": source_protocol,
            "source": _source(source_protocol),
            "message": {"reasoning": metadata_reasoning},
        },
    )

    assert DeepSeekResponsesTransformer.to_provider([message], model_id="deepseek-model", channel_id=17) == [
        {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": expected_reasoning}],
        },
        {"role": "assistant", "content": "Answer"},
    ]


def test_to_provider_accepts_legacy_openai_chat_metadata_for_openai_source() -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        provider_metadata={
            "protocol": "openai_chat_completions",
            "source": _source("openai"),
            "message": {"reasoning": "legacy chat reasoning"},
        },
    )

    assert DeepSeekResponsesTransformer.to_provider([message], model_id="deepseek-model", channel_id=17) == [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "legacy chat reasoning"}]},
        {"role": "assistant", "content": "Answer"},
    ]


def test_to_provider_maps_only_openai_response_reasoning_text_content() -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        reasoning_content="unscoped internal reasoning is ignored",
        provider_metadata={
            "protocol": "openai_responses",
            "source": _source("openai_responses"),
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_source",
                    "content": [
                        {"type": "reasoning_text", "text": "recoverable plaintext"},
                        {"type": "summary_text", "text": "summary must not be sent"},
                        {"type": "reasoning_text", "text": 123},
                    ],
                    "summary": [{"type": "summary_text", "text": "top-level summary"}],
                    "encrypted_content": "sealed",
                },
                {
                    "type": "reasoning",
                    "content": [],
                    "summary": [{"type": "summary_text", "text": "summary only"}],
                    "encrypted_content": "another sealed value",
                },
            ],
        },
    )

    assert DeepSeekResponsesTransformer.to_provider([message], model_id="deepseek-model", channel_id=17) == [
        {
            "type": "reasoning",
            "content": [{"type": "reasoning_text", "text": "recoverable plaintext"}],
        },
        {"role": "assistant", "content": "Answer"},
    ]


@pytest.mark.parametrize(
    ("source", "metadata_protocol"),
    [
        (None, "deepseek_responses"),
        (_source("deepseek_responses", channel_id=18), "deepseek_responses"),
        (_source("deepseek_responses", model_id="other-model"), "deepseek_responses"),
        (_source("unknown_protocol"), "unknown_protocol"),
        (_source("deepseek"), "openai_responses"),
        (_source("deepseek_responses"), "openai_chat_completions"),
    ],
    ids=["missing-source", "other-channel", "other-model", "unknown-protocol", "protocol-mismatch", "legacy-protocol-mismatch"],
)
def test_to_provider_does_not_leak_incompatible_reasoning_but_adds_placeholder(source, metadata_protocol: str) -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        reasoning_content="private reasoning",
        provider_metadata={
            "protocol": metadata_protocol,
            "source": source,
            "message": {"reasoning": "metadata reasoning"},
            "output": [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "native reasoning"}]}],
        },
    )
    original_message = message.model_dump(mode="python")

    assert DeepSeekResponsesTransformer.to_provider([message], model_id="deepseek-model", channel_id=17) == [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "(thinking unavailable)"}]},
        {"role": "assistant", "content": "Answer"},
    ]
    assert message.model_dump(mode="python") == original_message


@pytest.mark.parametrize(
    "provider_metadata",
    [
        pytest.param(None, id="no-metadata"),
        pytest.param(
            {"protocol": "deepseek_responses", "source": _source("deepseek_responses")},
            id="matching-source-without-output",
        ),
        pytest.param(
            {"protocol": "deepseek_responses", "source": _source("deepseek_responses"), "output": []},
            id="matching-source-with-empty-output",
        ),
    ],
)
def test_to_provider_adds_placeholder_reasoning_item_for_assistant_text_without_reasoning(
    provider_metadata: dict[str, object] | None,
) -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        reasoning_content=None,
        provider_metadata=provider_metadata,
    )
    expected_input = [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "(thinking unavailable)"}]},
        {"role": "assistant", "content": "Answer"},
    ]
    original_message = message.model_dump(mode="python")

    provider_input = DeepSeekResponsesTransformer.to_provider([message], model_id="deepseek-model", channel_id=17)
    token_payload = DeepSeekResponsesTransformer.build_input_token_payload(
        model_id="deepseek-model",
        channel_id=17,
        messages=[message],
        tools=None,
    )

    assert provider_input == expected_input
    assert token_payload == {"input": expected_input}
    assert token_payload["input"] == provider_input
    assert message.model_dump(mode="python") == original_message


@pytest.mark.parametrize(
    "content",
    [
        pytest.param(None, id="none"),
        pytest.param("", id="empty-string"),
        pytest.param([], id="empty-list"),
    ],
)
def test_to_provider_skips_empty_reasoning_for_completely_empty_assistant(content: str | list[TextPart] | None) -> None:
    messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="System instruction"),
        InternalMessage(role=MessageRole.ASSISTANT, content=content),
        InternalMessage(role=MessageRole.USER, content="Question"),
        InternalMessage(role=MessageRole.TOOL, content="Tool result", tool_call_id="call_123"),
    ]

    assert DeepSeekResponsesTransformer.to_provider(messages, model_id="deepseek-model", channel_id=17) == [
        {"role": "system", "content": "System instruction"},
        {"role": "user", "content": "Question"},
        {"type": "function_call_output", "call_id": "call_123", "output": "Tool result"},
    ]


def test_to_provider_pairs_tool_only_assistant_with_tool_output_and_sanitizes_tool_metadata() -> None:
    assistant = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        provider_metadata={
            "protocol": "deepseek_responses",
            "source": _source("deepseek_responses"),
        },
        tool_calls=[
            InternalToolCall(
                id="call_123",
                name="lookup",
                arguments={"query": "weather", "limit": 2},
                provider_metadata={
                    "protocol": "deepseek_responses",
                    "item": {
                        "id": "fc_item_123",
                        "status": "completed",
                        "provider_extension": "discard",
                    },
                },
            )
        ],
    )
    tool_result = InternalMessage(role=MessageRole.TOOL, content="Found results", tool_call_id="call_123")
    original_messages = [message.model_dump(mode="python") for message in (assistant, tool_result)]

    provider_items = DeepSeekResponsesTransformer.to_provider([assistant, tool_result], model_id="deepseek-model", channel_id=17)

    assert provider_items == [
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "(thinking unavailable)"}]},
        {
            "type": "function_call",
            "id": "fc_item_123",
            "status": "completed",
            "call_id": "call_123",
            "name": "lookup",
            "arguments": '{"query":"weather","limit":2}',
        },
        {"type": "function_call_output", "call_id": "call_123", "output": "Found results"},
    ]
    assert all("role" not in item for item in provider_items)
    assert [message.model_dump(mode="python") for message in (assistant, tool_result)] == original_messages


@pytest.mark.parametrize(
    "native_fields",
    [
        pytest.param({}, id="missing-content"),
        pytest.param({"content": None}, id="none-content"),
        pytest.param({"content": []}, id="empty-content"),
        pytest.param({"content": [{"type": "reasoning_text", "text": ""}]}, id="empty-reasoning-text"),
        pytest.param({"content": [{"type": "reasoning_text", "text": " \t\n"}]}, id="whitespace-reasoning-text"),
        pytest.param({"content": [{"type": "summary_text", "text": "summary is not reasoning"}]}, id="summary-only"),
        pytest.param({"content": [{"type": "reasoning_text", "text": 123}]}, id="non-string-reasoning-text"),
        pytest.param({"content": {"type": "reasoning_text", "text": "not a list"}}, id="non-list-content"),
    ],
)
def test_to_provider_adds_placeholder_for_unavailable_native_reasoning_with_tool_call(
    native_fields: dict[str, object],
) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_unavailable",
        "status": "completed",
        "summary": [{"type": "summary_text", "text": "summary is not reasoning"}],
        "encrypted_content": "sealed reasoning",
        **native_fields,
    }
    assistant = InternalMessage(
        role=MessageRole.ASSISTANT,
        content=None,
        reasoning_content=None,
        provider_metadata={
            "protocol": "deepseek_responses",
            "source": _source("deepseek_responses"),
            "output": [reasoning_item],
        },
        tool_calls=[
            InternalToolCall(
                id="call_unavailable",
                name="lookup",
                arguments={"query": "weather"},
            )
        ],
    )
    tool_result = InternalMessage(role=MessageRole.TOOL, content="Found results", tool_call_id="call_unavailable")
    messages = [assistant, tool_result]
    original_messages = [message.model_dump(mode="python") for message in messages]
    expected_input = [
        {
            "type": "reasoning",
            "id": "rs_unavailable",
            "status": "completed",
            "content": [{"type": "reasoning_text", "text": "(thinking unavailable)"}],
        },
        {
            "type": "function_call",
            "call_id": "call_unavailable",
            "name": "lookup",
            "arguments": '{"query":"weather"}',
        },
        {"type": "function_call_output", "call_id": "call_unavailable", "output": "Found results"},
    ]

    provider_input = DeepSeekResponsesTransformer.to_provider(messages, model_id="deepseek-model", channel_id=17)
    token_payload = DeepSeekResponsesTransformer.build_input_token_payload(
        model_id="deepseek-model",
        channel_id=17,
        messages=messages,
        tools=None,
    )

    assert provider_input == expected_input
    assert token_payload["input"] == expected_input
    assert [message.model_dump(mode="python") for message in messages] == original_messages


@pytest.mark.parametrize(
    ("native_content", "internal_reasoning", "expected_content"),
    [
        pytest.param(
            [],
            " \nrecovered reasoning\t ",
            [{"type": "reasoning_text", "text": " \nrecovered reasoning\t "}],
            id="recover-flat-reasoning-verbatim",
        ),
        pytest.param(
            [{"type": "reasoning_text", "text": ""}],
            "recovered reasoning",
            [{"type": "reasoning_text", "text": "recovered reasoning"}],
            id="recover-when-native-text-is-empty",
        ),
        pytest.param(
            [{"type": "reasoning_text", "text": " \n原生真实思考\t "}],
            "different flattened reasoning",
            [{"type": "reasoning_text", "text": " \n原生真实思考\t "}],
            id="prefer-native-text-verbatim",
        ),
        pytest.param(
            [
                {"type": "reasoning_text", "text": "first"},
                {"type": "reasoning_text", "text": " second"},
            ],
            "combined or alternate",
            [
                {"type": "reasoning_text", "text": "first"},
                {"type": "reasoning_text", "text": " second"},
            ],
            id="prefer-native-multiple-blocks",
        ),
        pytest.param(
            [],
            "",
            [{"type": "reasoning_text", "text": "(thinking unavailable)"}],
            id="placeholder-with-empty-flat-reasoning",
        ),
        pytest.param(
            [],
            " \t\n",
            [{"type": "reasoning_text", "text": "(thinking unavailable)"}],
            id="placeholder-with-whitespace-flat-reasoning",
        ),
    ],
)
def test_to_provider_prefers_native_reasoning_over_flat_reasoning(
    native_content: list[dict[str, object]],
    internal_reasoning: str,
    expected_content: list[dict[str, object]],
) -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        reasoning_content=internal_reasoning,
        provider_metadata={
            "protocol": "deepseek_responses",
            "source": _source("deepseek_responses"),
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_priority",
                    "status": "completed",
                    "content": native_content,
                }
            ],
        },
    )
    original_message = message.model_dump(mode="python")

    assert DeepSeekResponsesTransformer.to_provider([message], model_id="deepseek-model", channel_id=17) == [
        {
            "type": "reasoning",
            "id": "rs_priority",
            "status": "completed",
            "content": expected_content,
        },
        {"role": "assistant", "content": "Answer"},
    ]
    assert message.model_dump(mode="python") == original_message


def test_to_provider_converts_text_and_image_parts_and_rejects_audio() -> None:
    message = InternalMessage(
        role=MessageRole.USER,
        content=[
            TextPart(text="Describe this"),
            ImagePart(image_url={"url": "https://example.invalid/image.png"}),
        ],
    )

    assert DeepSeekResponsesTransformer.to_provider([message]) == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Describe this"},
                {"type": "input_image", "image_url": "https://example.invalid/image.png"},
            ],
        }
    ]

    with pytest.raises(LLMException) as exc_info:
        DeepSeekResponsesTransformer.to_provider([InternalMessage(role=MessageRole.USER, content=[AudioPart(format="wav", data="YXVkaW8=")])])
    assert exc_info.value.message == ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED


def test_build_input_token_payload_matches_provider_input_and_converts_nested_tool_schema() -> None:
    messages = [
        InternalMessage(role=MessageRole.USER, content="Question"),
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content="Answer",
            provider_metadata={
                "protocol": "deepseek",
                "source": _source("deepseek"),
                "message": {"reasoning": "Prior reasoning"},
            },
        ),
    ]
    nested_schema = {
        "type": "object",
        "properties": {
            "filter": {
                "type": "object",
                "properties": {"tags": {"type": "array", "items": {"type": "string"}}},
                "required": ["tags"],
                "additionalProperties": False,
            }
        },
        "required": ["filter"],
        "additionalProperties": False,
    }
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Find matching records",
                "parameters": nested_schema,
                "strict": True,
            },
        },
        {"type": "web_search_preview"},
    ]
    expected_input = [
        {"role": "user", "content": "Question"},
        {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "Prior reasoning"}]},
        {"role": "assistant", "content": "Answer"},
    ]
    original_messages = [message.model_dump(mode="python") for message in messages]

    provider_input = DeepSeekResponsesTransformer.to_provider(messages, model_id="deepseek-model", channel_id=17)
    token_payload = DeepSeekResponsesTransformer.build_input_token_payload(
        model_id="deepseek-model",
        channel_id=17,
        messages=messages,
        tools=tools,
    )

    assert provider_input == expected_input
    assert token_payload == {
        "input": expected_input,
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "parameters": nested_schema,
                "description": "Find matching records",
                "strict": True,
            }
        ],
    }
    assert token_payload["input"] == provider_input
    assert [message.model_dump(mode="python") for message in messages] == original_messages


def test_to_internal_response_extracts_reasoning_tools_usage_and_provider_metadata() -> None:
    reasoning_items = [
        {
            "type": "reasoning",
            "id": "rs_1",
            "status": "completed",
            "content": [
                {"type": "reasoning_text", "text": "Analyze"},
                {"type": "reasoning_text", "text": " privately"},
                {"type": "summary_text", "text": "not plaintext"},
            ],
            "summary": [{"type": "summary_text", "text": "summary is metadata"}],
            "encrypted_content": "sealed",
            "provider_extension": {"nested": ["preserved"]},
        },
        {
            "type": "reasoning",
            "id": "rs_2",
            "content": [{"type": "reasoning_text", "text": "Second thought"}],
            "summary": [{"type": "summary_text", "text": "another summary"}],
        },
    ]
    provider_response = {
        "id": "resp_123",
        "status": "completed",
        "model": "deepseek-chat",
        "output": [
            *reasoning_items,
            {
                "type": "message",
                "content": [{"type": "output_text", "text": "Public answer"}],
            },
            {
                "type": "function_call",
                "id": "fc_1",
                "call_id": "call_1",
                "name": "lookup",
                "arguments": '{"query":"weather","limit":2}',
                "status": "completed",
                "provider_extension": {"source": "deepseek"},
            },
            {
                "type": "function_call",
                "id": "call_id_fallback",
                "name": "lookup_again",
                "arguments": "not-json",
            },
        ],
        "usage": {
            "input_tokens": 12,
            "output_tokens": 5,
            "input_tokens_details": {"cached_tokens": 3, "other": "retained"},
        },
    }

    response = DeepSeekResponsesTransformer.to_internal_response(provider_response, "fallback-model")

    assert response.model == "deepseek-chat"
    assert response.message.content == "Public answer"
    assert response.message.reasoning_content == "Analyze privately\n\nSecond thought"
    assert "not plaintext" not in response.message.reasoning_content
    assert "summary is metadata" not in response.message.reasoning_content
    assert "sealed" not in response.message.reasoning_content
    assert response.message.provider_metadata == {
        "protocol": "deepseek_responses",
        "output": reasoning_items,
    }
    provider_response["output"][0]["provider_extension"]["nested"][0] = "mutation"
    assert response.message.provider_metadata["output"][0]["provider_extension"] == {"nested": ["preserved"]}
    assert [(call.id, call.name, call.arguments) for call in response.message.tool_calls] == [
        ("call_1", "lookup", {"query": "weather", "limit": 2}),
        ("call_id_fallback", "lookup_again", {}),
    ]
    assert response.message.tool_calls[0].provider_metadata == {
        "protocol": "deepseek_responses",
        "item": {
            "type": "function_call",
            "id": "fc_1",
            "status": "completed",
            "provider_extension": {"source": "deepseek"},
        },
    }
    assert response.usage == {
        "input_tokens": 12,
        "output_tokens": 5,
        "input_tokens_details": {"cached_tokens": 3, "other": "retained"},
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
        "cached_tokens": 3,
    }
    assert response.finish_reason == "tool_calls"
    assert response.finish_details == {"status": "completed"}


@pytest.mark.parametrize(
    ("reasoning_content", "expected_reasoning"),
    [(None, None), ("", "")],
    ids=["missing-plaintext", "empty-plaintext"],
)
def test_to_internal_response_keeps_reasoning_separate_from_public_text(reasoning_content: str | None, expected_reasoning: str | None) -> None:
    content = [{"type": "summary_text", "text": "summary only"}] if reasoning_content is None else [{"type": "reasoning_text", "text": reasoning_content}]
    response = DeepSeekResponsesTransformer.to_internal_response(
        {
            "status": "completed",
            "output": [
                {"type": "reasoning", "content": content, "encrypted_content": "sealed"},
                {"type": "message", "content": [{"type": "output_text", "text": "Public"}]},
            ],
        },
        "fallback-model",
    )

    assert response.message.content == "Public"
    assert response.message.reasoning_content == expected_reasoning


@pytest.mark.parametrize(
    ("message_content", "expected_content"),
    [(None, "Declined"), ("Available answer", "Available answer")],
    ids=["refusal-is-body", "text-remains-body"],
)
def test_to_internal_response_maps_refusal_without_mixing_public_text(message_content: str | None, expected_content: str) -> None:
    parts = [] if message_content is None else [{"type": "output_text", "text": message_content}]
    parts.append({"type": "refusal", "refusal": "Declined"})

    response = DeepSeekResponsesTransformer.to_internal_response(
        {"status": "completed", "output": [{"type": "message", "content": parts}]},
        "fallback-model",
    )

    assert response.message.content == expected_content
    assert response.message.refusal == "Declined"
    assert response.finish_reason == "refusal"


@pytest.mark.parametrize(
    ("reason", "expected_finish_reason"),
    [
        ("max_output_tokens", "length"),
        ("content_filter", "content_filter"),
        ("safety_violation", "content_filter"),
        ("other_limit", "incomplete"),
    ],
)
def test_to_internal_response_maps_incomplete_status_and_preserves_details(reason: str, expected_finish_reason: str) -> None:
    details = {"reason": reason, "provider_extension": {"retryable": False}}

    response = DeepSeekResponsesTransformer.to_internal_response({"status": "incomplete", "incomplete_details": details, "output": []}, "fallback-model")

    assert response.message.content is None
    assert response.finish_reason == expected_finish_reason
    assert response.finish_details == {"status": "incomplete", "incomplete_details": details}


def test_to_internal_response_normalizes_invalid_usage_counts() -> None:
    response = DeepSeekResponsesTransformer.to_internal_response(
        {
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {
                "input_tokens": True,
                "output_tokens": -4,
                "total_tokens": True,
                "input_tokens_details": {"cached_tokens": -2},
            },
        },
        "fallback-model",
    )

    assert response.usage["prompt_tokens"] == 0
    assert response.usage["completion_tokens"] == 0
    assert response.usage["total_tokens"] == 0
    assert response.usage["cached_tokens"] == 0


def test_to_internal_response_rejects_empty_completed_output() -> None:
    with pytest.raises(LLMException) as exc_info:
        DeepSeekResponsesTransformer.to_internal_response({"status": "completed", "output": []}, "fallback-model")

    assert exc_info.value.message == ERR_LLM_EMPTY_RESPONSE


def test_to_internal_response_classifies_provider_errors_by_error_code() -> None:
    with pytest.raises(LLMException) as generic_error:
        DeepSeekResponsesTransformer.to_internal_response(
            {
                "status": "failed",
                "error": {"code": "server_error", "message": "context_length_exceeded"},
            },
            "fallback-model",
        )
    assert type(generic_error.value) is LLMException
    assert generic_error.value.message == ERR_LLM_API_RESPONSE_ERROR

    with pytest.raises(LLMContextLengthException) as context_error:
        DeepSeekResponsesTransformer.to_internal_response(
            {"status": "failed", "error": {"code": "context_length_exceeded", "message": "limit reached"}},
            "fallback-model",
        )
    assert context_error.value.provider_message == "limit reached"


@pytest.mark.asyncio
async def test_generate_maps_responses_request_and_nested_function_tool_choice(monkeypatch) -> None:
    captured_request: dict[str, object] = {}
    provider_response = {
        "id": "resp_generate",
        "status": "completed",
        "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
    }

    async def post_json(**request):
        captured_request.update(request)
        return provider_response

    monkeypatch.setattr(http_transport, "post_json", post_json)
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Find matching records",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                "strict": True,
            },
        }
    ]

    result = await DeepSeekResponsesTransformer().generate(
        api_key="test-key",
        base_url="https://api.example/v1/",
        model_id="deepseek-chat",
        messages=[InternalMessage(role=MessageRole.USER, content="Find records")],
        temperature=0.25,
        max_tokens=256,
        tools=tools,
        tool_choice={"type": "function", "function": {"name": "lookup"}},
        timeout=12.5,
        http_proxy="http://proxy.example:8080",
        custom_headers={"X-Trace-Id": "trace-123"},
        top_p=0.6,
        reasoning_effort=" high ",
    )

    assert result == provider_response
    assert captured_request["url"] == "https://api.example/v1/responses"
    assert captured_request["headers"] == {
        "Authorization": "Bearer test-key",
        "Content-Type": "application/json",
        "x-trace-id": "trace-123",
    }
    assert captured_request["timeout"] == 12.5
    assert captured_request["http_proxy"] == "http://proxy.example:8080"
    assert captured_request["payload"] == {
        "model": "deepseek-chat",
        "input": [{"role": "user", "content": "Find records"}],
        "stream": False,
        "temperature": 0.25,
        "top_p": 0.6,
        "reasoning": {"effort": "high"},
        "max_output_tokens": 256,
        "tools": [
            {
                "type": "function",
                "name": "lookup",
                "parameters": {"type": "object", "properties": {"query": {"type": "string"}}},
                "description": "Find matching records",
                "strict": True,
            }
        ],
        "tool_choice": {"type": "function", "name": "lookup"},
    }
    assert {"store", "include", "previous_response_id"}.isdisjoint(captured_request["payload"])


@pytest.mark.asyncio
async def test_generate_omits_unspecified_options_and_disables_missing_tools(monkeypatch) -> None:
    captured_request: dict[str, object] = {}

    async def post_json(**request):
        captured_request.update(request)
        return {"status": "completed"}

    monkeypatch.setattr(http_transport, "post_json", post_json)

    await DeepSeekResponsesTransformer().generate(
        api_key="test-key",
        base_url="https://api.example/v1",
        model_id="deepseek-chat",
        messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        temperature=None,
        max_tokens=0,
    )

    payload = captured_request["payload"]
    assert payload == {
        "model": "deepseek-chat",
        "input": [{"role": "user", "content": "Question"}],
        "stream": False,
        "tool_choice": "none",
    }
    assert "temperature" not in payload
    assert "top_p" not in payload
    assert "reasoning" not in payload
    assert "max_output_tokens" not in payload


@pytest.fixture
def response_stream(monkeypatch):
    def install(events):
        consumed = []

        async def stream_sse_json(**kwargs):
            for event in events:
                consumed.append(event)
                normalized, _ = kwargs["normalize_event"](event)
                terminal = kwargs["is_terminal"](event)
                if normalized is not None:
                    yield normalized
                if terminal:
                    break

        monkeypatch.setattr(http_transport, "stream_sse_json", stream_sse_json)
        return consumed

    return install


async def _collect_stream():
    return [
        chunk
        async for chunk in DeepSeekResponsesTransformer().generate_stream(
            api_key="test-key",
            base_url="https://api.example/v1",
            model_id="deepseek-chat",
            messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        )
    ]


@pytest.mark.asyncio
async def test_generate_stream_deduplicates_text_and_reasoning_snapshots(response_stream) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_final",
        "status": "completed",
        "content": [{"type": "reasoning_text", "text": "private reasoning"}],
        "summary": [{"type": "summary_text", "text": "summary"}],
        "encrypted_content": "sealed",
        "provider_extension": {"preserved": True},
    }
    message_item = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Public answer"}],
    }
    events = [
        {"type": "response.reasoning_text.delta", "output_index": 0, "content_index": 0, "delta": "private reasoning"},
        {"type": "response.reasoning_text.done", "output_index": 0, "content_index": 0, "text": "private reasoning"},
        {"type": "response.output_text.delta", "output_index": 1, "content_index": 0, "delta": "Public answer"},
        {"type": "response.output_text.done", "output_index": 1, "content_index": 0, "text": "Public answer"},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning_item},
        {"type": "response.output_item.done", "output_index": 1, "item": message_item},
        {"type": "response.completed", "response": {"status": "completed", "output": [reasoning_item, message_item]}},
    ]
    response_stream(events)

    chunks = await _collect_stream()

    deltas = [chunk["choices"][0]["delta"] for chunk in chunks]
    assert "".join(delta.get("content", "") for delta in deltas) == "Public answer"
    assert "".join(delta.get("reasoning_content", "") for delta in deltas) == "private reasoning"
    assert chunks[-1]["message_provider_metadata"] == {
        "protocol": "deepseek_responses",
        "output": [reasoning_item],
    }


@pytest.mark.asyncio
async def test_generate_stream_keeps_reasoning_item_snapshot_without_terminal_output(response_stream) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_only_done",
        "status": "completed",
        "content": [{"type": "reasoning_text", "text": "done-only reasoning"}],
        "provider_extension": {"retained": "as-is"},
    }
    events = [
        {"type": "response.created", "response": {"id": "resp_created"}},
        {"type": "response.output_item.added", "output_index": 0, "item": {"type": "message", "role": "assistant", "content": []}},
        {"type": "response.output_text.delta", "output_index": 0, "delta": ""},
        {"type": "response.output_item.done", "output_index": 0, "item": reasoning_item},
        {"type": "response.completed", "response": {"status": "completed"}},
        {"type": "response.output_text.delta", "output_index": 1, "delta": "after terminal"},
    ]
    consumed = response_stream(events)

    chunks = await _collect_stream()

    assert consumed == events[:-1]
    assert len(chunks) == 2
    assert chunks[0]["choices"][0]["delta"] == {"reasoning_content": "done-only reasoning"}
    assert chunks[-1]["choices"][0]["finish_reason"] == "stop"
    assert chunks[-1]["message_provider_metadata"] == {
        "protocol": "deepseek_responses",
        "output": [reasoning_item],
    }


@pytest.mark.asyncio
async def test_generate_stream_associates_parallel_function_calls_and_appends_argument_snapshots(response_stream) -> None:
    first_item = {
        "type": "function_call",
        "id": "fc_item_weather",
        "call_id": "call_weather",
        "name": "weather",
        "arguments": '{"city":"Paris"}',
        "status": "completed",
    }
    second_item = {
        "type": "function_call",
        "id": "fc_item_units",
        "call_id": "call_units",
        "name": "units",
        "arguments": '{"units":"celsius"}',
        "status": "completed",
    }
    events = [
        {"type": "response.output_item.added", "output_index": 0, "item": {**first_item, "arguments": ""}},
        {"type": "response.output_item.added", "output_index": 1, "item": {**second_item, "arguments": ""}},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_item_weather", "delta": '{"city":'},
        {"type": "response.function_call_arguments.delta", "item_id": "fc_item_units", "delta": '{"units":'},
        {"type": "response.function_call_arguments.done", "item_id": "fc_item_units", "arguments": second_item["arguments"]},
        {"type": "response.function_call_arguments.done", "item_id": "fc_item_weather", "arguments": first_item["arguments"]},
        {"type": "response.output_item.done", "item": first_item},
        {"type": "response.output_item.done", "item": second_item},
        {"type": "response.completed", "response": {"status": "completed", "output": [first_item, second_item]}},
    ]
    response_stream(events)

    chunks = await _collect_stream()

    argument_parts = {0: "", 1: ""}
    call_ids = {}
    for chunk in chunks:
        for tool_delta in chunk["choices"][0]["delta"].get("tool_calls", []):
            index = tool_delta["index"]
            if tool_delta.get("id"):
                call_ids[index] = tool_delta["id"]
            function = tool_delta.get("function", {})
            argument_parts[index] += function.get("arguments", "")

    assert call_ids == {0: "call_weather", 1: "call_units"}
    assert json.loads(argument_parts[0]) == {"city": "Paris"}
    assert json.loads(argument_parts[1]) == {"units": "celsius"}


@pytest.mark.asyncio
async def test_generate_stream_backfills_terminal_snapshot_and_delivers_incomplete_usage(response_stream) -> None:
    reasoning_item = {
        "type": "reasoning",
        "id": "rs_terminal",
        "content": [{"type": "reasoning_text", "text": "snapshot reasoning"}],
        "provider_extension": "preserved",
    }
    message_item = {"type": "message", "content": [{"type": "output_text", "text": "snapshot answer"}]}
    tool_item = {
        "type": "function_call",
        "id": "fc_terminal",
        "call_id": "call_terminal",
        "name": "lookup",
        "arguments": '{"key":"value"}',
    }
    response_stream(
        [
            {
                "type": "response.completed",
                "response": {"status": "completed", "output": [reasoning_item, message_item, tool_item]},
            }
        ]
    )

    completed_chunks = await _collect_stream()
    completed_delta = completed_chunks[-1]["choices"][0]["delta"]
    assert len(completed_chunks) == 1
    assert completed_delta["content"] == "snapshot answer"
    assert completed_delta["reasoning_content"] == "snapshot reasoning"
    assert completed_delta["tool_calls"][0]["id"] == "call_terminal"
    assert completed_delta["tool_calls"][0]["function"] == {"name": "lookup", "arguments": '{"key":"value"}'}
    assert completed_chunks[-1]["message_provider_metadata"] == {
        "protocol": "deepseek_responses",
        "output": [reasoning_item],
    }

    response_stream(
        [
            {
                "type": "response.incomplete",
                "response": {
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "partial"}]}],
                    "usage": {"input_tokens": 4, "output_tokens": 9},
                },
            }
        ]
    )

    incomplete_chunks = await _collect_stream()

    assert len(incomplete_chunks) == 1
    assert incomplete_chunks[0]["choices"][0]["finish_reason"] == "length"
    assert incomplete_chunks[0]["choices"][0]["delta"]["content"] == "partial"
    assert incomplete_chunks[0]["usage"]["prompt_tokens"] == 4
    assert incomplete_chunks[0]["usage"]["completion_tokens"] == 9
    assert incomplete_chunks[0]["usage"]["total_tokens"] == 13


@pytest.mark.parametrize(
    ("event", "expected_type", "provider_message"),
    [
        pytest.param(
            {"type": "response.failed", "response": {"status": "failed", "error": {"code": "context_length_exceeded", "message": "nested context limit"}}},
            LLMContextLengthException,
            "nested context limit",
            id="failed-nested-context-error",
        ),
        pytest.param(
            {"type": "error", "error": {"code": "context_length_exceeded", "message": "stream context limit"}},
            LLMContextLengthException,
            "stream context limit",
            id="error-context-code",
        ),
        pytest.param(
            {"type": "error", "error": {"code": "server_error", "message": "context_length_exceeded"}},
            LLMException,
            None,
            id="ordinary-error-is-not-context-error",
        ),
    ],
)
@pytest.mark.asyncio
async def test_generate_stream_classifies_provider_errors(response_stream, event, expected_type, provider_message) -> None:
    response_stream([event])

    with pytest.raises(expected_type) as exc_info:
        await _collect_stream()

    if expected_type is LLMContextLengthException:
        assert exc_info.value.provider_message == provider_message
    else:
        assert type(exc_info.value) is LLMException
        assert exc_info.value.message == ERR_LLM_API_RESPONSE_ERROR
