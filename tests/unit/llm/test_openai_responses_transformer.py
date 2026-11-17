import json
import typing
from typing import Any

import pytest

from app.core.constants import ERR_LLM_CONNECTION_FAILED, ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH, ERR_LLM_EMPTY_RESPONSE
from app.core.exceptions import LLMContextLengthException, LLMException, LLMRequestRejectedException
from app.models.message import (
    AudioPart,
    FilePart,
    ImagePart,
    InternalMessage,
    InternalToolCall,
    MessageRole,
    TextPart,
)
from app.transformers.base import BaseTransformer
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer
from app.transformers.openai.base import BaseOpenAITransformer
from tests.unit.llm.openai_transformer_support import _mock_llm_request_max_bytes as _mock_llm_request_max_bytes


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
        "prompt_tokens": None,
        "completion_tokens": None,
        "total_tokens": None,
        "cached_tokens": None,
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


def test_openai_transformers_share_base_without_inheriting_each_other() -> None:
    assert issubclass(OpenAIChatCompletionsTransformer, BaseOpenAITransformer)
    assert issubclass(OpenAIResponsesTransformer, BaseOpenAITransformer)
    assert not issubclass(OpenAIResponsesTransformer, OpenAIChatCompletionsTransformer)


def test_base_transformer_generate_returns_provider_response() -> None:
    assert typing.get_type_hints(BaseTransformer.generate)["return"] is Any


def test_context_length_exception_remains_llm_exception_for_channel_fallback() -> None:
    assert issubclass(LLMContextLengthException, LLMException)


def test_chat_completions_classifies_only_explicit_context_length_code() -> None:
    with pytest.raises(LLMContextLengthException) as exc_info:
        OpenAIChatCompletionsTransformer._raise_provider_error(
            json.dumps(
                {
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "maximum context length is 8192 tokens",
                    }
                }
            ),
            status=400,
        )

    assert exc_info.value.message == ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH
    assert exc_info.value.code == 400
    assert exc_info.value.provider_message == "maximum context length is 8192 tokens"
    assert exc_info.value.kwargs["status"] == 400

    with pytest.raises(LLMException) as ordinary_error:
        OpenAIChatCompletionsTransformer._raise_provider_error(
            {
                "error": {
                    "code": "invalid_request_error",
                    "message": "maximum context length appeared only in text",
                }
            },
            status=400,
        )

    assert not isinstance(ordinary_error.value, LLMContextLengthException)


@pytest.mark.parametrize("transformer_cls", (OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer))
@pytest.mark.parametrize(
    "provider_error",
    (
        "<html>private body</html>",
        {"error": {"code": "context_length_exceeded", "message": "private provider detail"}},
    ),
)
def test_openai_transformers_prioritize_upstream_413_rejection(transformer_cls, provider_error) -> None:
    with pytest.raises(LLMRequestRejectedException) as exc_info:
        transformer_cls._raise_provider_error(provider_error, status=413)

    rejection = exc_info.value
    assert not isinstance(rejection, LLMContextLengthException)
    assert rejection.source == "upstream"
    assert rejection.upstream_status == 413
    assert rejection.code == 502
    assert rejection.upstream_detail == provider_error
    rendered = rejection.render_message()
    assert "413" in rendered
    assert "全局请求大小上限" in rendered
    assert "<html>private body</html>" not in rendered
    assert "private provider detail" not in rendered


def test_responses_classifies_nested_context_length_error_and_preserves_other_errors() -> None:
    with pytest.raises(LLMContextLengthException) as exc_info:
        OpenAIResponsesTransformer._raise_response_error(
            {
                "status": "failed",
                "error": {
                    "code": "context_length_exceeded",
                    "message": "input exceeds the model context window",
                },
            }
        )

    assert exc_info.value.message == ERR_LLM_CONTEXT_LENGTH_CONFIG_MISMATCH
    assert exc_info.value.provider_message == "input exceeds the model context window"

    with pytest.raises(LLMException) as ordinary_error:
        OpenAIResponsesTransformer._raise_response_error(
            {
                "status": "failed",
                "error": {
                    "code": "server_error",
                    "message": "provider failed",
                },
            }
        )

    assert not isinstance(ordinary_error.value, LLMContextLengthException)
    assert ordinary_error.value.message == ERR_LLM_CONNECTION_FAILED


def test_responses_stream_failed_event_classifies_nested_context_length_error() -> None:
    with pytest.raises(LLMContextLengthException):
        OpenAIResponsesTransformer._raise_event_error(
            {
                "type": "response.failed",
                "response": {
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "too much input",
                    }
                },
            }
        )


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


