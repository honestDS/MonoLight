import json
from typing import Any

import pytest

from app.core.constants import ERR_LLM_IMAGE_OUTPUT_INVALID
from app.core.exceptions import LLMException
from app.models.message import InternalGeneratedImage, InternalMessage, InternalToolCall, MessageRole
from app.providers.llm.client import LLMClient
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer
from app.transformers.openai import base as openai_base_module
from tests.unit.llm.openai_transformer_support import (
    _FakeAiohttpResponse,
    _FakeClientSession,
    _responses_sse_chunks,
)
from tests.unit.llm.openai_transformer_support import _mock_llm_request_max_bytes as _mock_llm_request_max_bytes


def test_responses_completed_image_is_exposed_by_from_provider_and_to_internal_response() -> None:
    provider_response = {
        "id": "resp_image",
        "status": "completed",
        "model": "gpt-image",
        "output": [
            {
                "type": "image_generation_call",
                "id": "img_1",
                "status": "completed",
                "result": "QUJD",
                "output_format": "png",
            }
        ],
    }
    expected_image = InternalGeneratedImage(id="img_1", data="QUJD", mime_type="image/png")

    message = OpenAIResponsesTransformer.from_provider(provider_response)
    response = OpenAIResponsesTransformer.to_internal_response(provider_response, default_model="fallback-model")

    assert message.content is None
    assert message.tool_calls is None
    assert message.generated_images == [expected_image]
    assert message.provider_metadata == {
        "protocol": "openai_responses",
        "output": [
            {
                "type": "image_generation_call",
                "id": "img_1",
                "status": "completed",
                "output_format": "png",
            }
        ],
    }
    assert response.message.generated_images == [expected_image]
    assert response.message.content is None
    assert response.message.tool_calls is None
    assert response.finish_reason == "stop"


def test_responses_mixed_text_reasoning_function_and_image_preserves_each_channel() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_mixed",
            "status": "completed",
            "model": "gpt-test",
            "output": [
                {
                    "type": "reasoning",
                    "id": "rs_1",
                    "encrypted_content": "sealed-reasoning",
                    "summary": [{"type": "summary_text", "text": "Think carefully."}],
                },
                {
                    "type": "message",
                    "id": "msg_1",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": "Answer"}],
                },
                {
                    "type": "image_generation_call",
                    "id": "img_1",
                    "status": "completed",
                    "result": "QUJD",
                    "output_format": "jpeg",
                },
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "status": "completed",
                    "name": "lookup",
                    "arguments": '{"query":"value"}',
                },
            ],
        },
        default_model="fallback-model",
    )

    assert response.message.content == "Answer"
    assert response.message.reasoning_content == "Think carefully."
    assert response.message.generated_images == [InternalGeneratedImage(id="img_1", data="QUJD", mime_type="image/jpeg")]
    assert response.message.tool_calls == [
        InternalToolCall(
            id="call_1",
            name="lookup",
            arguments={"query": "value"},
            provider_metadata={
                "protocol": "openai_responses",
                "item": {"type": "function_call", "id": "fc_1", "status": "completed"},
            },
        )
    ]
    assert [tool_call.id for tool_call in response.message.tool_calls] == ["call_1"]
    assert response.finish_reason == "tool_calls"
    assert response.message.provider_metadata == {
        "protocol": "openai_responses",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "sealed-reasoning",
                "summary": [{"type": "summary_text", "text": "Think carefully."}],
            },
            {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed"},
            {
                "type": "image_generation_call",
                "id": "img_1",
                "status": "completed",
                "output_format": "jpeg",
            },
        ],
    }


def test_responses_generated_images_deduplicate_ids_but_keep_same_content_for_distinct_ids() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_duplicate_images",
            "status": "completed",
            "output": [
                {
                    "type": "image_generation_call",
                    "id": "img_1",
                    "status": "completed",
                    "result": "QUJD",
                    "output_format": "webp",
                },
                {
                    "type": "image_generation_call",
                    "id": "img_1",
                    "status": "completed",
                    "result": "RElGRkVSRU5U",
                    "output_format": "webp",
                },
                {
                    "type": "image_generation_call",
                    "id": "img_2",
                    "status": "completed",
                    "result": "QUJD",
                    "output_format": "webp",
                },
            ],
        },
        default_model="fallback-model",
    )

    assert response.message.generated_images == [
        InternalGeneratedImage(id="img_1", data="QUJD", mime_type="image/webp"),
        InternalGeneratedImage(id="img_2", data="QUJD", mime_type="image/webp"),
    ]


