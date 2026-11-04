import pytest

from app.core.utils import request_token_baseline as baseline_module


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
                "prompt_tokens": 1000,
                "completion_tokens": 120,
            },
            {
                "input_tokens": 1000,
                "input_tokens_source": "provider",
                "output_tokens": 120,
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
            0,
        ),
        ({"output_tokens": 20}, 0),
        ({"total_output_tokens": True}, 0),
        ([], 0),
    ),
)
def test_extract_session_total_output_tokens(metadata, expected):
    assert baseline_module.extract_session_total_output_tokens(metadata) == expected


def test_confirmed_provider_input_tokens_require_current_context_revision():
    metadata = {
        "channel_id": 7,
        "input_tokens": 344_000,
        "input_tokens_source": "provider",
        "model_id": "gpt-5.6-luna",
        "protocol": "openai_responses",
        "context_summary_revision": 4,
        "context_content_revision": 2,
    }

    assert (
        baseline_module.extract_confirmed_provider_input_tokens(
            metadata,
            channel_id=7,
            model_id="gpt-5.6-luna",
            protocol="openai_responses",
            context_summary_revision=4,
            context_content_revision=2,
        )
        == 344_000
    )
    assert (
        baseline_module.extract_confirmed_provider_input_tokens(
            metadata,
            channel_id=7,
            model_id="gpt-5.6-luna",
            protocol="openai_responses",
            context_summary_revision=5,
            context_content_revision=2,
        )
        is None
    )
    assert (
        baseline_module.extract_confirmed_provider_input_tokens(
            metadata,
            channel_id=8,
            model_id="gpt-5.6-luna",
            protocol="openai_responses",
            context_summary_revision=4,
            context_content_revision=2,
        )
        is None
    )
    legacy_metadata = {key: value for key, value in metadata.items() if key != "channel_id"}
    assert (
        baseline_module.extract_confirmed_provider_input_tokens(
            legacy_metadata,
            channel_id=7,
            model_id="gpt-5.6-luna",
            protocol="openai_responses",
            context_summary_revision=4,
            context_content_revision=2,
        )
        is None
    )


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
            (0, 0),
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
                "total_input_tokens": 1000,
                "total_cached_tokens": 1200,
                "input_tokens": 500,
                "input_tokens_source": "provider",
                "cached_tokens": 400,
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


@pytest.mark.parametrize(
    ("total_input_tokens", "total_cached_tokens", "expected"),
    (
        (2100, 640, (2100, 640, 640 / 2100)),
        (2100, 0, (2100, 0, 0)),
        (0, 0, (0, 0, None)),
        (2100, 2200, (0, 0, None)),
        (2100, -1, (0, 0, None)),
        (2100, True, (0, 0, None)),
    ),
)
def test_build_session_cache_metrics_returns_three_validated_fields(total_input_tokens, total_cached_tokens, expected):
    metrics = baseline_module.build_session_cache_metrics(total_input_tokens, total_cached_tokens)

    assert metrics == {
        "total_input_tokens": expected[0],
        "total_cached_tokens": expected[1],
        "cache_hit_rate": expected[2],
    }


def test_accumulate_session_cache_metrics_keeps_provider_totals_and_ignores_estimates():
    first_provider_metrics = baseline_module.extract_provider_token_metrics(
        {
            "prompt_tokens": 1000,
            "cached_tokens": 200,
        }
    )
    total_input_tokens, total_cached_tokens = baseline_module.accumulate_session_cache_metrics(first_provider_metrics)

    assert first_provider_metrics["total_input_tokens"] == 1000
    assert first_provider_metrics["total_cached_tokens"] == 200
    assert first_provider_metrics["cache_hit_rate"] == pytest.approx(200 / 1000)

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

    third_provider_metrics = baseline_module.extract_provider_token_metrics({"prompt_tokens": 100})
    updated_totals = baseline_module.accumulate_session_cache_metrics(
        third_provider_metrics,
        total_input_tokens=total_input_tokens,
        total_cached_tokens=total_cached_tokens,
    )

    assert updated_totals == (2200, 640)
    assert third_provider_metrics["total_input_tokens"] == 2200
    assert third_provider_metrics["total_cached_tokens"] == 640
    assert third_provider_metrics["cache_hit_rate"] == pytest.approx(640 / 2200)


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
