import json
from typing import Any

import pytest

from app.core.constants import ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED
from app.core.exceptions import LLMContextLengthException, LLMException, LLMRequestRejectedException
from app.models.message import AudioPart, ImagePart, InternalMessage, InternalToolCall, MessageRole, TextPart
from app.transformers import http_transport
from app.transformers.deepseek.chat_completions import DeepSeekChatCompletionsTransformer


def _source(protocol: str = "deepseek", *, channel_id: int = 17, model_id: str = "deepseek-r1") -> dict[str, Any]:
    return {"channel_id": channel_id, "model_id": model_id, "protocol": protocol}


def test_to_provider_preserves_real_reasoning_defaults_missing_and_skips_only_empty_assistants() -> None:
    source = _source()
    messages = [
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content="Answer with empty reasoning",
            reasoning_content="",
            provider_metadata={"source": source},
        ),
        InternalMessage(role=MessageRole.ASSISTANT, content="Answer", reasoning_content="Think", provider_metadata={"source": source}),
        InternalMessage(role=MessageRole.ASSISTANT, content="No recorded reasoning"),
        InternalMessage(role=MessageRole.ASSISTANT),
        InternalMessage(role=MessageRole.USER, content="Question", reasoning_content="private"),
    ]
    original_messages = [message.model_dump(mode="python") for message in messages]

    provider_messages = DeepSeekChatCompletionsTransformer.to_provider(
        messages,
        model_id="deepseek-r1",
        channel_id=17,
    )

    assert provider_messages == [
        {"role": "assistant", "content": "Answer with empty reasoning", "reasoning_content": ""},
        {"role": "assistant", "content": "Answer", "reasoning_content": "Think"},
        {"role": "assistant", "content": "No recorded reasoning", "reasoning_content": ""},
        {"role": "user", "content": "Question"},
    ]
    assert [message.model_dump(mode="python") for message in messages] == original_messages


def test_to_provider_keeps_tool_only_assistant_and_matching_tool_result_without_placeholder() -> None:
    arguments = {"query": "value", "limit": 3}
    messages = [
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content=None,
            tool_calls=[
                InternalToolCall(
                    id="call_1",
                    name="lookup",
                    arguments=arguments,
                    provider_metadata={"keep": "tool metadata"},
                )
            ],
        ),
        InternalMessage(role=MessageRole.TOOL, content="found", tool_call_id="call_1"),
    ]
    original_messages = [message.model_dump(mode="python") for message in messages]

    provider_messages = DeepSeekChatCompletionsTransformer.to_provider(messages)

    assert provider_messages[0]["content"] is None
    tool_call = provider_messages[0]["tool_calls"][0]
    assert tool_call["id"] == "call_1"
    assert tool_call["function"]["name"] == "lookup"
    assert json.loads(tool_call["function"]["arguments"]) == arguments
    assert provider_messages[1] == {"role": "tool", "content": "found", "tool_call_id": "call_1"}
    assert "[tool_call]" not in json.dumps(provider_messages)
    assert [message.model_dump(mode="python") for message in messages] == original_messages


@pytest.mark.parametrize(
    ("source", "expected_reasoning"),
    [
        (_source(), "private reasoning"),
        (_source(channel_id=18), ""),
        (_source(model_id="other-model"), ""),
        (_source("unknown"), ""),
        (None, ""),
    ],
    ids=["same-source", "other-channel", "other-model", "unknown-protocol", "missing-source"],
)
def test_to_provider_scopes_reasoning_without_mutating_message_or_tool_metadata(source, expected_reasoning) -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Visible answer",
        reasoning_content="private reasoning",
        provider_metadata={"source": source, "message": {"reasoning": "metadata reasoning"}},
        tool_calls=[
            InternalToolCall(
                id="call_1",
                name="lookup",
                arguments={"query": "value"},
                provider_metadata={"preserve": [1, 2]},
            )
        ],
    )
    original_message = message.model_dump(mode="python")

    provider_message = DeepSeekChatCompletionsTransformer.to_provider(
        [message],
        model_id="deepseek-r1",
        channel_id=17,
    )[0]

    assert provider_message["reasoning_content"] == expected_reasoning
    assert message.model_dump(mode="python") == original_message


