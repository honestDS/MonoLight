import pytest

from app.core.utils import request_token_baseline as baseline_module
from app.models.message import InternalMessage, MessageRole


@pytest.mark.parametrize(
    ("usage", "expected"),
    (
        (
            {
                "prompt_tokens": 1000,
                "completion_tokens": 120,
                "cached_tokens": 250,
            },
            {
                "input_tokens": 1000,
                "input_tokens_source": "provider",
                "output_tokens": 120,
                "cached_tokens": 250,
                "cache_hit_rate": 0.25,
            },
        ),
        (
            {
                "prompt_tokens": 1000,
                "cached_tokens": 1500,
            },
            {
                "input_tokens": 1000,
                "input_tokens_source": "provider",
                "cached_tokens": 1500,
                "cache_hit_rate": 1.0,
            },
        ),
        (
            {
                "prompt_tokens": True,
                "completion_tokens": True,
                "cached_tokens": True,
            },
            {},
        ),
        (
            {
                "prompt_tokens": -1,
                "completion_tokens": -1,
                "cached_tokens": -1,
            },
            {},
        ),
        ([], {}),
    ),
)
def test_extract_provider_token_metrics(usage, expected):
    assert baseline_module.extract_provider_token_metrics(usage) == expected


@pytest.mark.parametrize(
    ("metadata", "expected"),
    (
        (
            {
                "output_tokens": 120,
                "cached_tokens": 250,
                "cache_hit_rate": 0.25,
                "input_tokens": 1000,
            },
            {
                "output_tokens": 120,
                "cached_tokens": 250,
                "cache_hit_rate": 0.25,
            },
        ),
        (
            {
                "output_tokens": True,
                "cached_tokens": -1,
                "cache_hit_rate": 1.1,
            },
            {},
        ),
        (
            {
                "output_tokens": 0,
                "cached_tokens": 0,
                "cache_hit_rate": False,
            },
            {
                "output_tokens": 0,
                "cached_tokens": 0,
            },
        ),
    ),
)
def test_extract_reusable_token_metrics(metadata, expected):
    assert baseline_module.extract_reusable_token_metrics(metadata) == expected


@pytest.mark.parametrize(
    ("metadata", "expected"),
    (
        (
            {
                "output_tokens": 20,
                "total_output_tokens": 200,
            },
            200,
        ),
        (
            {
                "output_tokens": 20,
                "total_output_tokens": -1,
            },
            20,
        ),
        ({"total_output_tokens": True}, 0),
        ([], 0),
    ),
)
def test_extract_session_total_output_tokens(metadata, expected):
    assert baseline_module.extract_session_total_output_tokens(metadata) == expected


def _metadata_for(messages, tools=None):
    metadata = baseline_module.build_request_token_baseline(
        messages,
        tools,
        model_id="grok-4.5",
        protocol="openai",
        context_summary_revision=2,
        context_content_revision=3,
    )
    metadata.update(
        {
            "input_tokens": 7500,
            "input_tokens_source": "provider",
        }
    )
    return metadata


def test_incremental_input_tokens_add_only_messages_after_provider_baseline(monkeypatch):
    monkeypatch.setattr(baseline_module, "estimate_tokens", len)
    previous_messages = [
        InternalMessage(role=MessageRole.SYSTEM, content="system"),
        InternalMessage(id=1, role=MessageRole.USER, content="old user"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="old answer"),
    ]
    metadata = _metadata_for(previous_messages)
    current_messages = [
        *previous_messages,
        InternalMessage(id=3, role=MessageRole.ASSISTANT, content="abc"),
        InternalMessage(id=4, role=MessageRole.USER, content="xy", environment_prompt="env"),
    ]

    result = baseline_module.estimate_incremental_input_tokens(
        current_messages,
        None,
        metadata,
        model_id="grok-4.5",
        protocol="openai",
        context_summary_revision=2,
        context_content_revision=3,
    )

    assert result == 7500 + len("abc") + len("xy") + len("env")


