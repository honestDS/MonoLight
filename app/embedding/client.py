"""
Embedding 客户端

提供统一的向量化调用接口，根据配置动态加载对应的 Transformer。
"""


from app.embedding.config import EmbeddingConfig
from app.embedding.transformers.base import (
    BaseEmbeddingTransformer,
    EmbeddingResponse,
)


class EmbeddingClient:
    """Embedding 统一客户端"""

    def __init__(self, config: EmbeddingConfig):
        self.config = config
        self.transformer = self._load_transformer()

    def _load_transformer(self) -> BaseEmbeddingTransformer:
        """根据配置加载对应的 Transformer"""
        provider_type = self.config.provider_type.lower()

        if provider_type == "openai":
            from app.embedding.transformers.openai import OpenAIEmbeddingTransformer

            return OpenAIEmbeddingTransformer(self.config)
        elif provider_type == "local":
            from app.embedding.transformers.local import LocalEmbeddingTransformer

            return LocalEmbeddingTransformer(self.config)
        else:
            raise ValueError(f"不支持的 Embedding Provider: {provider_type}")

    async def embed(self, texts: list[str]) -> EmbeddingResponse:
        """
        批量向量化文本

        Args:
            texts: 待向量化的文本列表

        Returns:
            EmbeddingResponse: 包含向量和元数据的响应对象
        """
        return await self.transformer.embed(texts)

    async def embed_single(self, text: str) -> list[float]:
        """
        单个文本向量化

        Args:
            text: 待向量化的文本

        Returns:
            List[float]: 文本向量
        """
        return await self.transformer.embed_single(text)
