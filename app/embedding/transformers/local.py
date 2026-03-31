"""
Local Embedding Transformer

基于 sentence-transformers 的本地模型适配器。
"""

from typing import List

from app.core.exceptions import ServerException
from app.embedding.transformers.base import (
    BaseEmbeddingTransformer,
    EmbeddingResponse,
)


class LocalEmbeddingTransformer(BaseEmbeddingTransformer):
    """本地 Embedding 模型适配器（基于 sentence-transformers）"""

    def __init__(self, config):
        super().__init__(config)
        self.model = None
        self._load_model()

    def _load_model(self):
        """加载本地模型"""
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            raise ServerException(
                "sentence-transformers 库未安装，请运行: pip install sentence-transformers"
            )

        try:
            cache_folder = self.config.model_cache_dir or None
            self.model = SentenceTransformer(
                self.config.model_id, cache_folder=cache_folder
            )
        except Exception as e:
            raise ServerException(f"加载本地 Embedding 模型失败: {str(e)}")

    async def embed(self, texts: List[str]) -> EmbeddingResponse:
        """
        使用本地模型进行向量化

        Args:
            texts: 待向量化的文本列表

        Returns:
            EmbeddingResponse: 标准响应对象

        Raises:
            ServerException: 模型推理失败时抛出
        """
        if not texts:
            raise ValueError("文本列表不能为空")

        if self.model is None:
            raise ServerException("本地 Embedding 模型未加载")

        try:
            embeddings_array = self.model.encode(
                texts,
                batch_size=self.config.batch_size,
                show_progress_bar=False,
                convert_to_numpy=True,
            )

            embeddings = [emb.tolist() for emb in embeddings_array]

            total_tokens = sum(len(text.split()) for text in texts)

            return EmbeddingResponse(
                embeddings=embeddings,
                model=self.config.model_id,
                usage={
                    "prompt_tokens": total_tokens,
                    "total_tokens": total_tokens,
                },
            )

        except Exception as e:
            raise ServerException(f"本地 Embedding 模型推理失败: {str(e)}")
