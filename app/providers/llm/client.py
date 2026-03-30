import logging
from typing import (
    Any,
)

from app.models.message import (
    InternalMessage,
    InternalResponse,
)
from app.transformers.openai import OpenAITransformer

logger = logging.getLogger(__name__)


class LLMClient:
    _transformers = {"openai": OpenAITransformer()}

    @classmethod
    async def generate(
        cls,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        protocol: str = "openai",
        **kwargs,
    ) -> InternalResponse:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            from app.core import constants
            from app.core.exceptions import LLMException

            raise LLMException(
                f"{constants.ERR_LLM_UNEXPECTED_ERROR}: Unsupported protocol {protocol}"
            )

        raw_response = await transformer.generate(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            **kwargs,
        )

        # 核心修复：调用 transformer.from_provider 将原始 dict 转换为 InternalResponse
        # 注意：目前的 OpenAITransformer.from_provider 返回的是 InternalMessage
        # 而 BaseTransformer.from_provider 定义返回的是 InternalResponse
        # 我们需要统一封装逻辑

        ai_message = transformer.from_provider(raw_response)

        return InternalResponse(
            message=ai_message,
            model=raw_response.get("model", model_id),
            usage=raw_response.get(
                "usage",
                {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            ),
        )
