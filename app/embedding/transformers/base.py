"""
Embedding Transformer 基类

定义所有 Embedding Transformer 必须实现的接口。
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class EmbeddingResponse(BaseModel):
    """Embedding 标准响应模型"""

    embeddings: List[List[float]] = Field(description="向量列表")
    model: str = Field(description="使用的模型标识")
    usage: Dict[str, Any] = Field(
        default_factory=dict,
        description="使用统计（如 token 数量）",
    )
    dimensions: Optional[int] = Field(
        default=None,
        description="向量维度",
    )

    def __init__(self, **data):
        super().__init__(**data)
        if self.embeddings and self.dimensions is None:
            self.dimensions = len(self.embeddings[0])


class BaseEmbeddingTransformer(ABC):
    """Embedding Transformer 抽象基类"""

    def __init__(self, config):
        """
        初始化 Transformer

        Args:
            config: EmbeddingConfig 配置对象
        """
        self.config = config

    @abstractmethod
    async def embed(self, texts: List[str]) -> EmbeddingResponse:
        """
        批量向量化文本

        Args:
            texts: 待向量化的文本列表

        Returns:
            EmbeddingResponse: 包含向量和元数据的响应对象
        """
        pass

    async def embed_single(self, text: str) -> List[float]:
        """
        单个文本向量化（默认实现）

        Args:
            text: 待向量化的文本

        Returns:
            List[float]: 文本向量
        """
        response = await self.embed([text])
        if not response.embeddings:
            raise ValueError("Embedding 返回为空")
        return response.embeddings[0]
