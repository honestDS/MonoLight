import pytest

from app.core.exceptions import LLMException
from app.models.provider import ProviderType
from app.providers.embedding import EmbeddingClient


class StubEmbeddingTransformer:
    async def get_embeddings(self, **kwargs):
        return {"called": "get_embeddings", "kwargs": kwargs}

    async def embed_texts(self, **kwargs):
        return [[1.0, 2.0, 3.0]]


@pytest.fixture
def stub_embedding_transformers(monkeypatch):
    monkeypatch.setattr(
        EmbeddingClient,
        "_transformers",
        {ProviderType.OPENAI.value.lower(): StubEmbeddingTransformer()},
    )


@pytest.mark.asyncio
async def test_embedding_client_get_embeddings_dispatches_by_provider_type(stub_embedding_transformers):
    result = await EmbeddingClient.get_embeddings(
        provider_type=ProviderType.OPENAI,
        api_key="key",
        base_url="https://example.com/v1",
        model_id="embedding-model",
        input_texts=["hello"],
        dimensions=1024,
    )

    assert result["called"] == "get_embeddings"
    assert result["kwargs"]["api_key"] == "key"
    assert result["kwargs"]["dimensions"] == 1024


@pytest.mark.asyncio
async def test_embedding_client_embed_texts_dispatches_by_provider_type_string(stub_embedding_transformers):
    result = await EmbeddingClient.embed_texts(
        provider_type="openai",
        api_key="key",
        base_url="https://example.com/v1",
        model_id="embedding-model",
        input_texts=["hello"],
        batch_size=8,
        dimensions=512,
    )

    assert result == [[1.0, 2.0, 3.0]]


def test_embedding_client_rejects_unsupported_provider():
    with pytest.raises(LLMException) as exc_info:
        EmbeddingClient.get_transformer("unsupported")

    assert "Unsupported embedding provider unsupported" in str(exc_info.value)
