from app.core.utils.tokenizer import estimate_tokens, resolve_token_encoding_name
from app.models.message import AudioPart, InternalMessage, MessageRole
from app.providers.llm.token_estimation import estimate_request_tokens_locally
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer


def test_modern_openai_model_uses_o200k_encoding_without_inferring_vendor_from_protocol():
    assert resolve_token_encoding_name("gpt-5.6-luna", protocol="openai") == "o200k_base"
    assert resolve_token_encoding_name("gpt-5.6-luna", protocol="compatible_gateway") == "o200k_base"
    assert resolve_token_encoding_name("gpt-4.5", protocol="openai") == "o200k_base"


def test_unknown_gateway_alias_uses_generic_fallback_without_vendor_guessing():
    assert resolve_token_encoding_name("main-model", protocol="openai") == "cl100k_base"
    assert resolve_token_encoding_name("main-model", protocol="openai_responses") == "cl100k_base"
    assert resolve_token_encoding_name("gpt-6.0-gateway", protocol="openai") == "cl100k_base"
    assert resolve_token_encoding_name("gpt-6.0-custom", protocol="openai") == "cl100k_base"


def test_legacy_openai_model_keeps_cl100k_encoding():
    assert resolve_token_encoding_name("gpt-4-0613", protocol="openai") == "cl100k_base"


def test_model_aware_token_count_uses_resolved_encoding_for_modern_model():
    text = "长期上下文 token estimation " * 200

    assert estimate_tokens(text, model_id="gpt-5.6-luna", protocol="openai") == estimate_tokens(
        text,
        encoding_name="o200k_base",
    )


def test_chat_estimation_payload_matches_provider_shape_and_excludes_internal_metadata():
    messages = [
        InternalMessage(
            role=MessageRole.ASSISTANT,
            content="visible",
            reasoning_content="internal reasoning",
            provider_metadata={"large_internal_blob": "x" * 1000},
        )
    ]

    payload = OpenAIChatCompletionsTransformer.build_input_token_payload(
        model_id="gpt-5.6-luna",
        messages=messages,
        tools=None,
    )

    assert payload == {"messages": [{"role": "assistant", "content": "visible"}]}


def test_responses_estimation_payload_matches_provider_input_and_tool_shape():
    tools = [
        {
            "type": "function",
            "function": {
                "name": "lookup",
                "description": "Lookup",
                "parameters": {"type": "object", "properties": {}},
            },
        }
    ]

    payload = OpenAIResponsesTransformer.build_input_token_payload(
        model_id="gpt-5.6-luna",
        messages=[InternalMessage(role=MessageRole.USER, content="hello")],
        tools=tools,
    )

    assert payload["input"] == [{"role": "user", "content": "hello"}]
    assert payload["tools"] == [
        {
            "type": "function",
            "name": "lookup",
            "description": "Lookup",
            "parameters": {"type": "object", "properties": {}},
            "strict": False,
        }
    ]


def test_local_request_estimate_counts_transformed_provider_payload():
    class LocalTransformer:
        @classmethod
        def build_input_token_payload(cls, *, model_id, messages, tools):
            del model_id, messages, tools
            return {"input": [{"role": "user", "content": "hello world"}]}

    estimate = estimate_request_tokens_locally(
        LocalTransformer(),
        model_id="gpt-5.6-luna",
        protocol="openai",
        messages=[InternalMessage(role=MessageRole.USER, content="ignored internal message")],
        tools=None,
    )

    assert estimate > 0


def test_local_request_estimate_does_not_count_audio_base64_as_text_tokens():
    def estimate_for(data: str) -> int:
        return estimate_request_tokens_locally(
            OpenAIChatCompletionsTransformer,
            model_id="gpt-5.6-luna",
            protocol="openai",
            messages=[
                InternalMessage(
                    role=MessageRole.USER,
                    content=[AudioPart(data=data, format="mp3")],
                )
            ],
            tools=None,
        )

    assert estimate_for("YQ==") == estimate_for("YQ==" * 10000)
