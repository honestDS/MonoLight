from typing import Any

from app.core.constants import ERR_LLM_UNSUPPORTED_EMBEDDING_CHANNEL
from app.core.exceptions import LLMException
from app.models.channel import ChannelType
from app.transformers.base import BaseEmbeddingTransformer
from app.transformers.openai import OpenAITransformer
from app.transformers.openai_responses import OpenAIResponsesTransformer


class EmbeddingClient:
    _transformers: dict[str, BaseEmbeddingTransformer] = {
        ChannelType.OPENAI.value.lower(): OpenAITransformer(),
        ChannelType.OPENAI_RESPONSES.value.lower(): OpenAIResponsesTransformer(),
    }

    @classmethod
    def get_transformer(cls, channel_type: ChannelType | str) -> BaseEmbeddingTransformer:
        transformer = cls._transformers.get(str(channel_type).lower())
        if not transformer:
            raise LLMException(message=ERR_LLM_UNSUPPORTED_EMBEDDING_CHANNEL, channel_type=channel_type)
        return transformer

    @classmethod
    async def get_embeddings(
        cls,
        channel_type: ChannelType | str,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: str | list[str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        transformer = cls.get_transformer(channel_type)
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
        channel_type: ChannelType | str,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: list[str],
        batch_size: int = 16,
        dimensions: int | None = None,
        timeout: float = 30.0,
    ) -> list[list[float]]:
        transformer = cls.get_transformer(channel_type)
        return await transformer.embed_texts(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            input_texts=input_texts,
            batch_size=batch_size,
            dimensions=dimensions,
            timeout=timeout,
        )
