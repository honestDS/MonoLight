import pytest

from app.core.rerank.knowledge_base import rerank_retrieval_hits
from app.core.rerank.schemas import RerankConfig
from app.core.retrieval.schemas import RetrievalHit
from app.models.provider import ProviderType
from app.providers.rerank.client import RERANK_MAX_DOCUMENT_CHARS, RerankClient


def _make_config() -> RerankConfig:
    return RerankConfig(
        provider_type=ProviderType.OPENAI,
        api_key="fake-key",
        base_url="https://api.example.com/v1",
        model_id="rerank-model",
        candidate_k=20,
        timeout=15.0,
    )


def _make_hits(n: int) -> list[RetrievalHit]:
    return [RetrievalHit(id=f"id-{i}", content=f"片段{i}", metadata={}, fusion_score=1.0 / (i + 1)) for i in range(n)]


@pytest.mark.asyncio
async def test_rerank_client_truncates_long_documents(monkeypatch):
    captured = {}

    async def fake_rerank_texts(self, **kwargs):
        captured["documents"] = kwargs["documents"]
        return [{"index": 0, "relevance_score": 0.5}]

    monkeypatch.setattr("app.transformers.openai.OpenAITransformer.rerank_texts", fake_rerank_texts)

    long_doc = "啊" * (RERANK_MAX_DOCUMENT_CHARS + 100)
    results = await RerankClient.rerank_texts(
        provider_type=ProviderType.OPENAI,
        api_key="fake-key",
        base_url="https://api.example.com/v1",
        model_id="rerank-model",
        query="问题",
        documents=[long_doc],
    )

    assert len(captured["documents"][0]) == RERANK_MAX_DOCUMENT_CHARS
    assert results[0].index == 0
    assert results[0].relevance_score == 0.5


@pytest.mark.asyncio
async def test_rerank_client_empty_documents_short_circuit():
    results = await RerankClient.rerank_texts(
        provider_type=ProviderType.OPENAI,
        api_key="fake-key",
        base_url="https://api.example.com/v1",
        model_id="rerank-model",
        query="问题",
        documents=[],
    )
    assert results == []


@pytest.mark.asyncio
async def test_rerank_retrieval_hits_backfills_scores_and_order(monkeypatch):
    from app.core.rerank.schemas import RerankResult

    hits = _make_hits(3)

    async def fake_client_rerank(**kwargs):
        # 倒序排列：index 2 最相关
        return [
            RerankResult(index=2, relevance_score=0.95),
            RerankResult(index=0, relevance_score=0.80),
        ]

    monkeypatch.setattr(RerankClient, "rerank_texts", staticmethod(fake_client_rerank))

    reranked = await rerank_retrieval_hits(_make_config(), "问题", hits, final_top_k=3)

    # 命中的两条按 relevance_score 降序在前，未命中的 index 1 按原顺序补齐
    assert reranked[0].id == "id-2"
    assert reranked[0].rerank_score == 0.95
    assert reranked[0].rerank_rank == 1
    assert reranked[1].id == "id-0"
    assert reranked[1].rerank_rank == 2
    assert reranked[2].id == "id-1"
    assert reranked[2].rerank_score is None
    assert reranked[2].rerank_rank is None


@pytest.mark.asyncio
async def test_rerank_retrieval_hits_empty_results_fallback(monkeypatch):
    hits = _make_hits(3)

    async def fake_client_rerank(**kwargs):
        return []

    monkeypatch.setattr(RerankClient, "rerank_texts", staticmethod(fake_client_rerank))

    reranked = await rerank_retrieval_hits(_make_config(), "问题", hits, final_top_k=3)

    # 空结果回退原 RRF 顺序
    assert [h.id for h in reranked] == ["id-0", "id-1", "id-2"]


@pytest.mark.asyncio
async def test_rerank_retrieval_hits_drops_illegal_index(monkeypatch):
    from app.core.rerank.schemas import RerankResult

    hits = _make_hits(2)

    async def fake_client_rerank(**kwargs):
        # 越界 index=5 与重复 index=0 应被丢弃
        return [
            RerankResult(index=5, relevance_score=0.99),
            RerankResult(index=0, relevance_score=0.90),
            RerankResult(index=0, relevance_score=0.10),
        ]

    monkeypatch.setattr(RerankClient, "rerank_texts", staticmethod(fake_client_rerank))

    reranked = await rerank_retrieval_hits(_make_config(), "问题", hits, final_top_k=2)

    assert reranked[0].id == "id-0"
    assert reranked[0].rerank_rank == 1
    # 剩余未命中的 id-1 补齐
    assert reranked[1].id == "id-1"
    assert reranked[1].rerank_score is None
