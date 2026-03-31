"""
Embedding 向量化模块

提供独立的文本向量化和相似度计算能力，支持多厂商 API 和本地模型。
"""

from app.embedding.client import EmbeddingClient
from app.embedding.config import EmbeddingConfig

__all__ = ["EmbeddingClient", "EmbeddingConfig"]
