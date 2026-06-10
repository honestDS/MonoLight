import pytest

from app.api.v1.profile import validate_rerank_provider
from app.core.exceptions import ParameterException
from app.models.provider import ModelUsage


class _FakeProvider:
    def __init__(self, usage):
        self.usage = usage


class _FakeProviderCrud:
    def __init__(self, provider):
        self._provider = provider

    async def get(self, db, provider_id):
        return self._provider


@pytest.mark.asyncio
async def test_validate_rerank_skips_when_not_configured():
    # 未配置 rerank 提供商与模型时视为未启用，直接通过不做任何校验
    await validate_rerank_provider(db=None, provider_config={})


@pytest.mark.asyncio
async def test_validate_rerank_rejects_partial_config():
    # 仅配置其一（缺模型 ID）视为配置不完整，应拦截
    config = {"rerank_provider_id": 1}
    with pytest.raises(ParameterException):
        await validate_rerank_provider(db=None, provider_config=config)


@pytest.mark.asyncio
async def test_validate_rerank_rejects_candidate_k_less_than_top_k(monkeypatch):
    # 候选数量 K 小于知识库返回数量时应拦截
    monkeypatch.setattr(
        "app.api.v1.profile.provider_crud",
        _FakeProviderCrud(_FakeProvider(ModelUsage.RERANK)),
    )
    config = {
        "rerank_provider_id": 1,
        "rerank_model_id": "rerank-model-id",
        "rerank_candidate_k": 3,
        "kb_query_top_k": 5,
    }
    with pytest.raises(ParameterException):
        await validate_rerank_provider(db=None, provider_config=config)


@pytest.mark.asyncio
async def test_validate_rerank_accepts_candidate_k_ge_top_k(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.profile.provider_crud",
        _FakeProviderCrud(_FakeProvider(ModelUsage.RERANK)),
    )
    config = {
        "rerank_provider_id": 1,
        "rerank_model_id": "rerank-model-id",
        "rerank_candidate_k": 20,
        "kb_query_top_k": 5,
    }
    # 候选数量 K >= 返回数量，校验通过不抛异常
    await validate_rerank_provider(db=None, provider_config=config)


@pytest.mark.asyncio
async def test_validate_rerank_rejects_wrong_usage(monkeypatch):
    monkeypatch.setattr(
        "app.api.v1.profile.provider_crud",
        _FakeProviderCrud(_FakeProvider(ModelUsage.CHAT)),
    )
    config = {
        "rerank_provider_id": 1,
        "rerank_model_id": "rerank-model-id",
        "rerank_candidate_k": 20,
        "kb_query_top_k": 5,
    }
    with pytest.raises(ParameterException):
        await validate_rerank_provider(db=None, provider_config=config)