def test_responses_image_without_output_format_has_no_mime_type() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_image_without_format",
            "status": "completed",
            "output": [
                {
                    "type": "image_generation_call",
                    "id": "img_1",
                    "status": "completed",
                    "result": "QUJD",
                }
            ],
        },
        default_model="fallback-model",
    )

    assert response.message.generated_images == [InternalGeneratedImage(id="img_1", data="QUJD", mime_type=None)]


def test_responses_noncompleted_image_does_not_replace_existing_text() -> None:
    response = OpenAIResponsesTransformer.to_internal_response(
        {
            "id": "resp_pending_image",
            "status": "completed",
            "output": [
                {
                    "type": "image_generation_call",
                    "id": "img_pending",
                    "status": "in_progress",
                    "result": "QUJD",
                    "output_format": "png",
                },
                {"type": "message", "content": [{"type": "output_text", "text": "Answer"}]},
            ],
        },
        default_model="fallback-model",
    )

    assert response.message.content == "Answer"
    assert response.message.generated_images is None
    assert response.message.tool_calls is None
    assert response.finish_reason == "stop"


@pytest.mark.parametrize(
    "image_item",
    [
        {
            "type": "image_generation_call",
            "status": "completed",
            "result": "QUJD",
            "output_format": "png",
        },
        {
            "type": "image_generation_call",
            "id": "img_1",
            "status": "completed",
            "result": None,
            "output_format": "png",
        },
        {
            "type": "image_generation_call",
            "id": "img_1",
            "status": "completed",
            "result": "QUJD",
            "output_format": "gif",
        },
    ],
)
def test_responses_rejects_invalid_completed_image_output(image_item: dict[str, Any]) -> None:
    with pytest.raises(LLMException) as exc_info:
        OpenAIResponsesTransformer.from_provider({"output": [image_item]})

    assert exc_info.value.message == ERR_LLM_IMAGE_OUTPUT_INVALID


def test_responses_to_provider_and_input_token_payload_do_not_serialize_generated_image_data() -> None:
    image = InternalGeneratedImage(id="img_1", data="QUJD", mime_type="image/png")
    message = InternalMessage(role=MessageRole.ASSISTANT, content=None, generated_images=[image])

    provider_items = OpenAIResponsesTransformer.to_provider([message])
    input_token_payload = OpenAIResponsesTransformer.build_input_token_payload(
        model_id="gpt-test",
        messages=[message],
        tools=None,
    )

    assert provider_items == []
    assert input_token_payload == {"input": provider_items}
    assert image.data not in json.dumps(provider_items)
    assert image.data not in json.dumps(input_token_payload)
    assert image.data == "QUJD"


def test_chat_completions_text_and_function_response_has_no_generated_images() -> None:
    response = OpenAIChatCompletionsTransformer.to_internal_response(
        {
            "id": "chatcmpl_1",
            "model": "gpt-test",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "I will look it up.",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": '{"query":"value"}'},
                            }
                        ],
                    },
                }
            ],
            "usage": {},
        },
        default_model="fallback-model",
    )

    assert response.message.content == "I will look it up."
    assert response.message.tool_calls is not None
    assert response.message.tool_calls[0].name == "lookup"
    assert response.message.generated_images is None
    assert response.finish_reason == "tool_calls"


