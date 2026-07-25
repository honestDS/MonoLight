import asyncio
import codecs
import json
import socket
from collections.abc import AsyncGenerator
from typing import (
    Any,
)

import aiohttp

from app.core.constants import (
    ERR_CHANNEL_MODEL_LIST_FORMAT_ERROR,
    ERR_EMBEDDING_COUNT_MISMATCH,
    ERR_EMBEDDING_DIMENSION_MISMATCH,
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    ERR_LLM_CONNECTION_FAILED,
    ERR_LLM_EMPTY_RESPONSE,
    ERR_LLM_FIRST_CHAR_TIMEOUT,
    ERR_LLM_STREAM_TIMEOUT,
    ERR_PROFILE_EMBEDDING_CALL_FAILED,
    ERR_PROFILE_RERANK_CALL_FAILED,
    ERR_RERANK_FORMAT_ERROR,
)
from app.core.exceptions import EmbeddingException, LLMException, RerankException
from app.core.i18n import t
from app.core.log import get_logger
from app.models.message import (
    FilePart,
    ImagePart,
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    MessageRole,
    TextPart,
)

from .base import (
    BaseEmbeddingTransformer,
    BaseImageGenerationTransformer,
    BaseRerankTransformer,
    BaseTransformer,
)

logger = get_logger(__name__)


def _is_timeout_exception(exc: Exception) -> bool:
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError, socket.timeout)):
        return True
    if isinstance(exc, aiohttp.ServerTimeoutError):
        return True
    return False


