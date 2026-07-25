from app.core.utils import request_token_baseline as baseline_module
from app.models.message import InternalMessage, MessageRole


def test_extract_provider_token_metrics_returns_available_usage_metrics():
    assert baseline_module.extract_provider_token_metrics(
        {
            "prompt_tokens": 1000,
            "completion_tokens": 120,
            "cached_tokens": 250,
        }
    ) == {
        "input_tokens": 1000,
        "input_tokens_source": "provider",
        "output_tokens": 120,
        "cached_tokens": 250,
        "cache_hit_rate": 0.25,
    }


def test_extract_provider_token_metrics_clamps_cache_hit_rate():
    assert baseline_module.extract_provider_token_metrics(
        {
            "prompt_tokens": 1000,
            "cached_tokens": 1500,
        }
    ) == {
        "input_tokens": 1000,
        "input_tokens_source": "provider",
        "cached_tokens": 1500,
        "cache_hit_rate": 1.0,
    }


def test_extract_provider_token_metrics_ignores_invalid_usage_values():
    invalid_usages = (
        {
            "prompt_tokens": True,
            "completion_tokens": True,
            "cached_tokens": True,
        },
        {
            "prompt_tokens": -1,
            "completion_tokens": -1,
            "cached_tokens": -1,
        },
        [],
    )

    for usage in invalid_usages:
        assert baseline_module.extract_provider_token_metrics(usage) == {}


def test_extract_reusable_token_metrics_returns_valid_display_fields():
    assert baseline_module.extract_reusable_token_metrics(
        {
            "output_tokens": 120,
            "cached_tokens": 250,
            "cache_hit_rate": 0.25,
            "input_tokens": 1000,
        }
    ) == {
        "output_tokens": 120,
        "cached_tokens": 250,
        "cache_hit_rate": 0.25,
    }


def test_extract_reusable_token_metrics_filters_invalid_fields():
    assert (
        baseline_module.extract_reusable_token_metrics(
            {
                "output_tokens": True,
                "cached_tokens": -1,
                "cache_hit_rate": 1.1,
            }
        )
        == {}
    )
    assert baseline_module.extract_reusable_token_metrics(
        {
            "output_tokens": 0,
            "cached_tokens": 0,
            "cache_hit_rate": False,
        }
    ) == {
        "output_tokens": 0,
        "cached_tokens": 0,
    }


def test_extract_session_total_output_tokens_prefers_accumulated_value():
    assert (
        baseline_module.extract_session_total_output_tokens(
            {
                "output_tokens": 20,
                "total_output_tokens": 200,
            }
        )
        == 200
    )


def test_extract_session_total_output_tokens_falls_back_to_legacy_value():
    assert (
        baseline_module.extract_session_total_output_tokens(
            {
                "output_tokens": 20,
                "total_output_tokens": -1,
            }
        )
        == 20
    )
    assert baseline_module.extract_session_total_output_tokens({"total_output_tokens": True}) == 0
    assert baseline_module.extract_session_total_output_tokens([]) == 0


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