@pytest.mark.asyncio
async def test_responses_stream_completed_image_done_and_snapshot_is_deduplicated(monkeypatch) -> None:
    image_item = {
        "type": "image_generation_call",
        "id": "img_stream_1",
        "status": "completed",
        "result": "QUJD",
        "output_format": "png",
    }
    response = _FakeAiohttpResponse(
        chunks=_responses_sse_chunks(
            [
                {"type": "response.output_item.done", "item": image_item},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_stream_image",
                        "status": "completed",
                        "model": "gpt-image",
                        "output": [image_item],
                        "usage": {
                            "input_tokens": 7,
                            "input_tokens_details": {"cached_tokens": 2},
                            "output_tokens": 3,
                            "total_tokens": 10,
                        },
                    },
                },
            ]
        )
    )
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)
    content_callbacks: list[str] = []

    async def on_content(content: str) -> None:
        content_callbacks.append(content)

    result = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-image",
        messages=[InternalMessage(role=MessageRole.USER, content="Draw an image")],
        on_content=on_content,
        protocol="openai_responses",
    )

    assert result.message.content is None
    assert result.message.tool_calls is None
    assert result.message.generated_images == [InternalGeneratedImage(id="img_stream_1", data="QUJD", mime_type="image/png")]
    assert result.finish_reason == "stop"
    assert result.usage == {
        "input_tokens": 7,
        "input_tokens_details": {"cached_tokens": 2},
        "output_tokens": 3,
        "total_tokens": 10,
        "prompt_tokens": 7,
        "completion_tokens": 3,
        "cached_tokens": 2,
    }
    assert content_callbacks == []
    assert result.message.provider_metadata == {
        "protocol": "openai_responses",
        "output": [
            {
                "type": "image_generation_call",
                "id": "img_stream_1",
                "status": "completed",
                "output_format": "png",
            }
        ],
    }
    assert "result" not in result.message.provider_metadata["output"][0]
    request_payload = json.loads(sessions[0].post_calls[0]["kwargs"]["data"])
    assert request_payload["model"] == "gpt-image"
    assert request_payload["stream"] is True


@pytest.mark.parametrize(
    ("event_type", "status", "expected_finish_reason", "incomplete_details"),
    [
        ("response.completed", "completed", "stop", None),
        ("response.incomplete", "incomplete", "length", {"reason": "max_output_tokens"}),
    ],
)
@pytest.mark.asyncio
async def test_responses_stream_terminal_snapshot_backfills_completed_image(
    monkeypatch,
    event_type: str,
    status: str,
    expected_finish_reason: str,
    incomplete_details: dict[str, str] | None,
) -> None:
    image_item = {
        "type": "image_generation_call",
        "id": f"img_{status}",
        "status": "completed",
        "result": "QUJD",
        "output_format": "webp",
    }
    terminal_response: dict[str, Any] = {
        "id": f"resp_{status}",
        "status": status,
        "model": "gpt-image",
        "output": [image_item],
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    }
    if incomplete_details is not None:
        terminal_response["incomplete_details"] = incomplete_details
    response = _FakeAiohttpResponse(chunks=_responses_sse_chunks([{"type": event_type, "response": terminal_response}]))
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)
    content_callbacks: list[str] = []

    async def on_content(content: str) -> None:
        content_callbacks.append(content)

    result = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-image",
        messages=[InternalMessage(role=MessageRole.USER, content="Draw an image")],
        on_content=on_content,
        protocol="openai_responses",
    )

    assert result.message.content is None
    assert result.message.tool_calls is None
    assert result.message.generated_images == [InternalGeneratedImage(id=f"img_{status}", data="QUJD", mime_type="image/webp")]
    assert result.finish_reason == expected_finish_reason
    assert result.usage == {
        "input_tokens": 4,
        "output_tokens": 2,
        "total_tokens": 6,
        "prompt_tokens": 4,
        "completion_tokens": 2,
        "cached_tokens": None,
    }
    assert result.finish_details is not None
    assert result.finish_details["status"] == status
    if incomplete_details is not None:
        assert result.finish_details["incomplete_details"] == incomplete_details
    assert content_callbacks == []
    assert result.message.provider_metadata == {
        "protocol": "openai_responses",
        "output": [
            {
                "type": "image_generation_call",
                "id": f"img_{status}",
                "status": "completed",
                "output_format": "webp",
            }
        ],
    }
    request_payload = json.loads(sessions[0].post_calls[0]["kwargs"]["data"])
    assert request_payload["stream"] is True


