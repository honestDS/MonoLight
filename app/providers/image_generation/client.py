from typing import Any

from app.core.constants import ERR_LLM_UNSUPPORTED_PROTOCOL
from app.core.exceptions import LLMException
from app.transformers.openai import OpenAIImageGenerationTransformer


class ImageGenerationClient:
    _transformers = {
        "openai_image": OpenAIImageGenerationTransformer(),
    }

    @classmethod
    def get_transformer(cls, protocol: str) -> OpenAIImageGenerationTransformer:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            raise LLMException(ERR_LLM_UNSUPPORTED_PROTOCOL, protocol=protocol)
        return transformer

    @classmethod
    async def generate_image(
        cls,
        api_key: str,
        base_url: str,
        model_id: str,
        protocol: str,
        prompt: str,
        size: str = "1024x1024",
        n: int = 1,
        quality: str | None = None,
        response_format: str | None = None,
        style: str | None = None,
        timeout: float = 60.0,
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        transformer = cls.get_transformer(protocol)
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
            http_proxy=http_proxy,
            custom_headers=custom_headers,
            **kwargs,
        )