def test_to_provider_restores_legacy_openai_chat_reasoning() -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        provider_metadata={
            "source": _source("openai"),
            "protocol": "openai_chat_completions",
            "message": {"reasoning": "legacy plaintext"},
        },
    )

    provider_message = DeepSeekChatCompletionsTransformer.to_provider(
        [message],
        model_id="deepseek-r1",
        channel_id=17,
    )[0]

    assert provider_message["reasoning_content"] == "legacy plaintext"


@pytest.mark.parametrize("reasoning_text", ["plain response reasoning", ""])
def test_to_provider_restores_deepseek_responses_plaintext_including_empty_text(reasoning_text) -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        provider_metadata={
            "source": _source("deepseek_responses"),
            "output": [
                {
                    "type": "reasoning",
                    "content": [{"type": "reasoning_text", "text": reasoning_text}],
                }
            ],
        },
    )

    provider_message = DeepSeekChatCompletionsTransformer.to_provider(
        [message],
        model_id="deepseek-r1",
        channel_id=17,
    )[0]

    assert provider_message["reasoning_content"] == reasoning_text


def test_to_provider_does_not_restore_openai_responses_summary_or_encrypted_reasoning() -> None:
    message = InternalMessage(
        role=MessageRole.ASSISTANT,
        content="Answer",
        reasoning_content="summary must not be reused",
        provider_metadata={
            "source": _source("openai_responses"),
            "output": [
                {
                    "type": "reasoning",
                    "encrypted_content": "sealed",
                    "summary": [{"type": "summary_text", "text": "summary must not be reused"}],
                }
            ],
        },
    )

    provider_message = DeepSeekChatCompletionsTransformer.to_provider(
        [message],
        model_id="deepseek-r1",
        channel_id=17,
    )[0]

    assert provider_message["reasoning_content"] == ""


@pytest.mark.parametrize(
    ("provider_message", "expected_reasoning"),
    [
        ({"role": "assistant", "content": "Answer"}, None),
        ({"role": "assistant", "content": "Answer", "reasoning_content": ""}, ""),
    ],
    ids=["missing-reasoning", "empty-reasoning"],
)
def test_from_provider_preserves_body_and_missing_or_empty_reasoning(provider_message, expected_reasoning) -> None:
    message = DeepSeekChatCompletionsTransformer.from_provider({"choices": [{"message": provider_message}]})

    assert message.role == MessageRole.ASSISTANT
    assert message.content == "Answer"
    assert message.reasoning_content == expected_reasoning


def test_from_provider_parses_function_json_and_uses_empty_object_for_invalid_json() -> None:
    message = DeepSeekChatCompletionsTransformer.from_provider(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Searching",
                        "reasoning_content": "Need a lookup",
                        "tool_calls": [
                            {
                                "id": "call_valid",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"query":"value"}'},
                            },
                            {
                                "id": "call_invalid",
                                "type": "function",
                                "function": {"name": "fallback", "arguments": "not-json"},
                            },
                        ],
                    }
                }
            ]
        }
    )

    assert message.content == "Searching"
    assert message.reasoning_content == "Need a lookup"
    assert [(call.id, call.name, call.arguments) for call in message.tool_calls] == [
        ("call_valid", "lookup", {"query": "value"}),
        ("call_invalid", "fallback", {}),
    ]


@pytest.mark.parametrize(
    ("finish_reason", "expected_finish_reason"),
    [
        ("stop", "stop"),
        ("tool_calls", "tool_calls"),
        ("length", "length"),
        ("aborted", "incomplete"),
        ("insufficient_system_resource", "incomplete"),
    ],
)
@pytest.mark.parametrize(
    ("cache_field", "cache_value"),
    [
        ("prompt_tokens_details", {"cached_tokens": 4}),
        ("prompt_cache_hit_tokens", 4),
    ],
    ids=["prompt-token-details", "prompt-cache-hit"],
)
def test_to_internal_response_normalizes_finish_reasons_and_cached_usage(
    finish_reason,
    expected_finish_reason,
    cache_field,
    cache_value,
) -> None:
    usage = {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17, cache_field: cache_value}
    response = DeepSeekChatCompletionsTransformer.to_internal_response(
        {
            "model": "deepseek-r1",
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": "Answer",
                        "reasoning_content": "Reasoning",
                    },
                }
            ],
            "usage": usage,
        },
        default_model="fallback",
    )

    assert response.message.content == "Answer"
    assert response.message.reasoning_content == "Reasoning"
    assert response.finish_reason == expected_finish_reason
    assert response.finish_details == {"raw_finish_reason": finish_reason}
    assert {key: response.usage[key] for key in ("prompt_tokens", "completion_tokens", "total_tokens", "cached_tokens")} == {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17, "cached_tokens": 4}


