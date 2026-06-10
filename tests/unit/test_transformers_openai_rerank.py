import json

import pytest

from app.core.exceptions import LLMException
from app.transformers.openai import OpenAITransformer


def test_normalize_rerank_base_url_variants():
    transformer = OpenAITransformer()
    # 根路径、/v1、/v1/rerank 三种输入统一归一化为不含 /rerank 后缀的基础路径
    assert transformer.normalize_rerank_base_url("https://api.example.com") == "https://api.example.com"
    assert transformer.normalize_rerank_base_url("https://api.example.com/v1") == "https://api.example.com/v1"
    assert transformer.normalize_rerank_base_url("https://api.example.com/v1/rerank") == "https://api.example.com/v1"
    assert transformer.normalize_rerank_base_url("https://api.example.com/v1/rerank/") == "https://api.example.com/v1"


def _build_mock_session(status, payload_obj, captured):
    class MockResponse:
        def __init__(self):
            self.status = status

        async def text(self):
            return json.dumps(payload_obj)

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    class MockSession:
        def __init__(self, *args, **kwargs):
            captured["session_kwargs"] = kwargs

        def post(self, url, **kwargs):
            captured["url"] = url
            captured["json"] = kwargs.get("json")
            return MockResponse()

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            pass

    return MockSession


@pytest.mark.asyncio
async def test_rerank_texts_parses_index_and_relevance_score(monkeypatch):
    captured = {}
    payload = {
        "results": [
            {"index": 1, "relevance_score": 0.9},
            {"index": 0, "relevance_score": 0.4},
        ]
    }
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", _build_mock_session(200, payload, captured))

    transformer = OpenAITransformer()
    results = await transformer.rerank_texts(
        api_key="fake-key",
        base_url="https://api.example.com/v1",
        model_id="rerank-model",
        query="问题",
        documents=["片段0", "片段1"],
        top_n=2,
    )

    assert results == [
        {"index": 1, "relevance_score": 0.9},
        {"index": 0, "relevance_score": 0.4},
    ]
    assert captured["url"] == "https://api.example.com/v1/rerank"
    assert captured["json"]["top_n"] == 2


@pytest.mark.asyncio
async def test_rerank_texts_score_fallback(monkeypatch):
    captured = {}
    # 缺少 relevance_score 时兼容 score 后备字段
    payload = {"results": [{"index": 0, "score": 0.7}]}
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", _build_mock_session(200, payload, captured))

    transformer = OpenAITransformer()
    results = await transformer.rerank_texts(
        api_key="fake-key",
        base_url="https://api.example.com/v1",
        model_id="rerank-model",
        query="问题",
        documents=["片段0"],
    )

    assert results == [{"index": 0, "relevance_score": 0.7}]


@pytest.mark.asyncio
async def test_rerank_texts_empty_documents_short_circuit(monkeypatch):
    transformer = OpenAITransformer()
    results = await transformer.rerank_texts(
        api_key="fake-key",
        base_url="https://api.example.com/v1",
        model_id="rerank-model",
        query="问题",
        documents=[],
    )
    assert results == []


@pytest.mark.asyncio
async def test_rerank_texts_http_error_raises_llm_exception(monkeypatch):
    captured = {}
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", _build_mock_session(500, {"error": "boom"}, captured))

    transformer = OpenAITransformer()
    with pytest.raises(LLMException):
        await transformer.rerank_texts(
            api_key="fake-key",
            base_url="https://api.example.com/v1",
            model_id="rerank-model",
            query="问题",
            documents=["片段0"],
        )


@pytest.mark.asyncio
async def test_rerank_texts_bad_format_raises_llm_exception(monkeypatch):
    captured = {}
    # 缺少 results 列表，应抛 LLMException
    import aiohttp

    monkeypatch.setattr(aiohttp, "ClientSession", _build_mock_session(200, {"foo": "bar"}, captured))

    transformer = OpenAITransformer()
    with pytest.raises(LLMException):
        await transformer.rerank_texts(
            api_key="fake-key",
            base_url="https://api.example.com/v1",
            model_id="rerank-model",
            query="问题",
            documents=["片段0"],
        )
