import re
from abc import (
    ABC,
    abstractmethod,
)
from collections.abc import AsyncGenerator, Mapping
from typing import (
    Any,
)

from app.core.constants import ERR_CHANNEL_MODEL_LIST_UNSUPPORTED
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.models.message import (
    InternalMessage,
    InternalResponse,
)

logger = get_logger(__name__)

_USAGE_KEY_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
_USAGE_MAX_DEPTH = 3
_USAGE_MAX_ITEMS = 16


def _summarize_usage(value: Any, depth: int = 0) -> Any:
    if value is None:
        return None
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    if not isinstance(value, Mapping):
        return type(value).__name__
    if depth >= _USAGE_MAX_DEPTH:
        return type(value).__name__

    summary: dict[str, Any] = {}
    for key, item in value.items():
        if len(summary) >= _USAGE_MAX_ITEMS:
            break
        if not isinstance(key, str) or _USAGE_KEY_PATTERN.fullmatch(key) is None:
            continue
        summary[key] = _summarize_usage(item, depth + 1)
    return summary


def log_upstream_usage(
    *,
    model_id: str,
    protocol: str,
    blocks: dict[str, Any],
    result_count: int | None = None,
) -> None:
    try:
        provider_usage = _summarize_usage(blocks)
        logger.bind(
            model_id=model_id,
            protocol=protocol,
            provider_usage=provider_usage,
        ).debug(
            t(
                "LOG_UPSTREAM_MODEL_USAGE",
                protocol=protocol,
                model_id=model_id,
                usage=provider_usage,
                result_count=result_count,
            )
        )
    except Exception:
        pass


class BaseTransformer(ABC):
    @classmethod
    @abstractmethod
    def build_input_token_payload(
        cls,
        *,
        model_id: str | None,
        channel_id: int | None = None,
        messages: list[InternalMessage],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def list_models(
        self,
        api_key: str,
        base_url: str,
        timeout: float = 30.0,
        http_proxy: str | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        raise LLMException(ERR_CHANNEL_MODEL_LIST_UNSUPPORTED, protocol=self.__class__.__name__)

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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs,
    ) -> Any:
        """返回厂商原始响应，由 to_internal_response 统一封装。"""
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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
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
    def from_provider(cls, provider_response: Any) -> InternalMessage:
        """将厂商特定响应转换为内部标准消息模型"""
        pass

    @classmethod
    def to_internal_response(cls, provider_response: Any, default_model: str) -> InternalResponse:
        """将厂商响应封装为内部标准响应，子类可覆写以保留协议细节。"""
        message = cls.from_provider(provider_response)
        if isinstance(provider_response, dict):
            model = provider_response.get("model")
            raw_usage = provider_response.get("usage")
        else:
            model = getattr(provider_response, "model", None)
            raw_usage = getattr(provider_response, "usage", None)

        usage: dict[str, Any] | None = None
        if isinstance(raw_usage, dict):
            usage = dict(raw_usage)
        elif hasattr(raw_usage, "model_dump"):
            dumped_usage = raw_usage.model_dump(mode="json")
            if isinstance(dumped_usage, dict):
                usage = dumped_usage
        elif raw_usage is not None:
            try:
                usage = dict(raw_usage)
            except (TypeError, ValueError):
                pass

        response_kwargs: dict[str, Any] = {
            "message": message,
            "model": str(model) if model is not None else default_model,
        }
        if usage is not None:
            response_kwargs["usage"] = usage
        return InternalResponse(**response_kwargs)


class BaseEmbeddingTransformer(ABC):
    @abstractmethod
    async def get_embeddings(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: str | list[str],
        timeout: float = 30.0,
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
    ) -> list[dict[str, Any]]:
        pass
