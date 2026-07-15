from typing import Any

from app.core.constants import ERR_LLM_UNSUPPORTED_IMAGE_GENERATION_CHANNEL
from app.core.exceptions import LLMException
from app.models.channel import ChannelType
from app.transformers.base import BaseImageGenerationTransformer
from app.transformers.openai import OpenAITransformer


class ImageGenerationClient:
    _transformers: dict[str, BaseImageGenerationTransformer] = {
        ChannelType.OPENAI.value.lower(): OpenAITransformer(),
    }

    @classmethod
    def get_transformer(cls, channel_type: ChannelType | str) -> BaseImageGenerationTransformer:
        transformer = cls._transformers.get(str(channel_type).lower())
        if not transformer:
            raise LLMException(message=ERR_LLM_UNSUPPORTED_IMAGE_GENERATION_CHANNEL, channel_type=channel_type)
        return transformer

    @classmethod
    async def generate_image(
        cls,
        channel_type: ChannelType | str,
        api_key: str,
        base_url: str,
        model_id: str,
        prompt: str,
        size: str = "1024x1024",
        n: int = 1,
        quality: str | None = None,
        response_format: str | None = None,
        style: str | None = None,
        timeout: float = 60.0,
        **kwargs: Any,
    ) -> dict[str, Any]:
        transformer = cls.get_transformer(channel_type)
        return await transformer.generate_image(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            prompt=prompt,
            size=size,
            n=n,
            quality=quality,
            response_format=response_format,
            style=style,
            timeout=timeout,
            **kwargs,
        )