class OpenAITransformer(BaseTransformer, BaseEmbeddingTransformer, BaseImageGenerationTransformer, BaseRerankTransformer):
    _PROTOCOL_METADATA = "openai_chat_completions"

    # 本转换器统一关闭 TLS 证书校验，以兼容自签名证书或证书链不完整的模型提供商。
    @staticmethod
    def _nonnegative_token_count(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @classmethod
    def _normalize_usage(cls, usage: Any) -> dict[str, Any]:
        normalized = dict(usage) if isinstance(usage, dict) else {}
        prompt_details = normalized.get("prompt_tokens_details")
        cached_tokens = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
        normalized.update(
            {
                "prompt_tokens": cls._nonnegative_token_count(normalized.get("prompt_tokens")),
                "completion_tokens": cls._nonnegative_token_count(normalized.get("completion_tokens")),
                "total_tokens": cls._nonnegative_token_count(normalized.get("total_tokens")),
                "cached_tokens": cls._nonnegative_token_count(cached_tokens),
            }
        )
        return normalized

    @classmethod
    def _normalize_finish_reason(cls, raw_reason: Any) -> tuple[str | None, dict[str, Any] | None]:
        if raw_reason is None:
            return None, None

        reason = str(raw_reason).strip().lower()
        aliases = {
            "stop": "stop",
            "completed": "stop",
            "complete": "stop",
            "length": "length",
            "max_tokens": "length",
            "max_output_tokens": "length",
            "tool_calls": "tool_calls",
            "function_call": "tool_calls",
            "content_filter": "content_filter",
            "content-filter": "content_filter",
            "safety": "content_filter",
            "moderation": "content_filter",
            "blocked": "content_filter",
            "refusal": "refusal",
            "refused": "refusal",
            "error": "error",
            "failed": "error",
            "incomplete": "incomplete",
            "cancelled": "incomplete",
            "canceled": "incomplete",
        }
        normalized = aliases.get(reason)
        if normalized is None and any(marker in reason for marker in ("content_filter", "safety", "moderation", "blocked", "guardrail")):
            normalized = "content_filter"
        elif normalized is None and any(marker in reason for marker in ("max_output", "max_token", "token_limit")):
            normalized = "length"
        elif normalized is None:
            normalized = "incomplete"
        return normalized, {"raw_finish_reason": raw_reason}

    @classmethod
    def _tool_call_provider_metadata(cls, tool_call: dict[str, Any]) -> dict[str, Any] | None:
        metadata = {key: value for key, value in tool_call.items() if key not in {"id", "function"}}
        function = tool_call.get("function")
        if isinstance(function, dict):
            function_metadata = {key: value for key, value in function.items() if key not in {"name", "arguments"}}
            if function_metadata:
                metadata["function"] = function_metadata
        if not metadata:
            return None
        return {"protocol": cls._PROTOCOL_METADATA, "tool_call": metadata}

    @classmethod
    def _message_provider_metadata(
        cls,
        choice: dict[str, Any],
        message: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "protocol": cls._PROTOCOL_METADATA,
            "choice": {key: value for key, value in choice.items() if key not in {"message", "finish_reason"}},
            "message": {key: value for key, value in message.items() if key not in {"content", "refusal", "tool_calls"}},
        }

    @classmethod
    def _response_provider_metadata(
        cls,
        provider_response: dict[str, Any],
        choice: dict[str, Any],
        message: dict[str, Any],
    ) -> dict[str, Any]:
        message_metadata = cls._message_provider_metadata(choice, message)
        return {
            "protocol": cls._PROTOCOL_METADATA,
            "response": {key: value for key, value in provider_response.items() if key not in {"choices", "model", "usage"}},
            "choice": message_metadata["choice"],
            "message": message_metadata["message"],
        }

    async def list_models(
        self,
        api_key: str,
        base_url: str,
        timeout: float = 30.0,
        **kwargs,
    ) -> list[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = f"{base_url.rstrip('/')}/models"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.get(url, headers=headers) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    parsed = json.loads(txt)
        except LLMException:
            raise
        except Exception as e:
            logger.bind(base_url=base_url).error(t("LOG_MODEL_LIST_FAILED", error=str(e)))
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(e))

        raw_models = parsed.get("data")
        if not isinstance(raw_models, list):
            raise LLMException(ERR_CHANNEL_MODEL_LIST_FORMAT_ERROR)

        models = []
        for item in raw_models:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            models.append(
                {
                    "id": str(item["id"]),
                    "owned_by": item.get("owned_by"),
                    "created": item.get("created"),
                }
            )
        return models

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
        if kwargs.get("top_p") is not None:
            payload["top_p"] = kwargs["top_p"]

        url = f"{base_url.rstrip('/')}/chat/completions"
        # 非流式：对整个请求设置整体超时
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    parsed = json.loads(txt)
                    parsed["usage"] = self._normalize_usage(parsed.get("usage"))
                    return parsed
        except LLMException:
            raise
        except Exception as e:
            logger.bind(model_id=model_id, base_url=base_url, stream=False).error(t("LOG_OPENAI_CHAT_FAILED", error=str(e)))
            if _is_timeout_exception(e):
                raise LLMException(ERR_LLM_FIRST_CHAR_TIMEOUT, timeout=timeout) from e
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(e))

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
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": model_id,
            "prompt": prompt,
            "n": n,
            "size": size,
        }

        optional_fields = {
            "quality": quality,
            "response_format": response_format,
            "style": style,
            "user": kwargs.get("user"),
            "background": kwargs.get("background"),
            "moderation": kwargs.get("moderation"),
            "output_compression": kwargs.get("output_compression"),
            "output_format": kwargs.get("output_format"),
        }
        payload.update({key: value for key, value in optional_fields.items() if value is not None})

        extra_body = kwargs.get("extra_body")
        if isinstance(extra_body, dict):
            payload.update(extra_body)

        url = f"{self._normalize_image_base_url(base_url)}/images/generations"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    return json.loads(txt)
        except LLMException:
            raise
        except Exception as e:
            logger.bind(model_id=model_id, base_url=base_url).error(t("LOG_OPENAI_IMAGE_GENERATION_FAILED", error=str(e)))
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(e))

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
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model_id,
            "messages": self.to_provider(messages),
            "temperature": temperature,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        if kwargs.get("top_p") is not None:
            payload["top_p"] = kwargs["top_p"]

        url = f"{base_url.rstrip('/')}/chat/completions"
        # 流响应超时覆盖建立连接、等待响应头、首个有效输出及后续有效输出间隔。
        # 仅成功解析且包含有效负载的数据块会重置超时截止时间。
        # 空行、keep-alive、无法解析的数据及无有效负载的占位块均不重置。
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None),
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                resp_cm = session.post(url, headers=headers, json=payload)
                # 等待响应头（含服务端首次有效输出前的思考时间）也纳入流响应超时
                try:
                    resp = await asyncio.wait_for(resp_cm.__aenter__(), timeout=max(deadline - loop.time(), 0.001))
                except TimeoutError:
                    raise LLMException(ERR_LLM_STREAM_TIMEOUT, timeout=timeout)
                try:
                    if resp.status != 200:
                        txt = await resp.text()
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)

                    buffer = ""
                    chunk_iter = resp.content.iter_any().__aiter__()
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    while True:
                        try:
                            line = await asyncio.wait_for(
                                chunk_iter.__anext__(),
                                timeout=max(deadline - loop.time(), 0.001),
                            )
                        except TimeoutError:
                            raise LLMException(ERR_LLM_STREAM_TIMEOUT, timeout=timeout)
                        except StopAsyncIteration:
                            buffer += decoder.decode(b"", final=True)
                            break

                        # iter_any() 返回任意大小的原始字节块，使用增量解码器保留跨块 UTF-8 字符。
                        chunks = decoder.decode(line)
                        buffer += chunks

                        done = False
                        while "\n" in buffer:
                            raw_line, buffer = buffer.split("\n", 1)
                            raw_line = raw_line.strip()
                            if not raw_line:
                                continue
                            if raw_line.startswith("data: "):
                                data_content = raw_line[6:]
                                if data_content == "[DONE]":
                                    done = True
                                    break
                                try:
                                    parsed = json.loads(data_content)
                                except Exception as json_err:
                                    logger.bind(model_id=model_id, base_url=base_url).warning(t("LOG_OPENAI_SSE_PARSE_FAILED", raw_line=raw_line, error=str(json_err)))
                                    continue
                                if "usage" in parsed:
                                    parsed["usage"] = self._normalize_usage(parsed.get("usage"))
                                if self._stream_chunk_has_payload(parsed):
                                    deadline = loop.time() + timeout
                                yield parsed
                        if done:
                            break
                finally:
                    await resp_cm.__aexit__(None, None, None)
        except LLMException:
            raise
        except Exception as e:
            logger.bind(model_id=model_id, base_url=base_url, stream=True).error(t("LOG_OPENAI_STREAM_CHAT_FAILED", error=str(e)))
            if _is_timeout_exception(e):
                raise LLMException(ERR_LLM_STREAM_TIMEOUT, timeout=timeout) from e
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(e))

    @staticmethod
    def _stream_chunk_has_payload(parsed: dict[str, Any]) -> bool:
        """判断流式数据块是否包含实质负载（非空文本、推理内容或工具调用）。

        用于重置覆盖首个有效输出及后续有效输出间隔的流响应超时：role-only 空块、
        usage-only 或 finish-only 块等不视为实质负载，不会重置超时截止时间。
        """
        try:
            delta = parsed["choices"][0].get("delta") or {}
        except (KeyError, IndexError, TypeError):
            return False
        if delta.get("content"):
            return True
        if delta.get("refusal"):
            return True
        if delta.get("reasoning_content"):
            return True
        if delta.get("tool_calls"):
            return True
        return False

    async def get_embeddings(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        input_texts: str | list[str],
        suppress_error_log: bool = False,
        timeout: float = 30.0,
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

        url = f"{self._normalize_embedding_base_url(base_url)}/embeddings"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise EmbeddingException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    return json.loads(txt)
        except EmbeddingException:
            raise
        except Exception as e:
            if suppress_error_log:
                logger.bind(model_id=model_id, base_url=base_url, fallback_candidate=True).warning(t("LOG_OPENAI_EMBEDDING_OPTIONAL_PARAMS_FAILED", error=str(e)))
            else:
                logger.bind(model_id=model_id, base_url=base_url).error(t("LOG_OPENAI_EMBEDDING_FAILED", error=str(e)))
            raise EmbeddingException(ERR_PROFILE_EMBEDDING_CALL_FAILED, message=str(e))

    @staticmethod
    def _normalize_embedding_base_url(base_url: str) -> str:
        return base_url.rstrip("/").removesuffix("/embeddings")

    @staticmethod
    def _normalize_image_base_url(base_url: str) -> str:
        return base_url.rstrip("/").removesuffix("/images/generations").removesuffix("/images")

    @staticmethod
    def _normalize_rerank_base_url(base_url: str) -> str:
        # 允许用户把 base_url 配到服务根路径、/v1 或 /v1/rerank，统一归一化为不含 /rerank 后缀的基础路径
        return base_url.rstrip("/").removesuffix("/rerank")

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
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": model_id,
            "query": query,
            "documents": documents,
        }
        if top_n is not None:
            payload["top_n"] = top_n

        url = f"{self._normalize_rerank_base_url(base_url)}/rerank"
        client_timeout = aiohttp.ClientTimeout(total=timeout)
        try:
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload) as resp:
                    txt = await resp.text()
                    if resp.status != 200:
                        raise RerankException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=txt)
                    return json.loads(txt)
        except RerankException:
            raise
        except Exception as e:
            logger.bind(model_id=model_id, base_url=base_url).error(t("LOG_OPENAI_RERANK_FAILED", error=str(e)))
            raise RerankException(ERR_PROFILE_RERANK_CALL_FAILED, params={"message": str(e)})

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
        if not documents:
            return []

        response = await self.get_rerank(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            query=query,
            documents=documents,
            top_n=top_n,
            timeout=timeout,
        )

        raw_results = response.get("results")
        if not isinstance(raw_results, list):
            raise RerankException(ERR_RERANK_FORMAT_ERROR)

        normalized: list[dict[str, Any]] = []
        for item in raw_results:
            if not isinstance(item, dict):
                continue
            index = item.get("index")
            if index is None:
                continue
            # 优先取 relevance_score，缺失时兼容 score 后备字段
            score = item.get("relevance_score")
            if score is None:
                score = item.get("score")
            if score is None:
                continue
            normalized.append({"index": int(index), "relevance_score": float(score)})

        return normalized

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
        if not input_texts:
            return []

        normalized_base_url = self._normalize_embedding_base_url(base_url)
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
                timeout=timeout,
            )

            dimensions_supported = result["dimensions_supported"]
            batch_embeddings = [item["embedding"] for item in result["response"].get("data", [])]

            if len(batch_embeddings) != len(batch_texts):
                raise EmbeddingException(ERR_EMBEDDING_COUNT_MISMATCH)
            if dimensions and dimensions_supported is True and batch_embeddings and len(batch_embeddings[0]) != dimensions:
                raise EmbeddingException(ERR_EMBEDDING_DIMENSION_MISMATCH, actual=len(batch_embeddings[0]), expected=dimensions)

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
        timeout: float = 30.0,
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
                    timeout=timeout,
                )
                return {"response": response, "dimensions_supported": True}
            except EmbeddingException:
                if dimensions_supported is True:
                    raise

        response = await self.get_embeddings(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            input_texts=input_texts,
            timeout=timeout,
        )
        return {"response": response, "dimensions_supported": False if dimensions else dimensions_supported}

    @classmethod
    def to_provider(cls, internal_messages: list[InternalMessage], **kwargs) -> list[dict[str, Any]]:
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
                tool_calls = []
                for tool_call in msg.tool_calls:
                    provider_metadata = tool_call.provider_metadata or {}
                    raw_tool_call = provider_metadata.get("tool_call") if provider_metadata.get("protocol") == cls._PROTOCOL_METADATA else None
                    provider_tool_call = dict(raw_tool_call) if isinstance(raw_tool_call, dict) else {}
                    raw_function = provider_tool_call.get("function")
                    provider_function = dict(raw_function) if isinstance(raw_function, dict) else {}
                    provider_function.update(
                        {
                            "name": tool_call.name,
                            "arguments": json.dumps(tool_call.arguments),
                        }
                    )
                    provider_tool_call.update(
                        {
                            "id": tool_call.id,
                            "type": "function",
                            "function": provider_function,
                        }
                    )
                    tool_calls.append(provider_tool_call)
                item["tool_calls"] = tool_calls
            if msg.tool_call_id:
                item["tool_call_id"] = msg.tool_call_id
            provider_msgs.append(item)
        return provider_msgs

    @classmethod
    def from_provider(cls, provider_response: Any) -> InternalMessage:
        choices = provider_response.get("choices") if isinstance(provider_response, dict) else None
        if not choices:
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)

        first_choice = choices[0] if isinstance(choices[0], dict) else None
        message = first_choice.get("message") if isinstance(first_choice, dict) else None
        if not isinstance(first_choice, dict) or not isinstance(message, dict):
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)

        tool_calls = None
        if "tool_calls" in message and message["tool_calls"] is not None:
            tool_calls = []
            for tc in message["tool_calls"]:
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
                            provider_metadata=cls._tool_call_provider_metadata(tc),
                        )
                    )
                except Exception as e:
                    logger.bind(tool_call=tc).warning(t("LOG_OPENAI_TOOL_ARGS_PARSE_FAILED", error=str(e)))

        refusal = message.get("refusal") if isinstance(message.get("refusal"), str) else None
        content = message.get("content")
        if not content and refusal:
            content = refusal

        return InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            refusal=refusal,
            provider_metadata=cls._message_provider_metadata(first_choice, message),
            tool_calls=tool_calls if tool_calls else None,
        )

    @classmethod
    def to_internal_response(cls, provider_response: Any, default_model: str) -> InternalResponse:
        if not isinstance(provider_response, dict):
            return super().to_internal_response(provider_response, default_model)

        choices = provider_response.get("choices")
        first_choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else None
        message = first_choice.get("message") if isinstance(first_choice, dict) else None
        if not isinstance(first_choice, dict) or not isinstance(message, dict):
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)

        internal_message = cls.from_provider(provider_response)
        finish_reason, finish_details = cls._normalize_finish_reason(first_choice.get("finish_reason"))
        if internal_message.refusal and finish_reason in {None, "stop"}:
            finish_reason = "refusal"
        model = provider_response.get("model")
        return InternalResponse(
            message=internal_message,
            model=str(model) if model is not None else default_model,
            usage=cls._normalize_usage(provider_response.get("usage")),
            finish_reason=finish_reason,
            finish_details=finish_details,
            provider_metadata=cls._response_provider_metadata(provider_response, first_choice, message),
        )
