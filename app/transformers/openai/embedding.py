import json
from typing import Any

import aiohttp

from app.core.constants import (
    ERR_EMBEDDING_COUNT_MISMATCH,
    ERR_EMBEDDING_DIMENSION_MISMATCH,
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    ERR_PROFILE_EMBEDDING_CALL_FAILED,
)
from app.core.exceptions import EmbeddingException
from app.core.i18n import t
from app.core.log import get_logger

from ..base import BaseEmbeddingTransformer

logger = get_logger(__name__)


class OpenAIEmbeddingTransformer(BaseEmbeddingTransformer):
    async def get_embeddings(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: str | list[str],
        suppress_error_log: bool = False,
        timeout: float = 30.0,
        **kwargs,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model_id,
            "input": input_texts,
        }
        if "dimensions" in kwargs:
            payload["dimensions"] = kwargs["dimensions"]
        if "encoding_format" in kwargs:
            payload["encoding_format"] = kwargs["encoding_format"]
        if "user" in kwargs:
            payload["user"] = kwargs["user"]

        url = f"{self._normalize_embedding_base_url(base_url)}/embeddings"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise EmbeddingException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    return json.loads(txt)
        except EmbeddingException:
            raise
        except Exception as e:
            if suppress_error_log:
                logger.bind(model_id=model_id, base_url=base_url, fallback_candidate=True).warning(t("LOG_OPENAI_EMBEDDING_OPTIONAL_PARAMS_FAILED", error=str(e)))
            else:
                logger.bind(model_id=model_id, base_url=base_url).error(t("LOG_OPENAI_EMBEDDING_FAILED", error=str(e)))
            raise EmbeddingException(ERR_PROFILE_EMBEDDING_CALL_FAILED, message=str(e))

    async def embed_texts(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: list[str],
        batch_size: int = 16,
        dimensions: int | None = None,
        timeout: float = 30.0,
    ) -> list[list[float]]:
        if not input_texts:
            return []

        normalized_base_url = self._normalize_embedding_base_url(base_url)
        embeddings: list[list[float]] = []
        dimensions_supported: bool | None = None

        for start in range(0, len(input_texts), batch_size):
            batch_texts = input_texts[start : start + batch_size]
            result = await self._get_embedding_batch_with_dimension_fallback(
                api_key=api_key,
                base_url=normalized_base_url,
                model_id=model_id,
                input_texts=batch_texts,
                dimensions=dimensions,
                dimensions_supported=dimensions_supported,
                timeout=timeout,
            )

            dimensions_supported = result["dimensions_supported"]
            batch_embeddings = [item["embedding"] for item in result["response"].get("data", [])]

            if len(batch_embeddings) != len(batch_texts):
                raise EmbeddingException(ERR_EMBEDDING_COUNT_MISMATCH)
            if dimensions and dimensions_supported is True and batch_embeddings and len(batch_embeddings[0]) != dimensions:
                raise EmbeddingException(ERR_EMBEDDING_DIMENSION_MISMATCH, actual=len(batch_embeddings[0]), expected=dimensions)

            embeddings.extend(batch_embeddings)

        return embeddings

    async def _get_embedding_batch_with_dimension_fallback(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: list[str],
        dimensions: int | None,
        dimensions_supported: bool | None,
        timeout: float = 30.0,
    ) -> dict[str, Any]:
        if dimensions and dimensions_supported is not False:
            try:
                response = await self.get_embeddings(
                    api_key=api_key,
                    base_url=base_url,
                    model_id=model_id,
                    input_texts=input_texts,
                    suppress_error_log=dimensions_supported is None,
                    dimensions=dimensions,
                    timeout=timeout,
                )
                return {"response": response, "dimensions_supported": True}
            except EmbeddingException:
                if dimensions_supported is True:
                    raise

        response = await self.get_embeddings(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            input_texts=input_texts,
            timeout=timeout,
        )
        return {"response": response, "dimensions_supported": False if dimensions else dimensions_supported}

    @staticmethod
    def _normalize_embedding_base_url(base_url: str) -> str:
        return base_url.rstrip("/").removesuffix("/embeddings")