@pytest.mark.asyncio
async def test_responses_stream_keeps_order_for_distinct_images_with_same_data(monkeypatch) -> None:
    first_image = {
        "type": "image_generation_call",
        "id": "img_first",
        "status": "completed",
        "result": "QUJD",
        "output_format": "jpeg",
    }
    second_image = {
        "type": "image_generation_call",
        "id": "img_second",
        "status": "completed",
        "result": "QUJD",
        "output_format": "jpeg",
    }
    response = _FakeAiohttpResponse(
        chunks=_responses_sse_chunks(
            [
                {"type": "response.output_item.done", "item": first_image},
                {"type": "response.output_item.done", "item": second_image},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_two_images",
                        "status": "completed",
                        "model": "gpt-image",
                        "output": [first_image, second_image],
                        "usage": {"input_tokens": 2, "output_tokens": 2, "total_tokens": 4},
                    },
                },
            ]
        )
    )
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)
    content_callbacks: list[str] = []

    async def on_content(content: str) -> None:
        content_callbacks.append(content)

    result = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-image",
        messages=[InternalMessage(role=MessageRole.USER, content="Draw two images")],
        on_content=on_content,
        protocol="openai_responses",
    )

    assert result.message.generated_images == [
        InternalGeneratedImage(id="img_first", data="QUJD", mime_type="image/jpeg"),
        InternalGeneratedImage(id="img_second", data="QUJD", mime_type="image/jpeg"),
    ]
    assert result.message.content is None
    assert result.message.tool_calls is None
    assert result.finish_reason == "stop"
    assert content_callbacks == []
    assert result.message.provider_metadata == {
        "protocol": "openai_responses",
        "output": [
            {"type": "image_generation_call", "id": "img_first", "status": "completed", "output_format": "jpeg"},
            {"type": "image_generation_call", "id": "img_second", "status": "completed", "output_format": "jpeg"},
        ],
    }
    assert json.loads(sessions[0].post_calls[0]["kwargs"]["data"])["stream"] is True


@pytest.mark.asyncio
async def test_responses_stream_ignores_partial_and_noncompleted_images(monkeypatch) -> None:
    noncompleted_images = [
        {
            "type": "image_generation_call",
            "id": "img_in_progress",
            "status": "in_progress",
            "result": "QUJD",
            "output_format": "png",
        },
        {
            "type": "image_generation_call",
            "id": "img_generating",
            "status": "generating",
            "result": "QUJD",
            "output_format": "png",
        },
        {
            "type": "image_generation_call",
            "id": "img_failed",
            "status": "failed",
            "result": "QUJD",
            "output_format": "png",
        },
    ]
    response = _FakeAiohttpResponse(
        chunks=_responses_sse_chunks(
            [
                {
                    "type": "response.image_generation_call.partial_image",
                    "item_id": "img_partial",
                    "output_index": 0,
                    "partial_image_index": 0,
                    "partial_image_b64": "QUJD",
                },
                {"type": "response.image_generation_call.in_progress", "item_id": "img_in_progress", "output_index": 1},
                {"type": "response.image_generation_call.generating", "item_id": "img_generating", "output_index": 2},
                {"type": "response.image_generation_call.failed", "item_id": "img_failed", "output_index": 3},
                *[{"type": "response.output_item.done", "item": item} for item in noncompleted_images],
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_noncompleted_images",
                        "status": "completed",
                        "model": "gpt-image",
                        "output": noncompleted_images,
                        "usage": {"input_tokens": 1, "output_tokens": 1, "total_tokens": 2},
                    },
                },
            ]
        )
    )
    sessions: list[_FakeClientSession] = []

    def fake_client_session(**_kwargs):
        session = _FakeClientSession(response)
        sessions.append(session)
        return session

    monkeypatch.setattr(openai_base_module.aiohttp, "ClientSession", fake_client_session)
    content_callbacks: list[str] = []

    async def on_content(content: str) -> None:
        content_callbacks.append(content)

    result = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-image",
        messages=[InternalMessage(role=MessageRole.USER, content="Draw an image")],
        on_content=on_content,
        protocol="openai_responses",
    )

    assert result.message.generated_images is None
    assert result.message.content is None
    assert result.message.tool_calls is None
    assert result.finish_reason == "stop"
    assert content_callbacks == []
    assert "QUJD" not in content_callbacks
    assert json.loads(sessions[0].post_calls[0]["kwargs"]["data"])["stream"] is True


