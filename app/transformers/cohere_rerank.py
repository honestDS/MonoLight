import json
from collections.abc import Awaitable, Callable
from typing import Any

import aiohttp

from app.core.constants import (
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    ERR_PROFILE_RERANK_CALL_FAILED,
    ERR_RERANK_FORMAT_ERROR,
)
from app.core.exceptions import RerankException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.http_proxy import build_aiohttp_proxy_kwargs
from app.core.utils.model_request_headers import build_model_request_headers

from .base import BaseRerankTransformer, log_upstream_usage

logger = get_logger(__name__)


class CohereRerankTransformer(BaseRerankTransformer):
    @staticmethod
    def extract_usage(response: dict[str, Any]) -> tuple[str | None, int | None]:
        model = response.get("model")
        model = model if isinstance(model, str) else None

        search_units = None
        meta = response.get("meta")
        if isinstance(meta, dict):
            billed_units = meta.get("billed_units")
            if isinstance(billed_units, dict):
                candidate = billed_units.get("search_units")
                if isinstance(candidate, int) and not isinstance(candidate, bool) and candidate >= 0:
                    search_units = candidate

        return model, search_units

    async def get_rerank(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        timeout: float = 15.0,
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        headers = build_model_request_headers(api_key, custom_headers)
        payload: dict[str, Any] = {
            "model": model_id,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = top_n

        url = f"{self._normalize_rerank_base_url(base_url)}/rerank"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            proxy_kwargs = build_aiohttp_proxy_kwargs(http_proxy)
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload, **proxy_kwargs) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise RerankException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    response = json.loads(txt)
                    if isinstance(response, dict):
                        meta = response.get("meta")
                        billed_units = meta.get("billed_units") if isinstance(meta, dict) else None
                        tokens = meta.get("tokens") if isinstance(meta, dict) else None
                        results = response.get("results")
                        log_upstream_usage(
                            model_id=model_id,
                            protocol="cohere_rerank",
                            blocks={
                                "usage": response.get("usage"),
                                "billed_units": billed_units,
                                "tokens": tokens,
                            },
                            result_count=len(results) if isinstance(results, list) else None,
                        )
                    return response
        except RerankException:
            raise
        except Exception as e:
            logger.bind(model_id=model_id, base_url=base_url).error(t("LOG_RERANK_FAILED", error=str(e)))
            raise RerankException(ERR_PROFILE_RERANK_CALL_FAILED, params={"message": str(e)})

    async def rerank_texts(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        timeout: float = 15.0,
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        on_usage: Callable[[str | None, int | None], Awaitable[None]] | None = None,
    ) -> list[dict[str, Any]]:
        if not documents:
            return []

        response = await self.get_rerank(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            query=query,
            documents=documents,
            top_n=top_n,
            timeout=timeout,
            http_proxy=http_proxy,
            custom_headers=custom_headers,
        )

        if on_usage is not None:
            await on_usage(*self.extract_usage(response))

        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise RerankException(ERR_RERANK_FORMAT_ERROR)

        normalized: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            if index is None:
                continue
            # 优先取 relevance_score，缺失时兼容 score 后备字段
            score = item.get("relevance_score")
            if score is None:
                score = item.get("score")
            if score is None:
                continue
            normalized.append({"index": int(index), "relevance_score": float(score)})

        return normalized

    @staticmethod
    def _normalize_rerank_base_url(base_url: str) -> str:
        # 允许用户把 base_url 配到服务根路径、/v1 或 /v1/rerank，统一归一化为不含 /rerank 后缀的基础路径
        return base_url.rstrip("/").removesuffix("/rerank")