def test_incremental_input_tokens_falls_back_when_history_range_changes(monkeypatch):
    monkeypatch.setattr(baseline_module, "estimate_tokens", len)
    previous_messages = [
        InternalMessage(id=1, role=MessageRole.USER, content="old user"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="old answer"),
    ]
    metadata = _metadata_for(previous_messages)

    result = baseline_module.estimate_incremental_input_tokens(
        [
            InternalMessage(id=2, role=MessageRole.ASSISTANT, content="old answer"),
            InternalMessage(id=3, role=MessageRole.USER, content="new user"),
        ],
        None,
        metadata,
        model_id="grok-4.5",
        protocol="openai",
        context_summary_revision=2,
        context_content_revision=3,
    )

    assert result is None


def test_incremental_input_tokens_falls_back_when_model_or_summary_changes(monkeypatch):
    monkeypatch.setattr(baseline_module, "estimate_tokens", len)
    messages = [
        InternalMessage(id=1, role=MessageRole.USER, content="old user"),
        InternalMessage(id=2, role=MessageRole.ASSISTANT, content="old answer"),
    ]
    metadata = _metadata_for(messages)

    model_changed = baseline_module.estimate_incremental_input_tokens(
        messages,
        None,
        metadata,
        model_id="other-model",
        protocol="openai",
        context_summary_revision=2,
        context_content_revision=3,
    )
    summary_changed = baseline_module.estimate_incremental_input_tokens(
        messages,
        None,
        metadata,
        model_id="grok-4.5",
        protocol="openai",
        context_summary_revision=3,
        context_content_revision=3,
    )

    assert model_changed is None
    assert summary_changed is None


@pytest.mark.parametrize(
    ("metadata", "expected"),
    (
        (
            {
                "total_input_tokens": 2100,
                "total_cached_tokens": 640,
            },
            (2100, 640),
        ),
        (
            {
                "input_tokens": 1000,
                "input_tokens_source": "provider",
                "cached_tokens": 1500,
            },
            (1000, 1000),
        ),
        (
            {
                "total_input_tokens": 1000,
                "total_cached_tokens": 1200,
            },
            (0, 0),
        ),
        (
            {
                "input_tokens": 1000,
                "input_tokens_source": "estimated",
                "cached_tokens": 200,
            },
            (0, 0),
        ),
    ),
)
def test_extract_session_cache_token_totals(metadata, expected):
    assert baseline_module.extract_session_cache_token_totals(metadata) == expected


@pytest.mark.parametrize(
    ("metadata", "total_input_tokens", "total_cached_tokens", "expected"),
    (
        (
            {
                "total_input_tokens": 2000,
                "total_cached_tokens": 640,
            },
            2100,
            600,
            (2100, 640),
        ),
        (
            {
                "total_input_tokens": 2000,
                "total_cached_tokens": 640,
            },
            1000,
            1100,
            (2000, 640),
        ),
    ),
)
def test_merge_session_cache_token_totals(metadata, total_input_tokens, total_cached_tokens, expected):
    assert (
        baseline_module.merge_session_cache_token_totals(
            metadata,
            total_input_tokens=total_input_tokens,
            total_cached_tokens=total_cached_tokens,
        )
        == expected
    )


def test_build_session_cache_metrics_uses_zero_rate_without_input_tokens():
    zero_metrics = baseline_module.build_session_cache_metrics(0, 0)
    valid_metrics = baseline_module.build_session_cache_metrics(2100, 640)

    assert zero_metrics["total_input_tokens"] == 0
    assert zero_metrics["total_cached_tokens"] == 0
    assert zero_metrics["cache_hit_rate"] == pytest.approx(0.0)
    assert valid_metrics["total_input_tokens"] == 2100
    assert valid_metrics["total_cached_tokens"] == 640
    assert valid_metrics["cache_hit_rate"] == pytest.approx(640 / 2100)


