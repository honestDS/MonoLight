"""
OpenAI Embedding Transformer

适配 OpenAI Embeddings API 协议。
"""

from typing import List

import aiohttp

from app.core.exceptions import LLMException
from app.embedding.transformers.base import (
    BaseEmbeddingTransformer,
    EmbeddingResponse,
)


class OpenAIEmbeddingTransformer(BaseEmbeddingTransformer):
    """OpenAI Embeddings API 适配器"""

    async def embed(self, texts: List[str]) -> EmbeddingResponse:
        """
        调用 OpenAI Embeddings API 进行向量化

        Args:
            texts: 待向量化的文本列表

        Returns:
            EmbeddingResponse: 标准响应对象

        Raises:
            LLMException: API 调用失败时抛出
        """
        if not texts:
            raise ValueError("文本列表不能为空")

        if not self.config.api_key:
            raise ValueError("OpenAI API Key 未配置")

        base_url = self.config.base_url or "https://api.openai.com/v1"
        url = f"{base_url.rstrip('/')}/embeddings"

        headers = {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "input": texts,
            "model": self.config.model_id,
        }

        if self.config.dimensions:
            payload["dimensions"] = self.config.dimensions

        try:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        error_text = await resp.text()
                        raise LLMException(
                            f"OpenAI Embeddings API 调用失败 [{resp.status}]: {error_text}"
                        )

                    data = await resp.json()

        except aiohttp.ClientError as e:
            raise LLMException(f"OpenAI Embeddings API 网络请求失败: {str(e)}")
        except Exception as e:
            raise LLMException(f"OpenAI Embeddings API 调用异常: {str(e)}")

        embeddings = [item["embedding"] for item in data.get("data", [])]
        usage = data.get("usage", {})

        return EmbeddingResponse(
            embeddings=embeddings,
            model=data.get("model", self.config.model_id),
            usage=usage,
        )
