from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional
from app.models.message import (
    InternalMessage,
    InternalResponse
)


class BaseTransformer(ABC):
    @abstractmethod
    async def generate(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: List[InternalMessage],
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: str = "auto",
        **kwargs,
    ) -> InternalResponse:
        pass

    @classmethod
    @abstractmethod
    def to_provider(cls, internal_messages: List[InternalMessage], **kwargs) -> Any:
        """将内部标准消息转换为厂商特定协议格式"""
        pass

    @classmethod
    @abstractmethod
    def from_provider(cls, provider_response: Any) -> InternalResponse:
        """将厂商特定响应转换为内部标准响应模型"""
        pass
