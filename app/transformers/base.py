from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import AsyncGenerator
from typing import (
    Any,
)

from app.core import constants
from app.core.exceptions import LLMException
from app.models.message import (
    InternalMessage,
    InternalResponse,
)


class BaseTransformer(ABC):
    async def list_models(
        self,
        api_key: str,
        base_url: str,
        timeout: float = 30.0,
        **kwargs,
    ) -> list[dict[str, Any]]:
        raise LLMException(constants.ERR_CHANNEL_MODEL_LIST_UNSUPPORTED, protocol=self.__class__.__name__)

    @abstractmethod
    async def generate(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        timeout: float = 60.0,
        **kwargs,
    ) -> InternalResponse:
        pass

    @abstractmethod
    async def generate_stream(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        timeout: float = 60.0,
        **kwargs,
    ) -> AsyncGenerator[dict[str, Any]]:
        pass

    @classmethod
    @abstractmethod
    def to_provider(cls, internal_messages: list[InternalMessage], **kwargs) -> Any:
        """将内部标准消息转换为厂商特定协议格式"""
        pass

    @classmethod
    @abstractmethod
    def from_provider(cls, provider_response: Any) -> InternalResponse:
        """将厂商特定响应转换为内部标准响应模型"""
        pass


class BaseEmbeddingTransformer(ABC):
    @abstractmethod
    async def get_embeddings(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: str | list[str],
        timeout: float = 30.0,
        **kwargs,
    ) -> dict[str, Any]:
        pass

    @abstractmethod
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
        pass


class BaseImageGenerationTransformer(ABC):
    @abstractmethod
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
        pass


class BaseRerankTransformer(ABC):
    @abstractmethod
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
        pass

    @abstractmethod
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
        pass
