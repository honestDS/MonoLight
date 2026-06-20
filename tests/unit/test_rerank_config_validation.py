import pytest
from pydantic import ValidationError

from app.models.channel import ChannelConfig, ChannelBase, ChannelType, ChannelUpdate, ModelUsage, validate_channel_model_ids
from app.models.profile import ProfileConfig, ChannelGroupConfig


def test_channel_group_config_defaults():
    cfg = ChannelGroupConfig()
    assert cfg.chat_channel is None
    assert cfg.embedding_channel is None
    assert cfg.rerank_channel is None


def test_rerank_channel_candidate_k_upper_bound():
    with pytest.raises(ValidationError):
        ChannelConfig(rerank_candidate_k=51)


def test_profile_config_fills_channel_defaults_for_legacy_configs():
    legacy = {"channel": {"chat_channel": None}}
    cfg = ProfileConfig.model_validate(legacy)
    assert cfg.channel.chat_channel is None
    assert cfg.channel.embedding_channel is None
    assert cfg.channel.rerank_channel is None


def test_rerank_model_entry_without_base_url_is_deferred_to_api_validation():
    channel = ChannelBase(
        name="rerank-channel",
        channel_type=ChannelType.OPENAI,
        api_key="fake-key",
        base_url=None,
        model_ids=[{"model_id": "rerank-model", "usage": ModelUsage.RERANK}],
    )

    assert channel.base_url is None
    assert channel.model_ids[0]["usage"] == ModelUsage.RERANK


def test_rerank_model_entry_with_base_url_ok():
    channel = ChannelBase(
        name="rerank-channel",
        channel_type=ChannelType.OPENAI,
        api_key="fake-key",
        base_url="https://api.example.com/v1",
        model_ids=[{"model_id": "rerank-model", "usage": ModelUsage.RERANK}],
    )
    assert channel.model_ids[0]["usage"] == ModelUsage.RERANK
    assert channel.base_url == "https://api.example.com/v1"


def test_channel_update_allows_same_model_id_for_different_usage():
    update = ChannelUpdate(
        model_ids=[
            {"model_id": "gpt-4o", "usage": ModelUsage.CHAT},
            {"model_id": "gpt-4o", "usage": ModelUsage.EMBEDDING},
        ]
    )

    assert len(update.model_ids) == 2


def test_channel_model_id_validation_reports_duplicate_model_id_in_same_usage():
    error_key, error_kwargs = validate_channel_model_ids(
        [
            {"model_id": "gpt-4o", "usage": ModelUsage.CHAT},
            {"model_id": "gpt-4o", "usage": ModelUsage.CHAT},
        ]
    )

    assert error_key == "ERR_CHANNEL_MODEL_IDS_DUPLICATED"
    assert error_kwargs == {"usage": ModelUsage.CHAT.value, "model_id": "gpt-4o"}

