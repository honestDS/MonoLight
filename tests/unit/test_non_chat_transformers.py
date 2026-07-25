import pytest

from app.core.constants import ERR_LLM_UNSUPPORTED_PROTOCOL
from app.core.exceptions import LLMException
from app.providers.embedding.client import EmbeddingClient
from app.providers.image_generation.client import ImageGenerationClient
from app.providers.rerank.client import RerankClient
from app.transformers.base import BaseEmbeddingTransformer, BaseImageGenerationTransformer, BaseRerankTransformer
from app.transformers.cohere_rerank import CohereRerankTransformer
from app.transformers.openai import (
    OpenAIChatCompletionsTransformer,
    OpenAIEmbeddingTransformer,
    OpenAIImageGenerationTransformer,
    OpenAIResponsesTransformer,
)


def test_non_chat_clients_bind_dedicated_transformers() -> None:
    assert isinstance(EmbeddingClient.get_transformer("openai_embedding"), OpenAIEmbeddingTransformer)
    assert isinstance(ImageGenerationClient.get_transformer("openai_image"), OpenAIImageGenerationTransformer)
    assert isinstance(RerankClient.get_transformer("cohere_rerank"), CohereRerankTransformer)


@pytest.mark.parametrize("transformer", [OpenAIChatCompletionsTransformer(), OpenAIResponsesTransformer()])
def test_chat_transformers_do_not_implement_non_chat_protocols(transformer: OpenAIChatCompletionsTransformer) -> None:
    assert not isinstance(transformer, BaseEmbeddingTransformer)
    assert not isinstance(transformer, BaseImageGenerationTransformer)
    assert not isinstance(transformer, BaseRerankTransformer)


@pytest.mark.parametrize(
    "client",
    [EmbeddingClient, ImageGenerationClient, RerankClient],
)
def test_non_chat_clients_reject_unsupported_protocol(client) -> None:
    with pytest.raises(LLMException) as exc_info:
        client.get_transformer("unsupported")

    assert exc_info.value.message == ERR_LLM_UNSUPPORTED_PROTOCOL


@pytest.mark.asyncio
async def test_rerank_empty_documents_returns_before_protocol_selection() -> None:
    results = await RerankClient.rerank_texts(
        api_key="test-key",
        base_url="https://example.invalid",
        model_id="test-model",
        protocol="unsupported",
        query="query",
        documents=[],
    )

    assert results == []
