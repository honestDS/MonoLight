import asyncio
import codecs
import json
from collections.abc import AsyncGenerator
from typing import Any

import aiohttp

from app.core.constants import (
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    ERR_LLM_CONNECTION_FAILED,
    ERR_LLM_EMPTY_RESPONSE,
    ERR_LLM_FIRST_CHAR_TIMEOUT,
    ERR_LLM_STREAM_TIMEOUT,
)
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.models.message import FilePart, ImagePart, InternalMessage, InternalToolCall, MessageRole, TextPart

from .openai import OpenAITransformer, _is_timeout_exception

logger = get_logger(__name__)


class OpenAIResponsesTransformer(OpenAITransformer):
    async def generate(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        temperature: float | None = 0.7,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        timeout: float = 60.0,
        **kwargs,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = self._request_payload(
            model_id=model_id,
            messages=messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            top_p=kwargs.get("top_p"),
        )
        url = f"{base_url.rstrip('/')}/responses"
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
                    parsed["usage"] = self._normalize_responses_usage(parsed.get("usage"))
                    return parsed
        except LLMException:
            raise
        except Exception as e:
            logger.bind(model_id=model_id, base_url=base_url, stream=False).error(t("LOG_OPENAI_CHAT_FAILED", error=str(e)))
            if _is_timeout_exception(e):
                raise LLMException(ERR_LLM_FIRST_CHAR_TIMEOUT, timeout=timeout) from e
            raise LLMException(ERR_LLM_CONNECTION_FAILED, detail=str(e))

    async def generate_stream(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        temperature: float | None = 0.7,
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
        payload = self._request_payload(
            model_id=model_id,
            messages=messages,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            top_p=kwargs.get("top_p"),
        )
        url = f"{base_url.rstrip('/')}/responses"
        argument_delta_indexes: set[int | str | None] = set()
        argument_fallback_indexes: set[int | str | None] = set()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        try:
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None),
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                resp_cm = session.post(url, headers=headers, json=payload)
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
                            raw_bytes = await asyncio.wait_for(
                                chunk_iter.__anext__(),
                                timeout=max(deadline - loop.time(), 0.001),
                            )
                        except TimeoutError:
                            raise LLMException(ERR_LLM_STREAM_TIMEOUT, timeout=timeout)
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
                            except Exception as json_err:
                                logger.bind(model_id=model_id, base_url=base_url).warning(t("LOG_OPENAI_SSE_PARSE_FAILED", raw_line=raw_line, error=str(json_err)))
                                continue

                            chunk, has_payload = self._normalize_stream_event(
                                event,
                                argument_delta_indexes=argument_delta_indexes,
                                argument_fallback_indexes=argument_fallback_indexes,
                            )
                            if chunk is None:
                                continue
                            if has_payload:
                                deadline = loop.time() + timeout
                            yield chunk
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

    @classmethod
    def to_provider(cls, internal_messages: list[InternalMessage], **kwargs) -> list[dict[str, Any]]:
        provider_items: list[dict[str, Any]] = []
        for message in internal_messages:
            role = getattr(message.role, "value", message.role)
            role = str(role).lower()

            if role == MessageRole.TOOL.value:
                provider_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": cls._content_as_text(message.content),
                    }
                )
                continue

            content = cls._content_as_input(message.content)
            if not message.tool_calls or content:
                provider_items.append({"role": role, "content": content})

            if role == MessageRole.ASSISTANT.value and message.tool_calls:
                for tool_call in message.tool_calls:
                    provider_items.append(
                        {
                            "type": "function_call",
                            "call_id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": json.dumps(tool_call.arguments, ensure_ascii=False, separators=(",", ":")),
                        }
                    )
        return provider_items

    @classmethod
    def from_provider(cls, provider_response: Any) -> InternalMessage:
        output = provider_response.get("output") if isinstance(provider_response, dict) else None
        if not isinstance(output, list):
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)

        text_parts: list[str] = []
        tool_calls: list[InternalToolCall] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "message":
                message_texts: list[str] = []
                refusals: list[str] = []
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        message_texts.append(part["text"])
                    elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                        refusals.append(part["refusal"])
                text_parts.extend(message_texts or refusals)
            elif item.get("type") == "function_call":
                raw_arguments = item.get("arguments")
                if isinstance(raw_arguments, dict):
                    arguments = raw_arguments
                else:
                    try:
                        parsed_arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) and raw_arguments.strip() else {}
                        arguments = parsed_arguments if isinstance(parsed_arguments, dict) else {}
                    except (TypeError, ValueError) as e:
                        logger.bind(tool_call=item).warning(t("LOG_OPENAI_TOOL_ARGS_PARSE_FAILED", error=str(e)))
                        arguments = {}
                tool_calls.append(
                    InternalToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        arguments=arguments,
                    )
                )

        content = "".join(text_parts)
        if not content and not tool_calls:
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)
        return InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content or None,
            tool_calls=tool_calls or None,
        )

    @classmethod
    def _request_payload(
        cls,
        *,
        model_id: str,
        messages: list[InternalMessage],
        stream: bool,
        temperature: float | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        tool_choice: str,
        top_p: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_id,
            "input": cls.to_provider(messages),
            "stream": stream,
            "store": False,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if max_tokens > 0:
            payload["max_output_tokens"] = max_tokens
        if tools:
            payload["tools"] = cls._convert_tools(tools)
            payload["tool_choice"] = tool_choice
        return payload

    @staticmethod
    def _convert_tools(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools:
            function = tool.get("function") if isinstance(tool, dict) else None
            if not isinstance(function, dict) or tool.get("type") != "function":
                converted.append(tool)
                continue
            converted_tool: dict[str, Any] = {
                "type": "function",
                "name": function.get("name"),
                "parameters": function.get("parameters", {}),
                "strict": function["strict"] if "strict" in function else False,
            }
            if function.get("description") is not None:
                converted_tool["description"] = function["description"]
            converted.append(converted_tool)
        return converted

    @classmethod
    def _content_as_input(cls, content: Any) -> Any:
        if not isinstance(content, list):
            return content
        converted: list[dict[str, Any]] = []
        for part in content:
            part_type = getattr(part, "type", None)
            if isinstance(part, dict):
                part_type = part.get("type")
            if isinstance(part, TextPart) or part_type == "text":
                text = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                converted.append({"type": "input_text", "text": text})
            elif isinstance(part, ImagePart) or part_type == "image_url":
                image_url = part.get("image_url", {}) if isinstance(part, dict) else getattr(part, "image_url", {})
                url = image_url.get("url", "") if isinstance(image_url, dict) else str(image_url)
                converted.append({"type": "input_image", "image_url": url, "detail": "auto"})
            elif isinstance(part, FilePart) or part_type == "file":
                path = part.get("path", "") if isinstance(part, dict) else getattr(part, "path", "")
                converted.append({"type": "input_text", "text": f"[Attached File: {path}]"})
            else:
                dumped = part.model_dump(mode="json") if hasattr(part, "model_dump") else part
                if isinstance(dumped, dict) and dumped.get("type") in {"input_text", "input_image"}:
                    converted.append(dumped)
                else:
                    converted.append({"type": "input_text", "text": cls._stringify(dumped)})
        return converted

    @classmethod
    def _content_as_text(cls, content: Any) -> str:
        if isinstance(content, str):
            return content
        if content is None:
            return ""
        if not isinstance(content, list):
            return cls._stringify(content)

        text_parts: list[str] = []
        for part in content:
            part_type = getattr(part, "type", None)
            if isinstance(part, dict):
                part_type = part.get("type")
            if isinstance(part, TextPart) or part_type in {"text", "input_text", "output_text"}:
                text = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                text_parts.append(str(text))
            elif isinstance(part, FilePart) or part_type == "file":
                path = part.get("path", "") if isinstance(part, dict) else getattr(part, "path", "")
                text_parts.append(f"[Attached File: {path}]")
            else:
                dumped = part.model_dump(mode="json") if hasattr(part, "model_dump") else part
                text_parts.append(cls._stringify(dumped))
        return "".join(text_parts)

    @staticmethod
    def _stringify(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return str(value)

    @classmethod
    def _normalize_responses_usage(cls, usage: Any) -> dict[str, Any]:
        normalized = dict(usage) if isinstance(usage, dict) else {}
        input_details = normalized.get("input_tokens_details")
        cached_tokens = input_details.get("cached_tokens") if isinstance(input_details, dict) else None
        normalized.update(
            {
                "prompt_tokens": cls._nonnegative_token_count(normalized.get("input_tokens")),
                "completion_tokens": cls._nonnegative_token_count(normalized.get("output_tokens")),
                "total_tokens": cls._nonnegative_token_count(normalized.get("total_tokens")),
                "cached_tokens": cls._nonnegative_token_count(cached_tokens),
            }
        )
        return normalized

    @classmethod
    def _normalize_stream_event(
        cls,
        event: Any,
        *,
        argument_delta_indexes: set[int | str | None],
        argument_fallback_indexes: set[int | str | None],
    ) -> tuple[dict[str, Any] | None, bool]:
        if not isinstance(event, dict):
            return None, False
        event_type = event.get("type")
        if event_type in {"response.failed", "response.incomplete", "error"}:
            cls._raise_event_error(event)

        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                return None, False
            return {"choices": [{"delta": {"content": delta}}]}, True

        if event_type == "response.output_item.added":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "function_call":
                return None, False
            output_index = cls._output_index(event)
            tool_call = {
                "index": output_index,
                "id": item.get("call_id"),
                "type": "function",
                "function": {"name": item.get("name")},
            }
            return {"choices": [{"delta": {"tool_calls": [tool_call]}}]}, True

        if event_type == "response.function_call_arguments.delta":
            output_index = cls._output_index(event)
            argument_delta_indexes.add(output_index)
            delta = event.get("delta")
            if not isinstance(delta, str):
                delta = cls._stringify(delta) if delta is not None else ""
            tool_call = {
                "index": output_index,
                "type": "function",
                "function": {"arguments": delta},
            }
            return {"choices": [{"delta": {"tool_calls": [tool_call]}}]}, True

        if event_type == "response.output_item.done":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "function_call":
                return None, False
            output_index = cls._output_index(event)
            if output_index in argument_delta_indexes or output_index in argument_fallback_indexes:
                return None, False
            argument_fallback_indexes.add(output_index)
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = cls._stringify(arguments) if arguments is not None else ""
            tool_call = {
                "index": output_index,
                "type": "function",
                "function": {"arguments": arguments},
            }
            return {"choices": [{"delta": {"tool_calls": [tool_call]}}]}, True

        if event_type == "response.function_call_arguments.done":
            output_index = cls._output_index(event)
            if output_index in argument_delta_indexes or output_index in argument_fallback_indexes:
                return None, False
            argument_fallback_indexes.add(output_index)
            arguments = event.get("arguments")
            if not isinstance(arguments, str):
                arguments = cls._stringify(arguments) if arguments is not None else ""
            tool_call = {
                "index": output_index,
                "type": "function",
                "function": {"arguments": arguments},
            }
            return {"choices": [{"delta": {"tool_calls": [tool_call]}}]}, True

        if event_type == "response.completed":
            response = event.get("response")
            response = response if isinstance(response, dict) else {}
            return {
                "choices": [],
                "model": response.get("model"),
                "usage": cls._normalize_responses_usage(response.get("usage")),
            }, False

        return None, False

    @staticmethod
    def _output_index(event: dict[str, Any]) -> int | str | None:
        output_index = event.get("output_index")
        if isinstance(output_index, bool):
            return str(output_index)
        if isinstance(output_index, (int, str)) or output_index is None:
            return output_index
        return str(output_index)

    @staticmethod
    def _raise_event_error(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        if event_type == "response.incomplete":
            official_detail = response.get("incomplete_details") or event.get("detail") or response
            official_error = response.get("error") or official_detail
        elif event_type == "response.failed":
            official_error = response.get("error") or event.get("error") or event_type
            official_detail = response.get("error") or event.get("detail") or response
        else:
            official_error = event.get("error") or event
            official_detail = event.get("detail") or event.get("message") or official_error
        raise LLMException(ERR_LLM_CONNECTION_FAILED, error=official_error, detail=official_detail)
