import json
from typing import Any

import aiohttp

from app.core.constants import (
    ERR_LLM_API_RESPONSE_ERROR,
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    ERR_LLM_CONNECTION_FAILED,
)
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils import llm_request_policy
from app.core.utils.http_proxy import build_aiohttp_proxy_kwargs
from app.core.utils.model_request_headers import build_model_request_headers

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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        reference_images: list[tuple[str, bytes, str]] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        is_edit = bool(reference_images)
        headers = build_model_request_headers(api_key, custom_headers)
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
        if is_edit:
            optional_fields["input_fidelity"] = kwargs.get("input_fidelity")
        payload.update({key: value for key, value in optional_fields.items() if value is not None})

        extra_body = kwargs.get("extra_body")
        if isinstance(extra_body, dict):
            payload.update(extra_body)

        if is_edit:
            payload.pop("style", None)

        url = f"{self._normalize_image_base_url(base_url)}/images/{'edits' if is_edit else 'generations'}"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            if is_edit:
                headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
                form = aiohttp.FormData()
                file_field = "image" if len(reference_images) == 1 else "image[]"
                for key, value in payload.items():
                    if key not in {"image", "image[]"}:
                        form.add_field(key, str(value))
                for filename, data, mime_type in reference_images:
                    form.add_field(file_field, data, filename=filename, content_type=mime_type)
                request_data = form()
                # FormData's payload size includes fields, file headers, files, and boundaries.
                max_size_bytes = await llm_request_policy.load_llm_request_max_bytes()
                llm_request_policy.validate_llm_request_size(request_data.size, max_size_bytes=max_size_bytes)
            else:
                request_data = await llm_request_policy.prepare_llm_json_request(payload)

            proxy_kwargs = build_aiohttp_proxy_kwargs(http_proxy)
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                request_kwargs = {"headers": headers, "data": request_data, **proxy_kwargs}

                async with session.post(url, **request_kwargs) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        rejection = llm_request_policy.build_upstream_rejection(status=resp.status, detail=txt)
                        if rejection is not None:
                            raise rejection
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    result = json.loads(txt)
                    if not isinstance(result, dict):
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR)
                    return result
        except LLMException:
            raise
        except Exception as e:
            logger.bind(model_id=model_id, base_url=base_url).error(t("LOG_OPENAI_IMAGE_GENERATION_FAILED", error=str(e)))
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(e))

    @staticmethod
    def _normalize_image_base_url(base_url: str) -> str:
        normalized = base_url.rstrip("/")
        for suffix in ("/images/edits", "/images/generations", "/images"):
            if normalized.endswith(suffix):
                return normalized[: -len(suffix)].rstrip("/")
        return normalized
