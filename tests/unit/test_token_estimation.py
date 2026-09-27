import pytest

from app.core.utils.token_calibration import (
    apply_token_calibration,
    build_token_calibration_metadata,
    extract_token_calibration_factor,
)
from app.core.utils.tokenizer import estimate_tokens, resolve_token_encoding_name
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.token_estimation import resolve_request_token_estimate
from app.transformers.openai import OpenAIChatCompletionsTransformer


def test_modern_openai_model_uses_o200k_encoding():
    assert resolve_token_encoding_name("gpt-5.6-luna", protocol="openai") == "o200k_base"


def test_legacy_openai_model_keeps_cl100k_encoding():
    assert resolve_token_encoding_name("gpt-4-0613", protocol="openai") == "cl100k_base"


def test_model_aware_token_count_uses_resolved_encoding_for_modern_model():
    text = "长期上下文 token estimation " * 200

    assert estimate_tokens(text, model_id="gpt-5.6-luna", protocol="openai") == estimate_tokens(
        text,
        encoding_name="o200k_base",
    )


def test_calibration_uses_same_model_and_protocol_provider_history():
    metadata = {
        "model_id": "gpt-5.6-luna",
        "protocol": "openai",
        "input_token_calibration_estimated_total": 500_000,
        "input_token_calibration_provider_total": 300_000,
        "input_token_calibration_samples": 1,
    }

    factor = extract_token_calibration_factor(
        metadata,
        model_id="gpt-5.6-luna",
        protocol="openai",
    )

    assert factor == pytest.approx(0.6)
    assert apply_token_calibration(500_000, factor) == 300_000


def test_calibration_accumulates_provider_observations_and_resets_on_model_change():
    previous = {
        "model_id": "gpt-5.6-luna",
        "protocol": "openai",
        "input_token_calibration_estimated_total": 500,
        "input_token_calibration_provider_total": 300,
        "input_token_calibration_samples": 1,
    }

    accumulated = build_token_calibration_metadata(
        previous,
        model_id="gpt-5.6-luna",
        protocol="openai",
        raw_estimated_tokens=600,
        provider_input_tokens=330,
    )
    reset = build_token_calibration_metadata(
        previous,
        model_id="other-model",
        protocol="openai",
        raw_estimated_tokens=600,
        provider_input_tokens=330,
    )

    assert accumulated["input_token_calibration_estimated_total"] == 1100
    assert accumulated["input_token_calibration_provider_total"] == 630
    assert accumulated["input_token_calibration_samples"] == 2
    assert reset["input_token_calibration_estimated_total"] == 600
    assert reset["input_token_calibration_provider_total"] == 330
    assert reset["input_token_calibration_samples"] == 1


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


@pytest.mark.asyncio
async def test_upstream_token_counter_has_priority_over_local_estimator():
    class CountingTransformer:
        @classmethod
        def build_input_token_payload(cls, *, model_id, messages, tools):
            return {"input": [{"role": "user", "content": "local"}]}

        async def count_input_tokens(self, **kwargs):
            return 321

    estimate = await resolve_request_token_estimate(
        CountingTransformer(),
        api_key="key",
        base_url="https://example.invalid/v1",
        model_id="provider-model",
        protocol="openai",
        messages=[InternalMessage(role=MessageRole.USER, content="hello")],
        tools=None,
        timeout=10,
        http_proxy=None,
        custom_headers=None,
        calibration_factor=0.5,
    )

    assert estimate.input_tokens == 321
    assert estimate.source == "provider_count"
    assert estimate.raw_local_tokens is None


@pytest.mark.asyncio
async def test_local_provider_payload_estimate_applies_historical_calibration():
    class LocalOnlyTransformer:
        @classmethod
        def build_input_token_payload(cls, *, model_id, messages, tools):
            return {"input": [{"role": "user", "content": "hello world"}]}

        async def count_input_tokens(self, **kwargs):
            return None

    estimate = await resolve_request_token_estimate(
        LocalOnlyTransformer(),
        api_key="key",
        base_url="https://example.invalid/v1",
        model_id="gpt-5.6-luna",
        protocol="openai",
        messages=[InternalMessage(role=MessageRole.USER, content="hello world")],
        tools=None,
        timeout=10,
        http_proxy=None,
        custom_headers=None,
        calibration_factor=0.6,
    )

    assert estimate.raw_local_tokens is not None
    assert estimate.input_tokens == apply_token_calibration(estimate.raw_local_tokens, 0.6)
    assert estimate.source == "local_payload_calibrated"