def test_openai_transformers_preserve_audio_as_native_multimodal_input() -> None:
    messages = [
        InternalMessage(
            role=MessageRole.USER,
            content=[
                TextPart(text="Listen to this"),
                AudioPart(data="YXVkaW8=", format="mp3"),
            ],
        )
    ]

    assert OpenAIChatCompletionsTransformer.to_provider(messages) == [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Listen to this"},
                {
                    "type": "input_audio",
                    "input_audio": {"data": "YXVkaW8=", "format": "mp3"},
                },
            ],
        }
    ]
    assert OpenAIResponsesTransformer.to_provider(messages) == [
        {
            "role": "user",
            "content": [
                {"type": "input_text", "text": "Listen to this"},
                {
                    "type": "input_audio",
                    "input_audio": {"data": "YXVkaW8=", "format": "mp3"},
                },
            ],
        }
    ]


@pytest.mark.asyncio
async def test_openai_audio_materialization_reads_file_without_mutating_message(tmp_path) -> None:
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"audio")
    source = InternalMessage(
        role=MessageRole.USER,
        content=[AudioPart(path=str(audio_path), format="mp3")],
    )

    materialized = await OpenAIChatCompletionsTransformer._materialize_audio_messages([source])

    assert materialized[0] is not source
    assert materialized[0].content[0].data == "YXVkaW8="
    assert materialized[0].content[0].path == str(audio_path)
    assert source.content[0].data is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("transformer_cls", "payload_key"),
    [
        (OpenAIChatCompletionsTransformer, "messages"),
        (OpenAIResponsesTransformer, "input"),
    ],
)
async def test_openai_generate_materializes_audio_into_request_payload(
    monkeypatch,
    tmp_path,
    transformer_cls,
    payload_key,
) -> None:
    audio_path = tmp_path / "sample.mp3"
    audio_path.write_bytes(b"audio")
    source = InternalMessage(
        role=MessageRole.USER,
        content=[AudioPart(path=str(audio_path), format="mp3")],
    )
    captured = {}

    async def post_json(**kwargs):
        captured.update(kwargs["payload"])
        return {}

    transformer = transformer_cls()
    monkeypatch.setattr(transformer, "_post_json", post_json)

    await transformer.generate(
        api_key="key",
        base_url="https://example.invalid/v1",
        model_id="audio-model",
        messages=[source],
    )

    content = captured[payload_key][0]["content"]
    assert content[0] == {
        "type": "input_audio",
        "input_audio": {"data": "YXVkaW8=", "format": "mp3"},
    }
    assert source.content[0].data is None


def test_chat_completions_to_provider_fills_empty_tool_call_content_without_mutating_messages() -> None:
    contents = [None, "", "  \n", [], "Keep this content"]
    messages = [
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            tool_calls=[InternalToolCall(id=f"call_{index}", name="lookup", arguments={})],
        )
        for index, content in enumerate(contents)
    ]

    provider_messages = OpenAIChatCompletionsTransformer.to_provider(messages)

    assert [message["content"] for message in provider_messages] == [
        "[tool_call]",
        "[tool_call]",
        "[tool_call]",
        "[tool_call]",
        "Keep this content",
    ]
    assert [message.content for message in messages] == contents


def test_chat_completions_from_provider_clears_tool_call_placeholder() -> None:
    message = OpenAIChatCompletionsTransformer.from_provider(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "[tool_call]",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"query":"value"}'},
                            }
                        ],
                    }
                }
            ]
        }
    )

    assert message.role == MessageRole.ASSISTANT
    assert message.content is None
    assert message.tool_calls == [
        InternalToolCall(
            id="call_1",
            name="lookup",
            arguments={"query": "value"},
            provider_metadata={
                "protocol": "openai_chat_completions",
                "tool_call": {"type": "function"},
            },
        )
    ]


def test_chat_completions_from_provider_preserves_text_around_tool_call_placeholder() -> None:
    message = OpenAIChatCompletionsTransformer.from_provider(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "I will call [tool_call]",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
    )

    assert message.content == "I will call [tool_call]"
    assert message.tool_calls == [
        InternalToolCall(
            id="call_1",
            name="lookup",
            arguments={},
            provider_metadata={
                "protocol": "openai_chat_completions",
                "tool_call": {"type": "function"},
            },
        )
    ]


def test_chat_completions_from_provider_uses_refusal_for_tool_call_placeholder() -> None:
    message = OpenAIChatCompletionsTransformer.from_provider(
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "[tool_call]",
                        "refusal": "I cannot help with that.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ],
                    }
                }
            ]
        }
    )

    assert message.content == "I cannot help with that."
    assert message.refusal == "I cannot help with that."


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