def test_raise_error_classifies_explicit_context_code_and_does_not_guess_from_message() -> None:
    transformer = DeepSeekChatCompletionsTransformer()
    with pytest.raises(LLMContextLengthException):
        transformer.raise_error(
            {"error": {"code": "context_length_exceeded", "message": "context is full"}},
            status=400,
        )

    with pytest.raises(LLMException) as ordinary_error:
        transformer.raise_error(
            {"error": {"code": "invalid_request", "message": "context length exceeded"}},
            status=400,
        )

    assert not isinstance(ordinary_error.value, LLMContextLengthException)
    assert ordinary_error.value.kwargs["status"] == 400


def test_raise_error_prioritizes_upstream_413_over_context_classification() -> None:
    with pytest.raises(LLMRequestRejectedException) as exc_info:
        DeepSeekChatCompletionsTransformer().raise_error(
            {"error": {"code": "context_length_exceeded", "message": "too long"}},
            status=413,
        )

    assert not isinstance(exc_info.value, LLMContextLengthException)
    assert exc_info.value.upstream_status == 413


@pytest.mark.asyncio
async def test_generate_raises_for_error_object_in_successful_http_response(monkeypatch) -> None:
    async def post_json(**_kwargs):
        return {"error": {"code": "server_error", "message": "provider failed"}}

    monkeypatch.setattr(http_transport, "post_json", post_json)

    with pytest.raises(LLMException):
        await DeepSeekChatCompletionsTransformer().generate(
            api_key="key",
            base_url="https://example.invalid/v1",
            model_id="deepseek-r1",
            messages=[InternalMessage(role=MessageRole.USER, content="Question")],
        )


def test_to_provider_accepts_text_and_images_but_rejects_audio() -> None:
    message = InternalMessage(
        role=MessageRole.USER,
        content=[
            TextPart(text="Describe this"),
            ImagePart(image_url={"url": "https://example.invalid/image.png"}),
        ],
    )

    provider_message = DeepSeekChatCompletionsTransformer.to_provider([message])[0]

    assert provider_message["content"] == [
        {"type": "text", "text": "Describe this"},
        {"type": "image_url", "image_url": {"url": "https://example.invalid/image.png"}},
    ]
    with pytest.raises(LLMException) as exc_info:
        DeepSeekChatCompletionsTransformer.to_provider([InternalMessage(role=MessageRole.USER, content=[AudioPart(format="wav", data="YXVkaW8=")])])
    assert exc_info.value.message == ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED


@pytest.mark.parametrize(
    ("reasoning_effort", "expected_thinking", "expected_effort"),
    [
        ("none", {"type": "disabled"}, None),
        ("high", {"type": "enabled"}, "high"),
        (None, None, None),
    ],
    ids=["disabled", "configured", "unspecified"],
)
@pytest.mark.asyncio
async def test_generate_sends_request_settings_and_input_token_messages_unchanged(
    monkeypatch,
    reasoning_effort,
    expected_thinking,
    expected_effort,
) -> None:
    model_id = "deepseek-r1"
    channel_id = 17
    tools = [
        {
            "type": "function",
            "function": {"name": "lookup", "parameters": {"type": "object"}},
        }
    ]
    messages = [
        InternalMessage(role=MessageRole.USER, content="Question"),
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content="Let me think",
            reasoning_content="prior reasoning",
            provider_metadata={"source": _source()},
        ),
    ]
    original_messages = [message.model_dump(mode="python") for message in messages]
    response = {
        "model": model_id,
        "choices": [{"message": {"role": "assistant", "content": "Answer"}, "finish_reason": "stop"}],
    }
    calls: list[dict[str, Any]] = []

    async def post_json(**kwargs):
        calls.append(kwargs)
        return response

    monkeypatch.setattr(http_transport, "post_json", post_json)
    transformer = DeepSeekChatCompletionsTransformer()
    result = await transformer.generate(
        api_key="secret",
        base_url="https://example.invalid/v1/",
        model_id=model_id,
        messages=messages,
        temperature=0.25,
        top_p=0.8,
        max_tokens=321,
        tools=tools,
        tool_choice="required",
        timeout=12.5,
        http_proxy="http://proxy.example:8080",
        custom_headers={"X-Region": "west"},
        channel_id=channel_id,
        reasoning_effort=reasoning_effort,
    )

    assert result == response
    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://example.invalid/v1/chat/completions"
    assert call["headers"]["Authorization"] == "Bearer secret"
    assert call["headers"]["x-region"] == "west"
    assert call["timeout"] == 12.5
    assert call["http_proxy"] == "http://proxy.example:8080"
    payload = call["payload"]
    assert payload["model"] == model_id
    assert payload["stream"] is False
    assert "stream_options" not in payload
    assert payload["temperature"] == 0.25
    assert payload["top_p"] == 0.8
    assert payload["max_tokens"] == 321
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "required"
    if expected_thinking is None:
        assert "thinking" not in payload
        assert "reasoning_effort" not in payload
    else:
        assert payload["thinking"] == expected_thinking
        if expected_effort is None:
            assert "reasoning_effort" not in payload
        else:
            assert payload["reasoning_effort"] == expected_effort

    input_token_payload = transformer.build_input_token_payload(
        model_id=model_id,
        channel_id=channel_id,
        messages=messages,
        tools=tools,
    )
    assert input_token_payload == {"messages": payload["messages"], "tools": payload["tools"]}
    assert [message.model_dump(mode="python") for message in messages] == original_messages


