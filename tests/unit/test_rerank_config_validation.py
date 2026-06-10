import pytest
from pydantic import ValidationError

from app.models.profile import ProfileConfig, ProviderConfig
from app.models.provider import ModelUsage, ProviderBase, ProviderType


def test_provider_config_rerank_defaults():
    # 缺省时未配置 rerank 提供商与模型（视为未启用），默认值填充
    cfg = ProviderConfig(model_id="gpt-4o")
    assert cfg.rerank_provider_id is None
    assert cfg.rerank_model_id is None
    assert cfg.rerank_candidate_k == 20
    assert cfg.rerank_timeout == 15.0


def test_provider_config_rerank_candidate_k_upper_bound():
    # rerank_candidate_k 上限 50
    with pytest.raises(ValidationError):
        ProviderConfig(model_id="gpt-4o", rerank_candidate_k=51)


def test_profile_config_fills_rerank_defaults_for_legacy_configs():
    # 存量 Profile 不含 rerank 键时，model_validate 应按默认值补齐
    legacy = {"provider": {"model_id": "gpt-4o"}}
    cfg = ProfileConfig.model_validate(legacy)
    assert cfg.provider.rerank_provider_id is None
    assert cfg.provider.rerank_candidate_k == 20


def test_rerank_provider_requires_base_url():
    # usage 为 RERANK 时 base_url 必填
    with pytest.raises(ValidationError):
        ProviderBase(
            name="rerank-provider",
            provider_type=ProviderType.OPENAI,
            usage=ModelUsage.RERANK,
            api_key="fake-key",
            base_url=None,
        )


def test_rerank_provider_with_base_url_ok():
    provider = ProviderBase(
        name="rerank-provider",
        provider_type=ProviderType.OPENAI,
        usage=ModelUsage.RERANK,
        api_key="fake-key",
        base_url="https://api.example.com/v1",
    )
    assert provider.usage == ModelUsage.RERANK
    assert provider.base_url == "https://api.example.com/v1"
