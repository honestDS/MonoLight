import json
from typing import Any

import aiohttp

from app.core.constants import ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, ERR_LLM_CONNECTION_FAILED
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger

from ..base import BaseImageGenerationTransformer

logger = get_logger(__name__)


class OpenAIImageGenerationTransformer(BaseImageGenerationTransformer):
    async def generate_image(
        self,
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
        **kwargs,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "n": n,
            "size": size,
        }

        optional_fields = {
            "quality": quality,
            "response_format": response_format,
            "style": style,
            "user": kwargs.get("user"),
            "background": kwargs.get("background"),
            "moderation": kwargs.get("moderation"),
            "output_compression": kwargs.get("output_compression"),
            "output_format": kwargs.get("output_format"),
        }
        payload.update({key: value for key, value in optional_fields.items() if value is not None})

        extra_body = kwargs.get("extra_body")
        if isinstance(extra_body, dict):
            payload.update(extra_body)

        url = f"{self._normalize_image_base_url(base_url)}/images/generations"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    return json.loads(txt)
        except LLMException:
            raise
        except Exception as e:
            logger.bind(model_id=model_id, base_url=base_url).error(t("LOG_OPENAI_IMAGE_GENERATION_FAILED", error=str(e)))
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(e))

    @staticmethod
    def _normalize_image_base_url(base_url: str) -> str:
        return base_url.rstrip("/").removesuffix("/images/generations").removesuffix("/images")
