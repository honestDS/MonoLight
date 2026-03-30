import json
import logging
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
    ) -> dict[
        str, Any
    ]:  # 返回原始响应字典，由 Dispatcher 或 BaseTransformer 处理最终封装
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
                        raise LLMException(
                            f"{constants.ERR_LLM_API_RESPONSE_ERROR} [Status: {resp.status}]: {txt}"
                        )
                    return json.loads(txt)
        except Exception as e:
            logger.error(f"OpenAI Driver Error: {str(e)}")
            raise LLMException(str(e))

    @classmethod
    def to_provider(
        cls, internal_messages: list[InternalMessage], **kwargs
    ) -> list[dict[str, Any]]:
        provider_msgs = []
        for msg in internal_messages:
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
        if "tool_calls" in choice:
            tool_calls = [
                InternalToolCall(
                    id=tc["id"],
                    name=tc["function"]["name"],
                    arguments=json.loads(tc["function"]["arguments"]),
                )
                for tc in choice["tool_calls"]
            ]

        return InternalMessage(
            role=MessageRole.ASSISTANT,
            content=choice.get("content"),
            tool_calls=tool_calls,
        )
