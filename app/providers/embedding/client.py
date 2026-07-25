from typing import Any

from app.core.constants import ERR_LLM_UNSUPPORTED_PROTOCOL
from app.core.exceptions import LLMException
from app.transformers.openai import OpenAIEmbeddingTransformer


class EmbeddingClient:
    _transformers = {
        "openai_embedding": OpenAIEmbeddingTransformer(),
    }

    @classmethod
    def get_transformer(cls, protocol: str) -> OpenAIEmbeddingTransformer:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            raise LLMException(ERR_LLM_UNSUPPORTED_PROTOCOL, protocol=protocol)
        return transformer

    @classmethod
    async def get_embeddings(
        cls,
        api_key: str,
        base_url: str,
        model_id: str,
        protocol: str,
        input_texts: str | list[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        transformer = cls.get_transformer(protocol)
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
        api_key: str,
        base_url: str,
        model_id: str,
        protocol: str,
        input_texts: list[str],
        batch_size: int = 16,
        dimensions: int | None = None,
        timeout: float = 30.0,
    ) -> list[list[float]]:
        transformer = cls.get_transformer(protocol)
        return await transformer.embed_texts(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            input_texts=input_texts,
            batch_size=batch_size,
            dimensions=dimensions,
            timeout=timeout,
        )