def test_accumulate_session_cache_metrics_tracks_provider_totals_and_writes_combined_metrics():
    first_provider_metrics = baseline_module.extract_provider_token_metrics(
        {
            "prompt_tokens": 1000,
            "cached_tokens": 200,
        }
    )
    total_input_tokens, total_cached_tokens = baseline_module.accumulate_session_cache_metrics(first_provider_metrics)

    second_provider_metrics = baseline_module.extract_provider_token_metrics(
        {
            "prompt_tokens": 1100,
            "cached_tokens": 440,
        }
    )
    total_input_tokens, total_cached_tokens = baseline_module.accumulate_session_cache_metrics(
        second_provider_metrics,
        total_input_tokens=total_input_tokens,
        total_cached_tokens=total_cached_tokens,
    )

    estimated_metrics = {
        "input_tokens": 500,
        "input_tokens_source": "estimated",
        "cached_tokens": 500,
    }
    unchanged_totals = baseline_module.accumulate_session_cache_metrics(
        estimated_metrics,
        total_input_tokens=total_input_tokens,
        total_cached_tokens=total_cached_tokens,
    )

    assert (total_input_tokens, total_cached_tokens) == (2100, 640)
    assert second_provider_metrics["total_input_tokens"] == 2100
    assert second_provider_metrics["total_cached_tokens"] == 640
    assert second_provider_metrics["cache_hit_rate"] == pytest.approx(640 / 2100)
    assert unchanged_totals == (2100, 640)
    assert estimated_metrics["total_input_tokens"] == 2100
    assert estimated_metrics["total_cached_tokens"] == 640
    assert estimated_metrics["cache_hit_rate"] == pytest.approx(640 / 2100)


def test_provider_request_usage_metadata_round_trips_raw_provider_metrics():
    metadata = baseline_module.build_provider_request_usage_metadata(
        "request-1",
        {
            "input_tokens": 100,
            "input_tokens_source": "provider",
            "cached_tokens": 140,
            "output_tokens": 7,
        },
    )

    assert metadata[baseline_module.PROVIDER_REQUEST_ID_METADATA_KEY] == "request-1"
    assert metadata[baseline_module.PROVIDER_INPUT_TOKENS_METADATA_KEY] == 100
    assert metadata[baseline_module.PROVIDER_CACHED_TOKENS_METADATA_KEY] == 100
    assert metadata[baseline_module.PROVIDER_OUTPUT_TOKENS_METADATA_KEY] == 7
    assert baseline_module.extract_provider_request_usage(metadata) == ("request-1", 100, 100, 7)


@pytest.mark.parametrize(
    "metadata",
    (
        [],
        {
            baseline_module.PROVIDER_REQUEST_ID_METADATA_KEY: "",
            baseline_module.PROVIDER_INPUT_TOKENS_METADATA_KEY: 100,
            baseline_module.PROVIDER_CACHED_TOKENS_METADATA_KEY: 0,
            baseline_module.PROVIDER_OUTPUT_TOKENS_METADATA_KEY: 7,
        },
        {
            baseline_module.PROVIDER_REQUEST_ID_METADATA_KEY: "request-1",
            baseline_module.PROVIDER_INPUT_TOKENS_METADATA_KEY: 100,
            baseline_module.PROVIDER_CACHED_TOKENS_METADATA_KEY: 101,
            baseline_module.PROVIDER_OUTPUT_TOKENS_METADATA_KEY: 7,
        },
        {
            baseline_module.PROVIDER_REQUEST_ID_METADATA_KEY: "request-1",
            baseline_module.PROVIDER_INPUT_TOKENS_METADATA_KEY: 0,
            baseline_module.PROVIDER_CACHED_TOKENS_METADATA_KEY: 0,
            baseline_module.PROVIDER_OUTPUT_TOKENS_METADATA_KEY: 0,
        },
        {
            baseline_module.PROVIDER_REQUEST_ID_METADATA_KEY: "request-1",
            baseline_module.PROVIDER_INPUT_TOKENS_METADATA_KEY: True,
            baseline_module.PROVIDER_CACHED_TOKENS_METADATA_KEY: 0,
            baseline_module.PROVIDER_OUTPUT_TOKENS_METADATA_KEY: 7,
        },
    ),
)
def test_extract_provider_request_usage_rejects_invalid_metadata(metadata):
    assert baseline_module.extract_provider_request_usage(metadata) is None


def test_provider_request_usage_metadata_ignores_estimated_input():
    metadata = baseline_module.build_provider_request_usage_metadata(
        "request-2",
        {
            "input_tokens": 100,
            "input_tokens_source": "estimated",
            "cached_tokens": 100,
            "output_tokens": 3,
        },
    )

    assert metadata[baseline_module.PROVIDER_INPUT_TOKENS_METADATA_KEY] == 0
    assert metadata[baseline_module.PROVIDER_CACHED_TOKENS_METADATA_KEY] == 0
    assert metadata[baseline_module.PROVIDER_OUTPUT_TOKENS_METADATA_KEY] == 3
    assert baseline_module.extract_provider_request_usage(metadata) == ("request-2", 0, 0, 3)
