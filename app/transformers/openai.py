import json
import logging
from collections.abc import AsyncGenerator
from typing import (
    Any,
)

import aiohttp

from app.core import constants
from app.core.exceptions import LLMException
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    MessageRole,
)

from .base import BaseTransformer

logger = logging.getLogger(__name__)


class OpenAITransformer(BaseTransformer):
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
        **kwargs,
    ) -> dict[str, Any]:  # 返回原始响应字典，由 Dispatcher 或 BaseTransformer 处理最终封装
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_id,
            "messages": self.to_provider(messages),
            "temperature": temperature,
            "stream": False,
        }

        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens

        url = f"{base_url.rstrip('/')}/chat/completions"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise LLMException(f"{constants.ERR_LLM_API_RESPONSE_ERROR} [Status: {resp.status}]: {txt}")
                    return json.loads(txt)
        except Exception as e:
            logger.error(f"OpenAI Driver Error: {str(e)}")
            raise LLMException(str(e))

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
        **kwargs,
    ) -> AsyncGenerator[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_id,
            "messages": self.to_provider(messages),
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens

        url = f"{base_url.rstrip('/')}/chat/completions"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    if resp.status != 200:
                        txt = await resp.text()
                        raise LLMException(f"{constants.ERR_LLM_API_RESPONSE_ERROR} [Status: {resp.status}]: {txt}")

                    buffer = ""
                    async for line in resp.content.iter_any():
                        # 使用 iter_any() 获取任意大小的非阻塞原始字节块，避免阻塞
                        chunks = line.decode("utf-8")
                        buffer += chunks

                        while "\n" in buffer:
                            raw_line, buffer = buffer.split("\n", 1)
                            raw_line = raw_line.strip()
                            if not raw_line:
                                continue
                            if raw_line.startswith("data: "):
                                data_content = raw_line[6:]
                                if data_content == "[DONE]":
                                    break
                                try:
                                    yield json.loads(data_content)
                                except Exception as json_err:
                                    logger.warning(f"Failed to parse SSE line: {raw_line}, error: {json_err}")
        except Exception as e:
            logger.error(f"OpenAI Stream Driver Error: {str(e)}")
            raise LLMException(str(e))

    @classmethod
    def to_provider(cls, internal_messages: list[InternalMessage], **kwargs) -> list[dict[str, Any]]:
        from app.models.message import FilePart, ImagePart, TextPart

        provider_msgs = []
        for msg in internal_messages:
            if isinstance(msg.content, list):
                # 转换 InternalMessage content 列表 为 OpenAI 官方多模态格式
                content = []
                for part in msg.content:
                    if isinstance(part, TextPart) or getattr(part, "type", None) == "text":
                        content.append({"type": "text", "text": getattr(part, "text", "")})
                    elif isinstance(part, ImagePart) or getattr(part, "type", None) == "image_url":
                        content.append({"type": "image_url", "image_url": {"url": getattr(part, "image_url", {}).get("url", "")}})
                    elif isinstance(part, FilePart) or getattr(part, "type", None) == "file":
                        # OpenAI 当前不支持直接传递任意文件，转为文本描述给上下文
                        content.append({"type": "text", "text": f"[Attached File: {getattr(part, 'path', '')}]"})
                    else:
                        # 对于未知 part 尝试调用 model_dump 或回退保留
                        content.append(part.model_dump() if hasattr(part, "model_dump") else part)
                item = {"role": msg.role.value, "content": content}
            else:
                item = {"role": msg.role.value, "content": msg.content}

            if msg.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id:
                item["tool_call_id"] = msg.tool_call_id
            provider_msgs.append(item)
        return provider_msgs

    @classmethod
    def from_provider(cls, provider_response: Any) -> InternalMessage:
        choice = provider_response["choices"][0]["message"]
        tool_calls = None
        if "tool_calls" in choice and choice["tool_calls"] is not None:
            tool_calls = []
            for tc in choice["tool_calls"]:
                try:
                    args = tc["function"]["arguments"]
                    if isinstance(args, str):
                        parsed_args = json.loads(args) if args.strip() else {}
                    else:
                        parsed_args = args if isinstance(args, dict) else {}
                    tool_calls.append(
                        InternalToolCall(
                            id=tc["id"],
                            name=tc["function"]["name"],
                            arguments=parsed_args,
                        )
                    )
                except Exception as e:
                    logger.warning(f"Failed to parse tool call arguments: {tc}, error: {e}")

        return InternalMessage(
            role=MessageRole.ASSISTANT,
            content=choice.get("content"),
            tool_calls=tool_calls if tool_calls else None,
        )
