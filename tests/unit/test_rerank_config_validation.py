import pytest
from pydantic import ValidationError

from app.models.channel import ChannelConfig
from app.models.profile import ProfileConfig, ProviderConfig
from app.models.provider import ModelUsage, ProviderBase, ProviderType, ProviderUpdate, validate_provider_model_ids


def test_provider_config_channel_defaults():
    cfg = ProviderConfig()
    assert cfg.chat_channel is None
    assert cfg.embedding_channel is None
    assert cfg.rerank_channel is None


def test_rerank_channel_candidate_k_upper_bound():
    with pytest.raises(ValidationError):
        ChannelConfig(rerank_candidate_k=51)


def test_profile_config_fills_channel_defaults_for_legacy_configs():
    legacy = {"provider": {"model_id": "gpt-4o"}}
    cfg = ProfileConfig.model_validate(legacy)
    assert cfg.provider.chat_channel is None
    assert cfg.provider.embedding_channel is None
    assert cfg.provider.rerank_channel is None


def test_rerank_model_entry_without_base_url_is_deferred_to_api_validation():
    provider = ProviderBase(
        name="rerank-provider",
        provider_type=ProviderType.OPENAI,
        api_key="fake-key",
        base_url=None,
        model_ids=[{"model_id": "rerank-model", "usage": ModelUsage.RERANK}],
    )

    assert provider.base_url is None
    assert provider.model_ids[0]["usage"] == ModelUsage.RERANK


def test_rerank_model_entry_with_base_url_ok():
    provider = ProviderBase(
        name="rerank-provider",
        provider_type=ProviderType.OPENAI,
        api_key="fake-key",
        base_url="https://api.example.com/v1",
        model_ids=[{"model_id": "rerank-model", "usage": ModelUsage.RERANK}],
    )
    assert provider.model_ids[0]["usage"] == ModelUsage.RERANK
    assert provider.base_url == "https://api.example.com/v1"


def test_provider_update_allows_same_model_id_for_different_usage():
    update = ProviderUpdate(
        model_ids=[
            {"model_id": "gpt-4o", "usage": ModelUsage.CHAT},
            {"model_id": "gpt-4o", "usage": ModelUsage.EMBEDDING},
        ]
    )

    assert len(update.model_ids) == 2


def test_provider_model_id_validation_reports_duplicate_model_id_in_same_usage():
    error_key, error_kwargs = validate_provider_model_ids(
        [
            {"model_id": "gpt-4o", "usage": ModelUsage.CHAT},
            {"model_id": "gpt-4o", "usage": ModelUsage.CHAT},
        ]
    )

    assert error_key == "ERR_PROVIDER_MODEL_IDS_DUPLICATED"
    assert error_kwargs == {"usage": ModelUsage.CHAT.value, "model_id": "gpt-4o"}