@pytest.mark.asyncio
async def test_generate_stream_normalizes_reasoning_content_tools_and_terminal_usage(monkeypatch) -> None:
    events = [
        {"choices": [{"delta": {"role": "assistant"}}]},
        {"choices": [], "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1}},
        {"choices": [{"delta": {}}]},
        {"choices": [{"delta": {"content": ""}}]},
        {"choices": [{"delta": {"reasoning_content": ""}}]},
        {"choices": [{"delta": {"reasoning_content": "先"}}]},
        {"choices": [{"delta": {"reasoning": "思"}}]},
        {"choices": [{"delta": {"content": "Answer"}}]},
        {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": '{"query":"value"}'}}]}}]},
        {
            "choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}],
            "usage": {
                "prompt_tokens": 8,
                "completion_tokens": 6,
                "total_tokens": 14,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
        },
    ]
    calls: list[dict[str, Any]] = []
    output_flags: list[bool] = []

    async def stream_sse_json(**kwargs):
        calls.append(kwargs)
        for event in events:
            terminal = kwargs["is_terminal"](event)
            normalized, has_output = kwargs["normalize_event"](event)
            output_flags.append(has_output)
            if normalized is not None:
                yield normalized
            if terminal:
                break

    monkeypatch.setattr(http_transport, "stream_sse_json", stream_sse_json)
    messages = [InternalMessage(role=MessageRole.USER, content="Question")]
    original_messages = [message.model_dump(mode="python") for message in messages]

    chunks = [
        chunk
        async for chunk in DeepSeekChatCompletionsTransformer().generate_stream(
            api_key="secret",
            base_url="https://example.invalid/v1/",
            model_id="deepseek-r1",
            messages=messages,
            timeout=9,
            http_proxy="http://proxy.example:8080",
            custom_headers={"X-Region": "west"},
        )
    ]

    assert len(calls) == 1
    call = calls[0]
    assert call["url"] == "https://example.invalid/v1/chat/completions"
    assert call["headers"]["x-region"] == "west"
    assert call["timeout"] == 9
    assert call["http_proxy"] == "http://proxy.example:8080"
    assert call["payload"]["stream"] is True
    assert call["payload"]["stream_options"] == {"include_usage": True}
    assert output_flags == [False, False, False, False, False, True, True, True, True, False]
    assert chunks[4]["message_provider_metadata"]["message"]["reasoning_content"] == ""
    assert chunks[5]["choices"][0]["delta"]["reasoning_content"] == "先"
    assert chunks[6]["choices"][0]["delta"]["reasoning_content"] == "思"
    assert chunks[7]["choices"][0]["delta"]["content"] == "Answer"
    assert chunks[8]["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"] == '{"query":"value"}'
    terminal = chunks[-1]
    assert terminal["choices"][0]["finish_reason"] == "tool_calls"
    assert terminal["usage"]["cached_tokens"] == 2
    assert terminal["message_provider_metadata"]["message"]["reasoning_content"] == "先思"
    assert [message.model_dump(mode="python") for message in messages] == original_messages
