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
from app.core.utils.http_proxy import build_aiohttp_proxy_kwargs
from app.core.utils.model_request_headers import build_model_request_headers
from app.models.message import FilePart, ImagePart, InternalMessage, InternalResponse, InternalToolCall, MessageRole, TextPart

from .chat_completions import OpenAIChatCompletionsTransformer, _is_timeout_exception

logger = get_logger(__name__)


class OpenAIResponsesTransformer(OpenAIChatCompletionsTransformer):
    _PROTOCOL_METADATA = "openai_responses"

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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        headers = build_model_request_headers(api_key, custom_headers)
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
            proxy_kwargs = build_aiohttp_proxy_kwargs(http_proxy)
            async with aiohttp.ClientSession(
                timeout=client_timeout,
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, headers=headers, json=payload, **proxy_kwargs) as resp:
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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs,
    ) -> AsyncGenerator[dict[str, Any]]:
        headers = build_model_request_headers(api_key, custom_headers)
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
        text_delta_indexes: set[tuple[int | str | None, int | str | None]] = set()
        text_fallback_indexes: set[tuple[int | str | None, int | str | None]] = set()
        refusal_delta_indexes: set[tuple[int | str | None, int | str | None]] = set()
        refusal_fallback_indexes: set[tuple[int | str | None, int | str | None]] = set()
        loop = asyncio.get_event_loop()
        deadline = loop.time() + timeout
        try:
            proxy_kwargs = build_aiohttp_proxy_kwargs(http_proxy)
            async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=None),
                connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                resp_cm = session.post(url, headers=headers, json=payload, **proxy_kwargs)
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
                                text_delta_indexes=text_delta_indexes,
                                text_fallback_indexes=text_fallback_indexes,
                                refusal_delta_indexes=refusal_delta_indexes,
                                refusal_fallback_indexes=refusal_fallback_indexes,
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

            if role == MessageRole.ASSISTANT.value and message.tool_calls:
                metadata = message.provider_metadata or {}
                if metadata.get("protocol") == cls._PROTOCOL_METADATA:
                    output_items = metadata.get("output")
                    if isinstance(output_items, list):
                        provider_items.extend(dict(item) for item in output_items if isinstance(item, dict) and item.get("type") == "reasoning")

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
                    metadata = tool_call.provider_metadata or {}
                    raw_item = metadata.get("item") if metadata.get("protocol") == cls._PROTOCOL_METADATA else None
                    provider_item = dict(raw_item) if isinstance(raw_item, dict) else {}
                    provider_item.update(
                        {
                            "type": "function_call",
                            "call_id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": json.dumps(tool_call.arguments, ensure_ascii=False, separators=(",", ":")),
                        }
                    )
                    provider_items.append(provider_item)
        return provider_items

    @classmethod
    def from_provider(cls, provider_response: Any) -> InternalMessage:
        output = provider_response.get("output") if isinstance(provider_response, dict) else None
        if not isinstance(output, list):
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)

        text_parts: list[str] = []
        refusal_parts: list[str] = []
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
                text_parts.extend(message_texts)
                refusal_parts.extend(refusals)
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
                        provider_metadata=cls._responses_tool_call_provider_metadata(item),
                    )
                )

        refusal = "".join(refusal_parts) or None
        content = "".join(text_parts) or refusal
        if not content and not tool_calls and provider_response.get("status") != "incomplete":
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)
        return InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content or None,
            refusal=refusal,
            provider_metadata=cls._responses_message_provider_metadata(output),
            tool_calls=tool_calls or None,
        )

    @classmethod
    def to_internal_response(cls, provider_response: Any, default_model: str) -> InternalResponse:
        if not isinstance(provider_response, dict):
            return super().to_internal_response(provider_response, default_model)
        if provider_response.get("status") == "failed" or provider_response.get("error"):
            cls._raise_response_error(provider_response)

        message = cls.from_provider(provider_response)
        finish_reason, finish_details = cls._responses_finish(provider_response, message)
        model = provider_response.get("model")
        return InternalResponse(
            message=message,
            model=str(model) if model is not None else default_model,
            usage=cls._normalize_responses_usage(provider_response.get("usage")),
            finish_reason=finish_reason,
            finish_details=finish_details,
            provider_metadata=cls._responses_provider_metadata(provider_response),
        )

    @classmethod
    def _responses_tool_call_provider_metadata(cls, item: dict[str, Any]) -> dict[str, Any] | None:
        metadata = {key: value for key, value in item.items() if key not in {"call_id", "name", "arguments"}}
        if not metadata:
            return None
        return {"protocol": cls._PROTOCOL_METADATA, "item": metadata}

    @classmethod
    def _responses_output_metadata(cls, output: Any) -> list[dict[str, Any]]:
        if not isinstance(output, list):
            return []

        output_metadata: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "function_call":
                continue
            if item_type != "message":
                output_metadata.append(dict(item))
                continue

            item_metadata = {key: value for key, value in item.items() if key != "content"}
            content_metadata: list[dict[str, Any]] = []
            for part in item.get("content") or []:
                if not isinstance(part, dict):
                    continue
                part_type = part.get("type")
                if part_type == "output_text":
                    part_metadata = {key: value for key, value in part.items() if key != "text"}
                elif part_type == "refusal":
                    part_metadata = {key: value for key, value in part.items() if key != "refusal"}
                else:
                    part_metadata = dict(part)
                if len(part_metadata) > 1 or part_type not in {"output_text", "refusal"}:
                    content_metadata.append(part_metadata)
            if content_metadata:
                item_metadata["content"] = content_metadata
            output_metadata.append(item_metadata)
        return output_metadata

    @classmethod
    def _responses_message_provider_metadata(cls, output: Any) -> dict[str, Any] | None:
        output_metadata = cls._responses_output_metadata(output)
        if not output_metadata:
            return None
        return {
            "protocol": cls._PROTOCOL_METADATA,
            "output": output_metadata,
        }

    @classmethod
    def _responses_provider_metadata(cls, response: dict[str, Any]) -> dict[str, Any] | None:
        response_metadata = {key: value for key, value in response.items() if key not in {"output", "model", "usage", "status", "incomplete_details", "error"}}
        if not response_metadata:
            return None
        return {
            "protocol": cls._PROTOCOL_METADATA,
            "response": response_metadata,
        }

    @classmethod
    def _responses_finish(
        cls,
        response: dict[str, Any],
        message: InternalMessage | None = None,
    ) -> tuple[str | None, dict[str, Any] | None]:
        status = response.get("status")
        incomplete_details = response.get("incomplete_details")
        error = response.get("error")

        if status == "completed":
            if message is not None:
                has_tool_calls = bool(message.tool_calls)
                has_refusal = bool(message.refusal)
            else:
                output = response.get("output")
                has_tool_calls = isinstance(output, list) and any(isinstance(item, dict) and item.get("type") == "function_call" for item in output)
                has_refusal = cls._responses_output_has_refusal(output)
            raw_reason = "tool_calls" if has_tool_calls else "refusal" if has_refusal else "stop"
        elif status == "incomplete":
            raw_reason = incomplete_details.get("reason") if isinstance(incomplete_details, dict) else incomplete_details
            raw_reason = raw_reason or "incomplete"
        else:
            raw_reason = status

        finish_reason, normalized_details = cls._normalize_finish_reason(raw_reason)
        details = dict(normalized_details or {})
        if status is not None:
            details["status"] = status
        if incomplete_details is not None:
            details["incomplete_details"] = incomplete_details
        if error is not None:
            details["error"] = error
        return finish_reason, details or None

    @staticmethod
    def _responses_output_has_refusal(output: Any) -> bool:
        if not isinstance(output, list):
            return False
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            if any(isinstance(part, dict) and part.get("type") == "refusal" and part.get("refusal") for part in item.get("content") or []):
                return True
        return False

    @staticmethod
    def _raise_response_error(response: dict[str, Any]) -> None:
        official_error = response.get("error") or response.get("status") or response
        raise LLMException(ERR_LLM_CONNECTION_FAILED, error=official_error, detail=response.get("error") or response)

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
            "include": ["reasoning.encrypted_content"],
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
        text_delta_indexes: set[tuple[int | str | None, int | str | None]] | None = None,
        text_fallback_indexes: set[tuple[int | str | None, int | str | None]] | None = None,
        refusal_delta_indexes: set[tuple[int | str | None, int | str | None]] | None = None,
        refusal_fallback_indexes: set[tuple[int | str | None, int | str | None]] | None = None,
    ) -> tuple[dict[str, Any] | None, bool]:
        if not isinstance(event, dict):
            return None, False
        text_delta_indexes = text_delta_indexes if text_delta_indexes is not None else set()
        text_fallback_indexes = text_fallback_indexes if text_fallback_indexes is not None else set()
        refusal_delta_indexes = refusal_delta_indexes if refusal_delta_indexes is not None else set()
        refusal_fallback_indexes = refusal_fallback_indexes if refusal_fallback_indexes is not None else set()
        event_type = event.get("type")
        if event_type in {"response.failed", "error"}:
            cls._raise_event_error(event)

        if event_type == "response.output_text.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                return None, False
            text_delta_indexes.add(cls._content_part_index(event))
            return {"choices": [{"delta": {"content": delta}}]}, True

        if event_type == "response.output_text.done":
            content_index = cls._content_part_index(event)
            if content_index in text_delta_indexes or content_index in text_fallback_indexes:
                return None, False
            text = event.get("text")
            if not isinstance(text, str):
                text = event.get("delta")
            if not isinstance(text, str):
                return None, False
            text_fallback_indexes.add(content_index)
            return {"choices": [{"delta": {"content": text}}]}, True

        if event_type == "response.refusal.delta":
            delta = event.get("delta")
            if not isinstance(delta, str):
                return None, False
            refusal_delta_indexes.add(cls._content_part_index(event))
            return {"choices": [{"delta": {"refusal": delta}}]}, True

        if event_type == "response.refusal.done":
            content_index = cls._content_part_index(event)
            if content_index in refusal_delta_indexes or content_index in refusal_fallback_indexes:
                return None, False
            refusal = event.get("refusal")
            if not isinstance(refusal, str):
                refusal = event.get("delta")
            if not isinstance(refusal, str):
                return None, False
            refusal_fallback_indexes.add(content_index)
            return {"choices": [{"delta": {"refusal": refusal}}]}, True

        if event_type == "response.output_item.added":
            item = event.get("item")
            if not isinstance(item, dict) or item.get("type") != "function_call":
                return None, False
            output_index = cls._output_index(event)
            tool_call = {
                "index": output_index,
                "id": item.get("call_id") or item.get("id"),
                "type": "function",
                "function": {"name": item.get("name")},
                "provider_metadata": cls._responses_tool_call_provider_metadata(item),
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
            tool_call = {
                "index": output_index,
                "type": "function",
                "provider_metadata": cls._responses_tool_call_provider_metadata(item),
            }
            if output_index in argument_delta_indexes or output_index in argument_fallback_indexes:
                return {"choices": [{"delta": {"tool_calls": [tool_call]}}]}, False
            argument_fallback_indexes.add(output_index)
            arguments = item.get("arguments")
            if not isinstance(arguments, str):
                arguments = cls._stringify(arguments) if arguments is not None else ""
            tool_call["function"] = {"arguments": arguments}
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

        if event_type in {"response.completed", "response.incomplete"}:
            response = event.get("response")
            response = dict(response) if isinstance(response, dict) else {}
            response.setdefault("status", "completed" if event_type == "response.completed" else "incomplete")
            if response.get("status") == "failed" or response.get("error"):
                cls._raise_response_error(response)
            finish_reason, finish_details = cls._responses_finish(response)
            return {
                "choices": [{"delta": {}, "finish_reason": finish_reason}],
                "model": response.get("model"),
                "usage": cls._normalize_responses_usage(response.get("usage")),
                "finish_details": finish_details,
                "provider_metadata": cls._responses_provider_metadata(response),
                "message_provider_metadata": cls._responses_message_provider_metadata(response.get("output")),
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

    @classmethod
    def _content_part_index(cls, event: dict[str, Any]) -> tuple[int | str | None, int | str | None]:
        content_index = event.get("content_index")
        if isinstance(content_index, bool):
            content_index = str(content_index)
        elif not isinstance(content_index, (int, str)) and content_index is not None:
            content_index = str(content_index)
        return cls._output_index(event), content_index

    @staticmethod
    def _raise_event_error(event: dict[str, Any]) -> None:
        event_type = event.get("type")
        response = event.get("response") if isinstance(event.get("response"), dict) else {}
        if event_type == "response.failed":
            official_error = response.get("error") or event.get("error") or event_type
            official_detail = response.get("error") or event.get("detail") or response
        else:
            official_error = event.get("error") or event
            official_detail = event.get("detail") or event.get("message") or official_error
        raise LLMException(ERR_LLM_CONNECTION_FAILED, error=official_error, detail=official_detail)
