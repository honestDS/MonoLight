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
    ERR_LLM_CONNECTION_FAILED,
    ERR_LLM_FIRST_CHAR_TIMEOUT,
    ERR_LLM_STREAM_TIMEOUT,
)
from app.core.exceptions import LLMException
from app.core.utils.http_proxy import build_aiohttp_proxy_kwargs
from app.core.utils.llm_request_policy import prepare_llm_json_request


def _is_timeout_exception(exc: Exception) -> bool:
    return isinstance(exc, (asyncio.TimeoutError, TimeoutError, socket.timeout, aiohttp.ServerTimeoutError))


async def _wait_for_stream_operation(awaitable, *, deadline: float, timeout: float):
    try:
        return await asyncio.wait_for(
            awaitable,
            timeout=max(deadline - asyncio.get_running_loop().time(), 0),
        )
    except TimeoutError as exc:
        raise LLMException(ERR_LLM_STREAM_TIMEOUT, timeout=timeout) from exc


async def list_models(
    *,
    api_key: str,
    base_url: str,
    timeout: float = 30,
    http_proxy: str | None = None,
    raise_error: Callable[..., None],
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
            async with session.get(url, headers=headers, **proxy_kwargs) as response:
                text = await response.text()
                if response.status != 200:
                    raise_error(text, status=response.status)
                    raise LLMException(ERR_LLM_API_RESPONSE_ERROR, status=response.status, detail=text)
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError) as exc:
                    raise LLMException(ERR_CHANNEL_MODEL_LIST_FORMAT_ERROR) from exc
    except LLMException:
        raise
    except Exception as exc:
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


async def post_json(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    http_proxy: str | None,
    raise_error: Callable[..., None],
) -> dict[str, Any]:
    try:
        serialized_payload = await prepare_llm_json_request(payload)
        proxy_kwargs = build_aiohttp_proxy_kwargs(http_proxy)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=timeout),
            connector=aiohttp.TCPConnector(ssl=False),
        ) as session:
            async with session.post(url, headers=headers, data=serialized_payload, **proxy_kwargs) as response:
                text = await response.text()
                if response.status != 200:
                    raise_error(text, status=response.status)
                    raise LLMException(ERR_LLM_API_RESPONSE_ERROR, status=response.status, detail=text)
                try:
                    parsed = json.loads(text)
                except (TypeError, ValueError) as exc:
                    raise LLMException(ERR_LLM_API_RESPONSE_ERROR, detail=text) from exc
                if not isinstance(parsed, dict):
                    raise LLMException(ERR_LLM_API_RESPONSE_ERROR, detail=text)
                return parsed
    except LLMException:
        raise
    except Exception as exc:
        if _is_timeout_exception(exc):
            raise LLMException(ERR_LLM_FIRST_CHAR_TIMEOUT, timeout=timeout) from exc
        raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(exc)) from exc


async def stream_sse_json(
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    timeout: float,
    http_proxy: str | None,
    normalize_event: Callable[[Any], tuple[dict[str, Any] | None, bool]],
    raise_error: Callable[..., None],
    is_terminal: Callable[[Any], bool] | None = None,
) -> AsyncGenerator[dict[str, Any]]:
    loop = asyncio.get_running_loop()
    try:
        serialized_payload = await prepare_llm_json_request(payload)
        deadline = loop.time() + timeout
        proxy_kwargs = build_aiohttp_proxy_kwargs(http_proxy)
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=None, sock_read=None),
            connector=aiohttp.TCPConnector(ssl=False),
        ) as session:
            response_context = session.post(url, headers=headers, data=serialized_payload, **proxy_kwargs)
            response = await _wait_for_stream_operation(
                response_context.__aenter__(),
                deadline=deadline,
                timeout=timeout,
            )
            try:
                if response.status != 200:
                    text = await _wait_for_stream_operation(
                        response.text(),
                        deadline=deadline,
                        timeout=timeout,
                    )
                    raise_error(text, status=response.status)
                    raise LLMException(ERR_LLM_API_RESPONSE_ERROR, status=response.status, detail=text)

                decoder = codecs.getincrementaldecoder("utf-8")()
                buffer = ""
                data_lines: list[str] = []
                chunk_iter = response.content.iter_any().__aiter__()

                def consume_line(raw_line: str) -> list[str] | None:
                    nonlocal data_lines
                    line = raw_line[:-1] if raw_line.endswith("\r") else raw_line
                    if not line:
                        if not data_lines:
                            return None
                        frame = data_lines
                        data_lines = []
                        return frame
                    if line.startswith(":"):
                        return None
                    field, separator, value = line.partition(":")
                    if field != "data":
                        return None
                    if separator and value.startswith(" "):
                        value = value[1:]
                    data_lines.append(value)
                    return None

                def normalize_frame(frame: list[str]) -> tuple[bool, dict[str, Any] | None]:
                    nonlocal deadline
                    raw_event = "\n".join(frame)
                    if raw_event.strip() == "[DONE]":
                        return True, None
                    try:
                        event = json.loads(raw_event)
                        finished = is_terminal(event) if is_terminal is not None else False
                        normalized, has_output = normalize_event(event)
                        if normalized is not None and not isinstance(normalized, dict):
                            raise TypeError("normalize_event must return a dictionary or None")
                        if not isinstance(has_output, bool):
                            raise TypeError("normalize_event output flag must be a bool")
                    except LLMException:
                        raise
                    except Exception as exc:
                        raise LLMException(ERR_LLM_API_RESPONSE_ERROR, detail=raw_event) from exc
                    if has_output:
                        deadline = loop.time() + timeout
                    return finished, normalized

                finished = False
                end_of_stream = False
                while not finished:
                    if end_of_stream:
                        break
                    try:
                        raw_chunk = await _wait_for_stream_operation(
                            chunk_iter.__anext__(),
                            deadline=deadline,
                            timeout=timeout,
                        )
                    except StopAsyncIteration:
                        try:
                            buffer += decoder.decode(b"", final=True)
                        except UnicodeDecodeError as exc:
                            raise LLMException(ERR_LLM_API_RESPONSE_ERROR, detail=str(exc)) from exc
                        end_of_stream = True
                    else:
                        try:
                            buffer += decoder.decode(raw_chunk)
                        except UnicodeDecodeError as exc:
                            raise LLMException(ERR_LLM_API_RESPONSE_ERROR, detail=str(exc)) from exc

                    while "\n" in buffer:
                        raw_line, buffer = buffer.split("\n", 1)
                        frame = consume_line(raw_line)
                        if frame is None:
                            continue
                        finished, normalized = normalize_frame(frame)
                        if normalized is not None:
                            yield normalized
                        if finished:
                            break

                    if finished:
                        break
                    if end_of_stream:
                        if buffer:
                            frame = consume_line(buffer)
                            buffer = ""
                            if frame is not None:
                                finished, normalized = normalize_frame(frame)
                                if normalized is not None:
                                    yield normalized
                                if finished:
                                    break
                        if not finished and data_lines:
                            frame = data_lines
                            data_lines = []
                            finished, normalized = normalize_frame(frame)
                            if normalized is not None:
                                yield normalized
                            if finished:
                                break
                        break
            finally:
                await response_context.__aexit__(None, None, None)
    except LLMException:
        raise
    except Exception as exc:
        if _is_timeout_exception(exc):
            raise LLMException(ERR_LLM_STREAM_TIMEOUT, timeout=timeout) from exc
        raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(exc)) from exc
