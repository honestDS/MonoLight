import asyncio
import codecs
import json
import socket
from collections.abc import AsyncGenerator, Callable
from typing import Any

import aiohttp

from app.core.constants import (
    ERR_CHANNEL_MODEL_LIST_FORMAT_ERROR,
    ERR_LLM_API_RESPONSE_ERROR,
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    ERR_LLM_CONNECTION_FAILED,
    ERR_LLM_FIRST_CHAR_TIMEOUT,
    ERR_LLM_STREAM_TIMEOUT,
)
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.http_proxy import build_aiohttp_proxy_kwargs

from ..base import BaseTransformer

logger = get_logger(__name__)


class BaseOpenAITransformer(BaseTransformer):
    @staticmethod
    def _is_timeout_exception(exc: Exception) -> bool:
        return isinstance(exc, (asyncio.TimeoutError, TimeoutError, socket.timeout, aiohttp.ServerTimeoutError))

    @staticmethod
    def _nonnegative_token_count(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

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

    async def list_models(
        self,
        api_key: str,
        base_url: str,
        timeout: float = 30.0,
        http_proxy: str | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = f"{base_url.rstrip('/')}/models"
        try:
            proxy_kwargs = build_aiohttp_proxy_kwargs(http_proxy)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.get(url, headers=headers, **proxy_kwargs) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=text)
                    parsed = json.loads(text)
        except LLMException:
            raise
        except Exception as exc:
            logger.bind(base_url=base_url).error(t("LOG_MODEL_LIST_FAILED", error=str(exc)))
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(exc)) from exc

        raw_models = parsed.get("data") if isinstance(parsed, dict) else None
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

    async def _post_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        http_proxy: str | None,
        model_id: str,
        base_url: str,
    ) -> dict[str, Any]:
        try:
            proxy_kwargs = build_aiohttp_proxy_kwargs(http_proxy)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=timeout),
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload, **proxy_kwargs) as resp:
                    text = await resp.text()
                    if resp.status != 200:
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=resp.status, detail=text)
                    parsed = json.loads(text)
                    if not isinstance(parsed, dict):
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR, detail=text)
                    return parsed
        except LLMException:
            raise
        except Exception as exc:
            logger.bind(model_id=model_id, base_url=base_url, stream=False).error(t("LOG_OPENAI_CHAT_FAILED", error=str(exc)))
            if self._is_timeout_exception(exc):
                raise LLMException(ERR_LLM_FIRST_CHAR_TIMEOUT, timeout=timeout) from exc
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(exc)) from exc

    async def _stream_sse_json(
        self,
        *,
        url: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        timeout: float,
        http_proxy: str | None,
        model_id: str,
        base_url: str,
        normalize_event: Callable[[Any], tuple[dict[str, Any] | None, bool]],
    ) -> AsyncGenerator[dict[str, Any]]:
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        try:
            proxy_kwargs = build_aiohttp_proxy_kwargs(http_proxy)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None),
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                response_context = session.post(url, headers=headers, json=payload, **proxy_kwargs)
                try:
                    response = await asyncio.wait_for(
                        response_context.__aenter__(),
                        timeout=max(deadline - loop.time(), 0.001),
                    )
                except TimeoutError as exc:
                    raise LLMException(ERR_LLM_STREAM_TIMEOUT, timeout=timeout) from exc
                try:
                    if response.status != 200:
                        text = await response.text()
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=response.status, detail=text)

                    buffer = ""
                    chunk_iter = response.content.iter_any().__aiter__()
                    decoder = codecs.getincrementaldecoder("utf-8")()
                    while True:
                        try:
                            raw_bytes = await asyncio.wait_for(
                                chunk_iter.__anext__(),
                                timeout=max(deadline - loop.time(), 0.001),
                            )
                        except TimeoutError as exc:
                            raise LLMException(ERR_LLM_STREAM_TIMEOUT, timeout=timeout) from exc
                        except StopAsyncIteration:
                            buffer += decoder.decode(b"", final=True)
                            break

                        buffer += decoder.decode(raw_bytes)
                        done = False
                        while "\n" in buffer:
                            raw_line, buffer = buffer.split("\n", 1)
                            raw_line = raw_line.strip()
                            if not raw_line or not raw_line.startswith("data:"):
                                continue
                            data_content = raw_line[5:].lstrip()
                            if data_content == "[DONE]":
                                done = True
                                break
                            try:
                                event = json.loads(data_content)
                            except Exception as json_error:
                                logger.bind(model_id=model_id, base_url=base_url).warning(t("LOG_OPENAI_SSE_PARSE_FAILED", raw_line=raw_line, error=str(json_error)))
                                continue

                            normalized, has_payload = normalize_event(event)
                            if has_payload:
                                deadline = loop.time() + timeout
                            if normalized is not None:
                                yield normalized
                        if done:
                            break
                finally:
                    await response_context.__aexit__(None, None, None)
        except LLMException:
            raise
        except Exception as exc:
            logger.bind(model_id=model_id, base_url=base_url, stream=True).error(t("LOG_OPENAI_STREAM_CHAT_FAILED", error=str(exc)))
            if self._is_timeout_exception(exc):
                raise LLMException(ERR_LLM_STREAM_TIMEOUT, timeout=timeout) from exc
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(exc)) from exc
