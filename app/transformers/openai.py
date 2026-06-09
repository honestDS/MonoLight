import json
from collections.abc import AsyncGenerator
from typing import (
    Any,
)

import aiohttp

from app.core import constants
from app.core.exceptions import LLMException
from app.core.log import get_logger
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    MessageRole,
)

from .base import (
    BaseEmbeddingTransformer,
    BaseTransformer,
)

logger = get_logger(__name__)


class OpenAITransformer(BaseTransformer, BaseEmbeddingTransformer):
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
            logger.bind(model_id=model_id, base_url=base_url, stream=False).error(f"OpenAI 对话接口调用失败: {str(e)}")
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
                                    logger.bind(model_id=model_id, base_url=base_url).warning(f"解析 SSE 响应行失败: {raw_line}，错误: {json_err}")
        except Exception as e:
            logger.bind(model_id=model_id, base_url=base_url, stream=True).error(f"OpenAI 流式对话接口调用失败: {str(e)}")
            raise LLMException(str(e))

    async def get_embeddings(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: str | list[str],
        suppress_error_log: bool = False,
        **kwargs,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }

        payload = {
            "model": model_id,
            "input": input_texts,
        }
        if "dimensions" in kwargs:
            payload["dimensions"] = kwargs["dimensions"]
        if "encoding_format" in kwargs:
            payload["encoding_format"] = kwargs["encoding_format"]
        if "user" in kwargs:
            payload["user"] = kwargs["user"]

        url = f"{self.normalize_embedding_base_url(base_url)}/embeddings"
        try:
            async with aiohttp.ClientSession() as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise LLMException(f"{constants.ERR_LLM_API_RESPONSE_ERROR} [Status: {resp.status}]: {txt}")
                    return json.loads(txt)
        except Exception as e:
            if suppress_error_log:
                logger.bind(model_id=model_id, base_url=base_url, fallback_candidate=True).warning(f"向量模型接口携带可选参数调用失败，准备降级重试: {str(e)}")
            else:
                logger.bind(model_id=model_id, base_url=base_url).error(f"向量模型接口调用失败: {str(e)}")
            raise LLMException(str(e))

    @staticmethod
    def normalize_embedding_base_url(base_url: str) -> str:
        return base_url.rstrip("/").removesuffix("/embeddings")

    async def embed_texts(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: list[str],
        batch_size: int = 16,
        dimensions: int | None = None,
    ) -> list[list[float]]:
        if not input_texts:
            return []

        normalized_base_url = self.normalize_embedding_base_url(base_url)
        embeddings: list[list[float]] = []
        dimensions_supported: bool | None = None

        for start in range(0, len(input_texts), batch_size):
            batch_texts = input_texts[start : start + batch_size]
            result = await self._get_embedding_batch_with_dimension_fallback(
                api_key=api_key,
                base_url=normalized_base_url,
                model_id=model_id,
                input_texts=batch_texts,
                dimensions=dimensions,
                dimensions_supported=dimensions_supported,
            )
            dimensions_supported = result["dimensions_supported"]
            batch_embeddings = [item["embedding"] for item in result["response"].get("data", [])]

            if len(batch_embeddings) != len(batch_texts):
                raise LLMException("向量模型返回数量与文本数量不一致")
            if dimensions and dimensions_supported is True and batch_embeddings and len(batch_embeddings[0]) != dimensions:
                raise LLMException(f"向量模型实际输出维度为 {len(batch_embeddings[0])}，与配置的 {dimensions} 不一致")

            embeddings.extend(batch_embeddings)

        return embeddings

    async def _get_embedding_batch_with_dimension_fallback(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: list[str],
        dimensions: int | None,
        dimensions_supported: bool | None,
    ) -> dict[str, Any]:
        if dimensions and dimensions_supported is not False:
            try:
                response = await self.get_embeddings(
                    api_key=api_key,
                    base_url=base_url,
                    model_id=model_id,
                    input_texts=input_texts,
                    suppress_error_log=dimensions_supported is None,
                    dimensions=dimensions,
                )
                return {"response": response, "dimensions_supported": True}
            except LLMException:
                if dimensions_supported is True:
                    raise

        response = await self.get_embeddings(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            input_texts=input_texts,
        )
        return {"response": response, "dimensions_supported": False if dimensions else dimensions_supported}

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
                    logger.bind(tool_call=tc).warning(f"解析工具调用参数失败: {e}")

        return InternalMessage(
            role=MessageRole.ASSISTANT,
            content=choice.get("content"),
            tool_calls=tool_calls if tool_calls else None,
        )
