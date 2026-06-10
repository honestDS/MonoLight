from typing import Any

from app.core import constants
from app.core.exceptions import LLMException
from app.models.provider import ProviderType
from app.transformers.base import BaseEmbeddingTransformer
from app.transformers.openai import OpenAITransformer


class EmbeddingClient:
    _transformers: dict[str, BaseEmbeddingTransformer] = {
        ProviderType.OPENAI.value.lower(): OpenAITransformer(),
    }

    @classmethod
    def get_transformer(cls, provider_type: ProviderType | str) -> BaseEmbeddingTransformer:
        transformer = cls._transformers.get(str(provider_type).lower())
        if not transformer:
            raise LLMException(f"{constants.ERR_LLM_UNEXPECTED_ERROR}: Unsupported embedding provider {provider_type}")
        return transformer

    @classmethod
    async def get_embeddings(
        cls,
        provider_type: ProviderType | str,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: str | list[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        transformer = cls.get_transformer(provider_type)
        return await transformer.get_embeddings(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            input_texts=input_texts,
            **kwargs,
        )

    @classmethod
    async def embed_texts(
        cls,
        provider_type: ProviderType | str,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: list[str],
        batch_size: int = 16,
        dimensions: int | None = None,
        timeout: float = 30.0,
    ) -> list[list[float]]:
        transformer = cls.get_transformer(provider_type)
        return await transformer.embed_texts(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            input_texts=input_texts,
            batch_size=batch_size,
            dimensions=dimensions,
            timeout=timeout,
        )