@pytest.mark.asyncio
async def test_responses_stream_image_coexists_with_text_reasoning_and_function_deltas(monkeypatch) -> None:
    image_item = {
        "type": "image_generation_call",
        "id": "img_mixed_stream",
        "status": "completed",
        "result": "QUJD",
        "output_format": "jpeg",
    }
    response = _FakeAiohttpResponse(
        chunks=_responses_sse_chunks(
            [
                {"type": "response.output_text.delta", "output_index": 0, "content_index": 0, "delta": "Answer"},
                {
                    "type": "response.reasoning_summary_text.delta",
                    "output_index": 1,
                    "summary_index": 0,
                    "delta": "Think",
                },
                {
                    "type": "response.output_item.added",
                    "output_index": 2,
                    "item": {
                        "type": "function_call",
                        "id": "fc_1",
                        "call_id": "call_1",
                        "name": "lookup",
                    },
                },
                {
                    "type": "response.function_call_arguments.delta",
                    "output_index": 2,
                    "item_id": "fc_1",
                    "delta": '{"query":"value"}',
                },
                {"type": "response.output_item.done", "item": image_item},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_mixed_stream",
                        "status": "completed",
                        "model": "gpt-mixed",
                        "output": [
                            {
                                "type": "reasoning",
                                "id": "rs_1",
                                "encrypted_content": "sealed-reasoning",
                                "summary": [{"type": "summary_text", "text": "Think"}],
                            },
                            {
                                "type": "message",
                                "id": "msg_1",
                                "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": "Answer"}],
                            },
                            image_item,
                            {
                                "type": "function_call",
                                "id": "fc_1",
                                "call_id": "call_1",
                                "status": "completed",
                                "name": "lookup",
                                "arguments": '{"query":"value"}',
                            },
                        ],
                        "usage": {
                            "input_tokens": 5,
                            "input_tokens_details": {"cached_tokens": 1},
                            "output_tokens": 6,
                            "total_tokens": 11,
                        },
                    },
                },
            ]
        )
    )
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

    async def on_reasoning(content: str) -> None:
        reasoning_callbacks.append(content)

    result = await LLMClient.generate_with_stream_callback(
        api_key="key",
        base_url="https://example.invalid",
        model_id="gpt-mixed",
        messages=[InternalMessage(role=MessageRole.USER, content="Answer and draw")],
        on_content=on_content,
        on_reasoning=on_reasoning,
        protocol="openai_responses",
    )

    assert result.message.content == "Answer"
    assert result.message.reasoning_content == "Think"
    assert result.message.generated_images == [InternalGeneratedImage(id="img_mixed_stream", data="QUJD", mime_type="image/jpeg")]
    assert result.message.tool_calls is not None
    assert [(tool_call.name, tool_call.arguments) for tool_call in result.message.tool_calls] == [("lookup", {"query": "value"})]
    assert result.finish_reason == "tool_calls"
    assert result.usage == {
        "input_tokens": 5,
        "input_tokens_details": {"cached_tokens": 1},
        "output_tokens": 6,
        "total_tokens": 11,
        "prompt_tokens": 5,
        "completion_tokens": 6,
        "cached_tokens": 1,
    }
    assert content_callbacks == ["Answer"]
    assert reasoning_callbacks == ["Think"]
    assert "QUJD" not in content_callbacks
    assert result.message.provider_metadata == {
        "protocol": "openai_responses",
        "output": [
            {
                "type": "reasoning",
                "id": "rs_1",
                "encrypted_content": "sealed-reasoning",
                "summary": [{"type": "summary_text", "text": "Think"}],
            },
            {"type": "message", "id": "msg_1", "role": "assistant", "status": "completed"},
            {
                "type": "image_generation_call",
                "id": "img_mixed_stream",
                "status": "completed",
                "output_format": "jpeg",
            },
        ],
    }
    assert "result" not in result.message.provider_metadata["output"][2]
    request_payload = json.loads(sessions[0].post_calls[0]["kwargs"]["data"])
    assert request_payload["model"] == "gpt-mixed"
    assert request_payload["stream"] is True
