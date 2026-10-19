import json
from collections.abc import AsyncGenerator
from copy import deepcopy
from typing import Any

from app.core.constants import (
    DEEPSEEK_RESPONSES_REASONING_PLACEHOLDER,
    ERR_LLM_API_RESPONSE_ERROR,
    ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS,
    ERR_LLM_EMPTY_RESPONSE,
    ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED,
)
from app.core.exceptions import LLMContextLengthException, LLMException
from app.core.utils.llm_request_policy import build_upstream_rejection
from app.core.utils.model_request_headers import build_model_request_headers
from app.models.message import (
    AudioPart,
    FilePart,
    ImagePart,
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    MessageRole,
    TextPart,
    build_provider_source,
)
from app.transformers import http_transport
from app.transformers.base import BaseTransformer


class DeepSeekResponsesTransformer(BaseTransformer):
    _PROTOCOL = "deepseek_responses"
    _CONTEXT_LENGTH_ERROR_CODE = "context_length_exceeded"
    _REASONING_SOURCE_PROTOCOLS = frozenset(
        {
            "deepseek",
            "deepseek_responses",
            "openai",
            "openai_responses",
        }
    )

    @classmethod
    def build_input_token_payload(
        cls,
        *,
        model_id: str | None,
        channel_id: int | None = None,
        messages: list[InternalMessage],
        tools: list[dict[str, Any]] | None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "input": cls.to_provider(messages, model_id=model_id, channel_id=channel_id),
        }
        converted_tools = cls._convert_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
        return payload

    async def list_models(
        self,
        api_key: str,
        base_url: str,
        timeout: float = 30.0,
        http_proxy: str | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        return await http_transport.list_models(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            http_proxy=http_proxy,
            raise_error=self._raise_http_error,
        )

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
        channel_id = kwargs.get("channel_id")
        payload = self._request_payload(
            model_id=model_id,
            channel_id=channel_id,
            messages=messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            top_p=kwargs.get("top_p"),
            reasoning_effort=kwargs.get("reasoning_effort"),
        )
        return await http_transport.post_json(
            url=f"{base_url.rstrip('/')}/responses",
            headers=build_model_request_headers(api_key, custom_headers),
            payload=payload,
            timeout=timeout,
            http_proxy=http_proxy,
            raise_error=self._raise_http_error,
        )

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
        channel_id = kwargs.get("channel_id")
        payload = self._request_payload(
            model_id=model_id,
            channel_id=channel_id,
            messages=messages,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            top_p=kwargs.get("top_p"),
            reasoning_effort=kwargs.get("reasoning_effort"),
        )
        text_values: dict[tuple[Any, Any], str] = {}
        reasoning_values: dict[tuple[Any, Any], str] = {}
        refusal_values: dict[tuple[Any, Any], str] = {}
        tools_by_index: dict[int, dict[str, Any]] = {}
        tools_by_alias: dict[str, dict[str, Any]] = {}
        reasoning_done_items: list[tuple[Any, dict[str, Any]]] = []

        def output_index(event: dict[str, Any]) -> Any:
            value = event.get("output_index")
            if isinstance(value, bool):
                return str(value)
            if isinstance(value, (int, str)) or value is None:
                return value
            return str(value)

        def content_key(event: dict[str, Any], content_index: Any = None) -> tuple[Any, Any]:
            index = event.get("content_index", content_index)
            if index is None:
                index = 0
            if isinstance(index, bool):
                index = str(index)
            elif not isinstance(index, (int, str)):
                index = str(index)
            return output_index(event), index

        def add_delta(values: dict[tuple[Any, Any], str], key: tuple[Any, Any], text: str) -> str:
            if not text:
                return ""
            values[key] = values.get(key, "") + text
            return text

        def add_snapshot(values: dict[tuple[Any, Any], str], key: tuple[Any, Any], text: str) -> str:
            previous = values.get(key, "")
            if text.startswith(previous):
                suffix = text[len(previous) :]
            else:
                suffix = ""
            if suffix:
                values[key] = previous + suffix
            return suffix

        def tool_index(value: Any) -> int | None:
            if isinstance(value, bool) or value is None:
                return None
            try:
                return int(value)
            except (TypeError, ValueError):
                return None

        def get_tool_state(
            event: dict[str, Any],
            item: dict[str, Any] | None = None,
            *,
            fallback_index: int | None = None,
        ) -> dict[str, Any]:
            item = item if isinstance(item, dict) else {}
            raw_index = event.get("output_index")
            if raw_index is None:
                raw_index = fallback_index
            index = tool_index(raw_index)
            call_id = event.get("call_id") or item.get("call_id")
            item_id = event.get("item_id") or item.get("id")
            aliases = [str(value) for value in (call_id, item_id) if value]

            state = tools_by_index.get(index) if index is not None else None
            if state is None:
                state = next((tools_by_alias[alias] for alias in aliases if alias in tools_by_alias), None)
            if state is None:
                if index is None:
                    index = 0
                    while index in tools_by_index:
                        index += 1
                state = {
                    "index": index,
                    "call_id": None,
                    "item_id": None,
                    "name": "",
                    "arguments": "",
                    "item": {},
                }
                tools_by_index[index] = state
            elif index is not None:
                tools_by_index[index] = state

            if call_id and not state["call_id"]:
                state["call_id"] = str(call_id)
            if item_id and not state["item_id"]:
                state["item_id"] = str(item_id)
            for alias in aliases:
                tools_by_alias[alias] = state
            if item:
                state["item"].update(deepcopy(item))
                if isinstance(item.get("name"), str):
                    state["name"] = item["name"]
            return state

        def tool_id(state: dict[str, Any], *, allow_item_id: bool = False) -> str | None:
            return state["call_id"] or (state["item_id"] if allow_item_id else None)

        def argument_text(value: Any) -> str:
            if isinstance(value, str):
                return value
            return self._stringify(value) if value is not None else ""

        def tool_metadata(state: dict[str, Any]) -> dict[str, Any] | None:
            return self._tool_call_provider_metadata(state["item"])

        def update_item_snapshot(
            item: dict[str, Any],
            event: dict[str, Any],
            *,
            fallback_index: int | None = None,
        ) -> tuple[dict[str, Any], bool]:
            delta: dict[str, Any] = {}
            substantive = False
            item_type = item.get("type")
            index = output_index(event)
            if index is None and fallback_index is not None:
                index = fallback_index

            if item_type == "reasoning":
                reasoning_done_items.append((index, deepcopy(item)))
                for content_index, part in enumerate(item.get("content") or []):
                    if not isinstance(part, dict) or part.get("type") != "reasoning_text":
                        continue
                    text = part.get("text")
                    if not isinstance(text, str):
                        continue
                    key = (index, content_index)
                    suffix = add_snapshot(reasoning_values, key, text)
                    if suffix:
                        delta["reasoning_content"] = delta.get("reasoning_content", "") + suffix
                        substantive = True
                return delta, substantive

            if item_type == "message":
                for content_index, part in enumerate(item.get("content") or []):
                    if not isinstance(part, dict):
                        continue
                    part_type = part.get("type")
                    if part_type == "output_text" and isinstance(part.get("text"), str):
                        key = (index, content_index)
                        suffix = add_snapshot(text_values, key, part["text"])
                        if suffix:
                            delta["content"] = delta.get("content", "") + suffix
                            substantive = True
                    elif part_type == "refusal" and isinstance(part.get("refusal"), str):
                        key = (index, content_index)
                        suffix = add_snapshot(refusal_values, key, part["refusal"])
                        if suffix:
                            delta["refusal"] = delta.get("refusal", "") + suffix
                            substantive = True
                return delta, substantive

            if item_type == "function_call":
                state = get_tool_state(event, item, fallback_index=fallback_index)
                args = argument_text(item.get("arguments"))
                previous = state["arguments"]
                suffix = args[len(previous) :] if args.startswith(previous) else ""
                if suffix:
                    state["arguments"] = previous + suffix
                    delta["arguments"] = suffix
                    substantive = True
                call_id = tool_id(state, allow_item_id=True)
                if call_id:
                    delta["id"] = call_id
                    substantive = True
                if state["name"]:
                    delta["name"] = state["name"]
                    substantive = True
                if delta:
                    tool_delta: dict[str, Any] = {
                        "index": state["index"],
                        "type": "function",
                        "provider_metadata": tool_metadata(state),
                    }
                    if "id" in delta:
                        tool_delta["id"] = delta["id"]
                    function_delta: dict[str, Any] = {}
                    if "name" in delta:
                        function_delta["name"] = delta["name"]
                    if "arguments" in delta:
                        function_delta["arguments"] = delta["arguments"]
                    if function_delta:
                        tool_delta["function"] = function_delta
                    return {"tool_calls": [tool_delta]}, substantive
                return {}, False
            return {}, False

        def normalize_event(event: Any) -> tuple[dict[str, Any] | None, bool]:
            if not isinstance(event, dict):
                return None, False
            event_type = event.get("type")

            if event_type in {"response.failed", "error"}:
                self._raise_provider_error(event, status=None)

            if event_type in {"response.output_text.delta", "response.reasoning_text.delta", "response.refusal.delta"}:
                text = event.get("delta")
                if not isinstance(text, str) or not text:
                    return None, False
                if event_type == "response.output_text.delta":
                    values, key, field = text_values, content_key(event), "content"
                elif event_type == "response.reasoning_text.delta":
                    values, key, field = reasoning_values, content_key(event), "reasoning_content"
                else:
                    values, key, field = refusal_values, content_key(event), "refusal"
                add_delta(values, key, text)
                return {"choices": [{"delta": {field: text}}]}, True

            if event_type in {"response.output_text.done", "response.reasoning_text.done", "response.refusal.done"}:
                if event_type == "response.output_text.done":
                    text = event.get("text", event.get("delta"))
                    values, field = text_values, "content"
                elif event_type == "response.reasoning_text.done":
                    text = event.get("text", event.get("delta"))
                    values, field = reasoning_values, "reasoning_content"
                else:
                    text = event.get("refusal", event.get("delta"))
                    values, field = refusal_values, "refusal"
                if not isinstance(text, str):
                    return None, False
                suffix = add_snapshot(values, content_key(event), text)
                if not suffix:
                    return None, False
                return {"choices": [{"delta": {field: suffix}}]}, True

            if event_type == "response.output_item.added":
                item = event.get("item")
                if not isinstance(item, dict) or item.get("type") != "function_call":
                    return None, False
                state = get_tool_state(event, item)
                tool_delta: dict[str, Any] = {
                    "index": state["index"],
                    "type": "function",
                    "provider_metadata": tool_metadata(state),
                }
                call_id = tool_id(state)
                if call_id:
                    tool_delta["id"] = call_id
                if state["name"]:
                    tool_delta["function"] = {"name": state["name"]}
                if not call_id and not state["name"]:
                    return None, False
                return {"choices": [{"delta": {"tool_calls": [tool_delta]}}]}, True

            if event_type == "response.function_call_arguments.delta":
                state = get_tool_state(event)
                text = argument_text(event.get("delta"))
                if not text:
                    return None, False
                state["arguments"] += text
                tool_delta = {
                    "index": state["index"],
                    "type": "function",
                    "function": {"arguments": text},
                    "provider_metadata": tool_metadata(state),
                }
                call_id = tool_id(state)
                if call_id:
                    tool_delta["id"] = call_id
                return {"choices": [{"delta": {"tool_calls": [tool_delta]}}]}, True

            if event_type == "response.function_call_arguments.done":
                state = get_tool_state(event)
                final_arguments = argument_text(event.get("arguments"))
                suffix = final_arguments[len(state["arguments"]) :] if final_arguments.startswith(state["arguments"]) else ""
                if suffix:
                    state["arguments"] += suffix
                call_id = tool_id(state)
                tool_delta: dict[str, Any] = {
                    "index": state["index"],
                    "type": "function",
                    "provider_metadata": tool_metadata(state),
                }
                if call_id:
                    tool_delta["id"] = call_id
                if suffix:
                    tool_delta["function"] = {"arguments": suffix}
                if not suffix and not call_id:
                    return None, False
                return {"choices": [{"delta": {"tool_calls": [tool_delta]}}]}, bool(suffix or call_id)

            if event_type == "response.output_item.done":
                item = event.get("item")
                if not isinstance(item, dict):
                    return None, False
                delta, substantive = update_item_snapshot(item, event)
                if delta.get("tool_calls"):
                    return {"choices": [{"delta": delta}]}, substantive
                if delta:
                    return {"choices": [{"delta": delta}]}, substantive
                return None, False

            if event_type in {"response.completed", "response.incomplete"}:
                response = event.get("response")
                response = dict(response) if isinstance(response, dict) else {}
                response.setdefault("status", "completed" if event_type == "response.completed" else "incomplete")
                if response.get("status") == "failed" or response.get("error") is not None:
                    self._raise_provider_error(response, status=None)

                delta: dict[str, Any] = {}
                output = response.get("output")
                has_tool_calls = False
                if isinstance(output, list):
                    for index, item in enumerate(output):
                        if not isinstance(item, dict):
                            continue
                        if item.get("type") == "function_call":
                            has_tool_calls = True
                        item_delta, _ = update_item_snapshot(item, {"output_index": index}, fallback_index=index)
                        for field in ("content", "reasoning_content", "refusal"):
                            if item_delta.get(field):
                                delta[field] = delta.get(field, "") + item_delta[field]
                        if item.get("type") == "function_call" and item_delta.get("tool_calls"):
                            delta.setdefault("tool_calls", []).extend(item_delta["tool_calls"])

                finish_reason, finish_details = self._responses_finish(
                    response,
                    has_tool_calls=has_tool_calls or bool(tools_by_index),
                )
                normalized = {
                    "choices": [{"delta": delta, "finish_reason": finish_reason}],
                    "model": response.get("model"),
                    "usage": self._normalize_responses_usage(response.get("usage")),
                    "finish_details": finish_details,
                    "provider_metadata": self._responses_provider_metadata(response),
                    "message_provider_metadata": self._responses_message_provider_metadata(
                        output,
                        reasoning_done_items=reasoning_done_items,
                    ),
                }
                substantive = any(bool(delta.get(field)) for field in ("content", "reasoning_content", "refusal", "tool_calls"))
                return normalized, substantive

            return None, False

        async for chunk in http_transport.stream_sse_json(
            url=f"{base_url.rstrip('/')}/responses",
            headers=build_model_request_headers(api_key, custom_headers),
            payload=payload,
            timeout=timeout,
            http_proxy=http_proxy,
            normalize_event=normalize_event,
            raise_error=self._raise_http_error,
            is_terminal=lambda event: isinstance(event, dict) and event.get("type") in {"response.completed", "response.incomplete", "response.failed"},
        ):
            yield chunk

    @classmethod
    def to_provider(cls, internal_messages: list[InternalMessage], **kwargs) -> list[dict[str, Any]]:
        channel_id = kwargs.get("channel_id")
        model_id = kwargs.get("model_id")
        provider_items: list[dict[str, Any]] = []
        for message in internal_messages:
            role = getattr(message.role, "value", message.role)
            role = str(role).lower()
            if role not in {
                MessageRole.USER.value,
                MessageRole.SYSTEM.value,
                MessageRole.ASSISTANT.value,
                MessageRole.TOOL.value,
            }:
                continue

            source_protocol = cls._compatible_source_protocol(
                message,
                channel_id=channel_id,
                model_id=model_id,
            )
            if role == MessageRole.ASSISTANT.value:
                reasoning_items = cls._outbound_reasoning(message, source_protocol)
                if not reasoning_items and (message.content or message.tool_calls):
                    reasoning_items = [cls._plain_reasoning_item("")]
                for item in reasoning_items:
                    if not cls._has_reasoning_text(item):
                        item["content"] = cls._plain_reasoning_item("")["content"]
                provider_items.extend(reasoning_items)

            if role == MessageRole.TOOL.value:
                provider_items.append(
                    {
                        "type": "function_call_output",
                        "call_id": message.tool_call_id,
                        "output": cls._content_as_tool_output(message.content),
                    }
                )
                continue

            content = cls._content_as_input(message.content)
            tool_calls = message.tool_calls if role == MessageRole.ASSISTANT.value else None
            if role != MessageRole.ASSISTANT.value or content:
                provider_items.append({"role": role, "content": content})

            if tool_calls:
                metadata = message.provider_metadata if isinstance(message.provider_metadata, dict) else {}
                same_protocol = source_protocol == cls._PROTOCOL and metadata.get("protocol") == cls._PROTOCOL
                for tool_call in tool_calls:
                    raw_item = None
                    tool_metadata = tool_call.provider_metadata
                    if same_protocol and isinstance(tool_metadata, dict) and tool_metadata.get("protocol") == cls._PROTOCOL:
                        candidate = tool_metadata.get("item")
                        raw_item = candidate if isinstance(candidate, dict) else None
                    item = cls._sanitize_tool_item(raw_item) if raw_item is not None else {}
                    item.update(
                        {
                            "type": "function_call",
                            "call_id": tool_call.id,
                            "name": tool_call.name,
                            "arguments": json.dumps(
                                tool_call.arguments,
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        }
                    )
                    provider_items.append(item)
        return provider_items

    @classmethod
    def from_provider(cls, provider_response: Any) -> InternalMessage:
        if not isinstance(provider_response, dict):
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)
        cls._raise_response_error_if_needed(provider_response)
        raw_output = provider_response.get("output")
        output = raw_output if isinstance(raw_output, list) else []
        text_parts: list[str] = []
        refusal_parts: list[str] = []
        reasoning_groups: list[str] = []
        has_reasoning_item = False
        has_reasoning_text = False
        tool_calls: list[InternalToolCall] = []

        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type == "reasoning":
                has_reasoning_item = True
                text_values = [part.get("text") for part in item.get("content") or [] if isinstance(part, dict) and part.get("type") == "reasoning_text" and isinstance(part.get("text"), str)]
                if text_values:
                    has_reasoning_text = True
                    reasoning_groups.append("".join(text_values))
            elif item_type == "message":
                for part in item.get("content") or []:
                    if not isinstance(part, dict):
                        continue
                    if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                        text_parts.append(part["text"])
                    elif part.get("type") == "refusal" and isinstance(part.get("refusal"), str):
                        refusal_parts.append(part["refusal"])
            elif item_type == "function_call":
                tool_calls.append(
                    InternalToolCall(
                        id=str(item.get("call_id") or item.get("id") or ""),
                        name=str(item.get("name") or ""),
                        arguments=cls._parse_arguments(item.get("arguments")),
                        provider_metadata=cls._tool_call_provider_metadata(item),
                    )
                )

        content = "".join(text_parts)
        refusal = "".join(refusal_parts) if refusal_parts else None
        if not content:
            content = refusal or ""
        reasoning_content = "\n\n".join(reasoning_groups) if has_reasoning_text else None
        if not content and not tool_calls and not has_reasoning_item and provider_response.get("status") != "incomplete":
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)

        return InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content or None,
            reasoning_content=reasoning_content,
            refusal=refusal,
            provider_metadata=cls._responses_message_provider_metadata(output),
            tool_calls=tool_calls or None,
        )

    @classmethod
    def to_internal_response(cls, provider_response: Any, default_model: str) -> InternalResponse:
        if not isinstance(provider_response, dict):
            raise LLMException(ERR_LLM_API_RESPONSE_ERROR, detail=provider_response)
        cls._raise_response_error_if_needed(provider_response)
        message = cls.from_provider(provider_response)
        finish_reason, finish_details = cls._responses_finish(
            provider_response,
            has_tool_calls=bool(message.tool_calls),
            has_refusal=bool(message.refusal),
        )
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
    def _compatible_source_protocol(
        cls,
        message: InternalMessage,
        *,
        channel_id: Any,
        model_id: Any,
    ) -> str | None:
        metadata = message.provider_metadata if isinstance(message.provider_metadata, dict) else {}
        raw_source = metadata.get("source")
        if not isinstance(raw_source, dict):
            return None
        source = build_provider_source(
            channel_id=raw_source.get("channel_id"),
            model_id=raw_source.get("model_id"),
            protocol=raw_source.get("protocol"),
        )
        target = build_provider_source(
            channel_id=channel_id,
            model_id=model_id,
            protocol=cls._PROTOCOL,
        )
        if source is None or target is None:
            return None
        if source["channel_id"] != target["channel_id"] or source["model_id"] != target["model_id"]:
            return None
        if source["protocol"] not in cls._REASONING_SOURCE_PROTOCOLS:
            return None
        metadata_protocol = metadata.get("protocol")
        if metadata_protocol != source["protocol"] and not (source["protocol"] == "openai" and metadata_protocol == "openai_chat_completions"):
            return None
        return source["protocol"]

    @classmethod
    def _outbound_reasoning(cls, message: InternalMessage, source_protocol: str | None) -> list[dict[str, Any]]:
        if source_protocol is None:
            return []
        metadata = message.provider_metadata if isinstance(message.provider_metadata, dict) else {}
        if source_protocol == cls._PROTOCOL:
            output = metadata.get("output")
            reasoning_items = [cls._sanitize_reasoning_item(item) for item in output or [] if isinstance(item, dict) and item.get("type") == "reasoning"] if isinstance(output, list) else []
            if reasoning_items:
                if not any(cls._has_reasoning_text(item) for item in reasoning_items):
                    if isinstance(message.reasoning_content, str) and message.reasoning_content.strip():
                        reasoning_items[0]["content"] = cls._plain_reasoning_item(message.reasoning_content)["content"]
                return reasoning_items
            if isinstance(message.reasoning_content, str):
                return [cls._plain_reasoning_item(message.reasoning_content)]
            return []

        if source_protocol in {"deepseek", "openai"}:
            found, text = cls._chat_reasoning(message)
            return [cls._plain_reasoning_item(text)] if found else []

        if source_protocol == "openai_responses":
            output = metadata.get("output")
            if not isinstance(output, list):
                return []
            converted: list[dict[str, Any]] = []
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "reasoning":
                    continue
                content = [{"type": "reasoning_text", "text": part["text"]} for part in item.get("content") or [] if isinstance(part, dict) and part.get("type") == "reasoning_text" and isinstance(part.get("text"), str)]
                if content:
                    converted.append({"type": "reasoning", "content": content})
            return converted
        return []

    @staticmethod
    def _chat_reasoning(message: InternalMessage) -> tuple[bool, str]:
        metadata = message.provider_metadata if isinstance(message.provider_metadata, dict) else {}
        raw_message = metadata.get("message")
        candidates = [message.reasoning_content]
        if isinstance(raw_message, dict):
            candidates.extend((raw_message.get("reasoning_content"), raw_message.get("reasoning")))
        string_candidates = [candidate for candidate in candidates if isinstance(candidate, str)]
        for candidate in string_candidates:
            if candidate.strip():
                return True, candidate
        return (True, "") if string_candidates else (False, "")

    @staticmethod
    def _plain_reasoning_item(text: str) -> dict[str, Any]:
        return {
            "type": "reasoning",
            "content": [
                {
                    "type": "reasoning_text",
                    "text": text if text.strip() else DEEPSEEK_RESPONSES_REASONING_PLACEHOLDER,
                }
            ],
        }

    @staticmethod
    def _has_reasoning_text(item: dict[str, Any]) -> bool:
        content = item.get("content")
        return isinstance(content, list) and any(isinstance(part, dict) and part.get("type") == "reasoning_text" and isinstance(part.get("text"), str) and bool(part["text"].strip()) for part in content)

    @staticmethod
    def _sanitize_reasoning_item(item: dict[str, Any]) -> dict[str, Any]:
        sanitized = {"type": "reasoning"}
        for field in ("id", "status", "content"):
            if field in item:
                sanitized[field] = deepcopy(item[field])
        return sanitized

    @staticmethod
    def _sanitize_tool_item(item: dict[str, Any]) -> dict[str, Any]:
        sanitized: dict[str, Any] = {"type": "function_call"}
        for field in ("id", "status"):
            if field in item:
                sanitized[field] = deepcopy(item[field])
        return sanitized

    @classmethod
    def _content_as_input(cls, content: Any) -> str | list[dict[str, Any]] | None:
        if content is None or isinstance(content, str):
            return content
        if not isinstance(content, list):
            return [{"type": "input_text", "text": cls._stringify(content)}]

        converted: list[dict[str, Any]] = []
        for part in content:
            part_type = part.get("type") if isinstance(part, dict) else getattr(part, "type", None)
            if isinstance(part, AudioPart) or part_type == "audio":
                raise LLMException(ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED)
            if isinstance(part, (TextPart,)) or part_type == "text":
                text = part.get("text", "") if isinstance(part, dict) else getattr(part, "text", "")
                converted.append({"type": "input_text", "text": text if isinstance(text, str) else cls._stringify(text)})
            elif isinstance(part, ImagePart) or part_type == "image_url":
                image = part.get("image_url", {}) if isinstance(part, dict) else getattr(part, "image_url", {})
                image_url = image.get("url", "") if isinstance(image, dict) else image
                converted.append({"type": "input_image", "image_url": image_url if isinstance(image_url, str) else cls._stringify(image_url)})
            elif isinstance(part, FilePart) or part_type == "file":
                path = part.get("path", "") if isinstance(part, dict) else getattr(part, "path", "")
                converted.append({"type": "input_text", "text": f"[Attached File:{path}]"})
            else:
                dumped = part.model_dump(mode="json") if hasattr(part, "model_dump") else part
                converted.append({"type": "input_text", "text": cls._stringify(dumped)})
        return converted

    @classmethod
    def _content_as_tool_output(cls, content: Any) -> Any:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return cls._content_as_input(content)
        return cls._stringify(content)

    @classmethod
    def _convert_tools(cls, tools: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
        converted: list[dict[str, Any]] = []
        for tool in tools or []:
            if not isinstance(tool, dict) or tool.get("type") != "function":
                continue
            function = tool.get("function")
            function = function if isinstance(function, dict) else tool
            name = function.get("name")
            if not isinstance(name, str) or not name:
                continue
            converted_tool: dict[str, Any] = {
                "type": "function",
                "name": name,
                "parameters": function.get("parameters", {}),
            }
            if function.get("description") is not None:
                converted_tool["description"] = function["description"]
            if "strict" in function:
                converted_tool["strict"] = function["strict"]
            converted.append(converted_tool)
        return converted

    @classmethod
    def _request_payload(
        cls,
        *,
        model_id: str,
        channel_id: int | None,
        messages: list[InternalMessage],
        stream: bool,
        temperature: float | None,
        max_tokens: int,
        tools: list[dict[str, Any]] | None,
        tool_choice: Any,
        top_p: Any,
        reasoning_effort: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_id,
            "input": cls.to_provider(messages, model_id=model_id, channel_id=channel_id),
            "stream": stream,
        }
        if temperature is not None:
            payload["temperature"] = temperature
        if top_p is not None:
            payload["top_p"] = top_p
        if reasoning_effort is not None:
            effort = reasoning_effort.strip() if isinstance(reasoning_effort, str) else str(reasoning_effort).strip()
            payload["reasoning"] = {"effort": effort}
        if isinstance(max_tokens, int) and not isinstance(max_tokens, bool) and max_tokens > 0:
            payload["max_output_tokens"] = max_tokens
        converted_tools = cls._convert_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools
            if isinstance(tool_choice, dict):
                function_choice = tool_choice.get("function")
                if isinstance(function_choice, dict):
                    name = function_choice.get("name")
                    payload["tool_choice"] = {"type": "function", "name": name} if name else tool_choice
                else:
                    payload["tool_choice"] = tool_choice
            else:
                payload["tool_choice"] = tool_choice
        else:
            payload["tool_choice"] = "none"
        return payload

    @classmethod
    def _responses_message_provider_metadata(
        cls,
        output: Any,
        *,
        reasoning_done_items: list[tuple[Any, dict[str, Any]]] | None = None,
    ) -> dict[str, Any]:
        reasoning_items_by_index: dict[int, dict[str, Any]] = {}
        fallback_items: list[dict[str, Any]] = []
        if isinstance(output, list):
            for index, item in enumerate(output):
                if isinstance(item, dict) and item.get("type") == "reasoning":
                    reasoning_items_by_index[index] = deepcopy(item)
        for index, item in reasoning_done_items or []:
            if isinstance(index, int) and not isinstance(index, bool):
                reasoning_items_by_index.setdefault(index, deepcopy(item))
            else:
                fallback_items.append(deepcopy(item))

        reasoning_items = [reasoning_items_by_index[index] for index in sorted(reasoning_items_by_index)]
        reasoning_items.extend(fallback_items)
        return {"protocol": cls._PROTOCOL, "output": reasoning_items}

    @classmethod
    def _tool_call_provider_metadata(cls, item: dict[str, Any]) -> dict[str, Any] | None:
        metadata = {key: deepcopy(value) for key, value in item.items() if key not in {"call_id", "name", "arguments"}}
        if not metadata:
            return None
        return {"protocol": cls._PROTOCOL, "item": metadata}

    @classmethod
    def _responses_provider_metadata(cls, response: dict[str, Any]) -> dict[str, Any]:
        metadata = {key: deepcopy(value) for key, value in response.items() if key not in {"output", "model", "usage", "status", "incomplete_details", "error"}}
        return {"protocol": cls._PROTOCOL, "response": metadata}

    @classmethod
    def _responses_finish(
        cls,
        response: dict[str, Any],
        *,
        has_tool_calls: bool = False,
        has_refusal: bool = False,
    ) -> tuple[str | None, dict[str, Any] | None]:
        status = response.get("status")
        incomplete_details = response.get("incomplete_details")
        if status == "completed":
            output = response.get("output")
            if isinstance(output, list):
                has_tool_calls = has_tool_calls or any(isinstance(item, dict) and item.get("type") == "function_call" for item in output)
                has_refusal = has_refusal or any(isinstance(part, dict) and part.get("type") == "refusal" and bool(part.get("refusal")) for item in output if isinstance(item, dict) and item.get("type") == "message" for part in item.get("content") or [])
            finish_reason = "tool_calls" if has_tool_calls else "refusal" if has_refusal else "stop"
        elif status == "incomplete":
            reason = incomplete_details.get("reason") if isinstance(incomplete_details, dict) else incomplete_details
            normalized_reason = str(reason or "incomplete").strip().lower()
            if normalized_reason == "max_output_tokens" or "max_output" in normalized_reason:
                finish_reason = "length"
            elif "content_filter" in normalized_reason or "safety" in normalized_reason:
                finish_reason = "content_filter"
            else:
                finish_reason = "incomplete"
        else:
            finish_reason = None
        details: dict[str, Any] = {}
        if status is not None:
            details["status"] = status
        if incomplete_details is not None:
            details["incomplete_details"] = deepcopy(incomplete_details)
        return finish_reason, details or None

    @classmethod
    def _normalize_responses_usage(cls, usage: Any) -> dict[str, Any]:
        normalized = deepcopy(usage) if isinstance(usage, dict) else {}
        input_details = normalized.get("input_tokens_details")
        prompt_tokens = cls._nonnegative_token_count(normalized.get("input_tokens"))
        completion_tokens = cls._nonnegative_token_count(normalized.get("output_tokens"))
        total_tokens = normalized.get("total_tokens")
        if not isinstance(total_tokens, int) or isinstance(total_tokens, bool) or total_tokens < 0:
            total_tokens = prompt_tokens + completion_tokens
        cached_tokens = input_details.get("cached_tokens") if isinstance(input_details, dict) else None
        normalized.update(
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": cls._nonnegative_token_count(total_tokens),
                "cached_tokens": cls._nonnegative_token_count(cached_tokens),
            }
        )
        return normalized

    @staticmethod
    def _nonnegative_token_count(value: Any) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

    @classmethod
    def _parse_arguments(cls, raw_arguments: Any) -> dict[str, Any]:
        if isinstance(raw_arguments, dict):
            return raw_arguments
        if isinstance(raw_arguments, str) and raw_arguments.strip():
            try:
                parsed = json.loads(raw_arguments)
            except (TypeError, ValueError):
                return {}
            return parsed if isinstance(parsed, dict) else {}
        return {}

    @classmethod
    def _raise_response_error_if_needed(cls, response: dict[str, Any]) -> None:
        if response.get("status") == "failed" or response.get("error") is not None:
            cls._raise_provider_error(response, status=None)

    @classmethod
    def _raise_http_error(cls, provider_error: Any, *, status: int | None) -> None:
        cls._raise_provider_error(provider_error, status=status)

    @classmethod
    def _raise_provider_error(cls, provider_error: Any, *, status: int | None) -> None:
        rejection = build_upstream_rejection(status=status, detail=provider_error)
        if rejection is not None:
            raise rejection
        error = cls._extract_provider_error(provider_error)
        code = error.get("code")
        if isinstance(code, str) and code.strip().lower() == cls._CONTEXT_LENGTH_ERROR_CODE:
            kwargs: dict[str, Any] = {"detail": provider_error}
            if status is not None:
                kwargs["status"] = status
            provider_message = error.get("message")
            raise LLMContextLengthException(
                provider_message=provider_message if isinstance(provider_message, str) else None,
                **kwargs,
            )
        if status is not None:
            raise LLMException(ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS, status=status, detail=provider_error)
        raise LLMException(ERR_LLM_API_RESPONSE_ERROR, detail=provider_error)

    @classmethod
    def _extract_provider_error(cls, provider_error: Any) -> dict[str, Any]:
        payload = provider_error
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except (TypeError, ValueError):
                return {}
        if not isinstance(payload, dict):
            return {}
        response = payload.get("response")
        if isinstance(response, dict) and isinstance(response.get("error"), dict):
            return response["error"]
        error = payload.get("error")
        if isinstance(error, dict):
            return error
        return payload if isinstance(payload.get("code"), str) else {}

    @staticmethod
    def _stringify(value: Any) -> str:
        if isinstance(value, str):
            return value
        try:
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        except (TypeError, ValueError):
            return str(value)
