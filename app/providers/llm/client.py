import json
from collections.abc import AsyncGenerator, Awaitable, Callable
from typing import (
    Any,
)

from app.core import constants
from app.core.exceptions import LLMException
from app.models.message import (
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    MessageRole,
)
from app.transformers.openai import OpenAITransformer


class LLMClient:
    _transformers = {"openai": OpenAITransformer()}

    @classmethod
    async def list_models(
        cls,
        api_key: str,
        base_url: str,
        protocol: str = "openai",
        timeout: float = 30.0,
        **kwargs,
    ) -> list[dict[str, Any]]:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            raise LLMException(constants.ERR_CHANNEL_MODEL_LIST_UNSUPPORTED, protocol=protocol)

        return await transformer.list_models(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            **kwargs,
        )

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
            raise LLMException(message=constants.ERR_LLM_UNSUPPORTED_PROTOCOL, protocol=protocol)

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
    async def generate_with_stream_callback(
        cls,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        on_content: Callable[[str], Awaitable[None]],
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        protocol: str = "openai",
        timeout: float = 60.0,
        **kwargs,
    ) -> InternalResponse:
        content_chunks: list[str] = []
        tool_calls_by_index: dict[int, dict[str, str]] = {}
        model = model_id
        usage: dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }

        async for chunk in cls.generate_stream(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            protocol=protocol,
            timeout=timeout,
            **kwargs,
        ):
            if isinstance(chunk.get("model"), str):
                model = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            if not choices:
                continue
            delta = choices[0].get("delta") or {}
            content = delta.get("content")
            if isinstance(content, str) and content:
                content_chunks.append(content)
                await on_content(content)
            for tool_call in delta.get("tool_calls") or []:
                index = int(tool_call.get("index", 0))
                accumulated = tool_calls_by_index.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if tool_call.get("id"):
                    accumulated["id"] = str(tool_call["id"])
                function = tool_call.get("function") or {}
                if function.get("name"):
                    accumulated["name"] = str(function["name"])
                if function.get("arguments"):
                    accumulated["arguments"] += str(function["arguments"])

        tool_calls: list[InternalToolCall] = []
        for index, accumulated in sorted(tool_calls_by_index.items()):
            if not accumulated["name"]:
                continue
            arguments: dict[str, Any] = {}
            if accumulated["arguments"]:
                try:
                    parsed_arguments = json.loads(accumulated["arguments"])
                    if isinstance(parsed_arguments, dict):
                        arguments = parsed_arguments
                except (TypeError, ValueError):
                    pass
            tool_calls.append(
                InternalToolCall(
                    id=accumulated["id"] or f"call_{index}",
                    name=accumulated["name"],
                    arguments=arguments,
                )
            )

        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content="".join(content_chunks) or None,
                tool_calls=tool_calls or None,
            ),
            model=model,
            usage=usage,
        )

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
            raise LLMException(message=constants.ERR_LLM_UNSUPPORTED_PROTOCOL, protocol=protocol)

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
