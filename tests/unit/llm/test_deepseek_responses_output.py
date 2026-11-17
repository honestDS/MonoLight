import pytest

from app.core.constants import ERR_LLM_API_RESPONSE_ERROR, ERR_LLM_EMPTY_RESPONSE
from app.core.exceptions import LLMContextLengthException, LLMException
from app.transformers.deepseek.responses import DeepSeekResponsesTransformer


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

    assert response.usage["prompt_tokens"] is None
    assert response.usage["completion_tokens"] is None
    assert response.usage["total_tokens"] is None
    assert response.usage["cached_tokens"] is None


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
