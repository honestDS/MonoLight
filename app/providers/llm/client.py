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
from app.transformers.openai import OpenAITransformer


class LLMClient:
    _transformers = {"openai": OpenAITransformer()}

    @classmethod
    async def generate_stream(
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
        timeout: float = 60.0,
        **kwargs,
    ) -> AsyncGenerator[dict[str, Any]]:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            raise LLMException(constants.ERR_LLM_UNEXPECTED_ERROR_WITH_DETAIL, detail=f"Unsupported protocol {protocol}")

        async for chunk in transformer.generate_stream(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            timeout=timeout,
            **kwargs,
        ):
            yield chunk

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
        timeout: float = 60.0,
        **kwargs,
    ) -> InternalResponse:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            raise LLMException(constants.ERR_LLM_UNEXPECTED_ERROR_WITH_DETAIL, detail=f"Unsupported protocol {protocol}")

        raw_response = await transformer.generate(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            timeout=timeout,
            **kwargs,
        )

        # Transformer 返回 InternalMessage，客户端统一封装为 InternalResponse。
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
