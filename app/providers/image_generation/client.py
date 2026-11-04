from typing import Any

from app.core.constants import ERR_LLM_UNSUPPORTED_PROTOCOL
from app.core.exceptions import LLMException
from app.core.provider_usage import ProviderUsageSource, record_provider_usage
from app.models.provider_usage import ProviderUsageType
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
        usage_source: ProviderUsageSource | None = None,
        usage_type: ProviderUsageType = ProviderUsageType.IMAGE_GENERATION,
        usage_uid: str | None = None,
        usage_session_id: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        transformer = cls.get_transformer(protocol)
        response = await transformer.generate_image(
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
        response_model_id, input_tokens, cached_tokens, output_tokens, image_count = transformer.extract_usage(response)
        await record_provider_usage(
            usage_type=usage_type,
            source=usage_source if usage_source is not None else ProviderUsageSource(model_id=model_id, protocol=protocol),
            uid=usage_uid,
            session_id=usage_session_id,
            response_model_id=response_model_id,
            input_tokens=input_tokens,
            cached_tokens=cached_tokens,
            output_tokens=output_tokens,
            image_count=image_count,
        )
        return response
