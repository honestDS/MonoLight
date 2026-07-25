import json
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

from .base import BaseRerankTransformer

logger = get_logger(__name__)


class CohereRerankTransformer(BaseRerankTransformer):
    async def get_rerank(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        query: str,
        documents: list[str],
        top_n: int | None = None,
        timeout: float = 15.0,
        **kwargs,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
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
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise RerankException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    return json.loads(txt)
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
        )

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
