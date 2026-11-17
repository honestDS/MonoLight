import pytest

from app.core.constants import (
    ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED,
)
from app.core.exceptions import LLMException
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
