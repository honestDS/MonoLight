import json
from collections.abc import AsyncGenerator
from typing import Any

from app.core.constants import (
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


class DeepSeekChatCompletionsTransformer(BaseTransformer):
    protocol = "deepseek"

    @classmethod
    def build_input_token_payload(
        cls,
        *,
        model_id: str | None,
        messages: list[InternalMessage],
        tools: list[dict[str, Any]] | None,
        channel_id: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "messages": cls.to_provider(messages, model_id=model_id, channel_id=channel_id),
        }
        if tools:
            payload["tools"] = tools
        return payload

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
        tool_choice: str,
        top_p: Any,
        reasoning_effort: Any,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "model": model_id,
            "messages": cls.to_provider(messages, model_id=model_id, channel_id=channel_id),
            "stream": stream,
        }
        if stream:
            payload["stream_options"] = {"include_usage": True}
        if temperature is not None:
            payload["temperature"] = temperature
        if max_tokens > 0:
            payload["max_tokens"] = max_tokens
        if top_p is not None:
            payload["top_p"] = top_p
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = tool_choice
        else:
            payload["tool_choice"] = "none"

        if reasoning_effort is not None:
            if isinstance(reasoning_effort, str):
                normalized_effort = reasoning_effort.strip()
                if "".join(normalized_effort.split()).lower() == "none":
                    payload["thinking"] = {"type": "disabled"}
                else:
                    payload["thinking"] = {"type": "enabled"}
                    payload["reasoning_effort"] = normalized_effort
            else:
                payload["thinking"] = {"type": "enabled"}
                payload["reasoning_effort"] = reasoning_effort
        return payload

    @staticmethod
    def _role_value(message: InternalMessage) -> str:
        role = message.role
        return role.value if isinstance(role, MessageRole) else str(role)

    @staticmethod
    def _part_value(part: Any, key: str, default: Any = None) -> Any:
        if isinstance(part, dict):
            return part.get(key, default)
        return getattr(part, key, default)

    @classmethod
    def _provider_content(cls, content: Any) -> Any:
        if not isinstance(content, list):
            return content

        provider_content: list[Any] = []
        for part in content:
            part_type = cls._part_value(part, "type")
            if isinstance(part, AudioPart) or part_type == "audio":
                raise LLMException(ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED)
            if isinstance(part, TextPart) or part_type == "text":
                provider_content.append({"type": "text", "text": cls._part_value(part, "text", "")})
            elif isinstance(part, ImagePart) or part_type == "image_url":
                image_url = cls._part_value(part, "image_url", {})
                if isinstance(image_url, dict):
                    image_url = {"url": image_url.get("url", "")}
                provider_content.append({"type": "image_url", "image_url": image_url})
            elif isinstance(part, FilePart) or part_type == "file":
                path = cls._part_value(part, "path", "")
                provider_content.append({"type": "text", "text": f"[Attached File: {path}]"})
            elif isinstance(part, dict):
                provider_content.append(dict(part))
            elif hasattr(part, "model_dump"):
                provider_content.append(part.model_dump(mode="json"))
            else:
                provider_content.append(part)
        return provider_content

    @classmethod
    def _assistant_reasoning(
        cls,
        message: InternalMessage,
        target_source: dict[str, Any] | None,
    ) -> str | None:
        metadata = message.provider_metadata
        raw_source = metadata.get("source") if isinstance(metadata, dict) else None
        source = (
            build_provider_source(
                channel_id=raw_source.get("channel_id"),
                model_id=raw_source.get("model_id"),
                protocol=raw_source.get("protocol"),
            )
            if isinstance(raw_source, dict)
            else None
        )
        if source is None or target_source is None:
            return None
        if source["channel_id"] != target_source["channel_id"] or source["model_id"] != target_source["model_id"]:
            return None

        protocol = source["protocol"]
        if protocol not in {"deepseek", "deepseek_responses", "openai", "openai_responses"}:
            return None

        if protocol in {"deepseek", "openai"}:
            if isinstance(message.reasoning_content, str):
                return message.reasoning_content
            metadata_message = metadata.get("message") if isinstance(metadata, dict) else None
            if isinstance(metadata_message, dict):
                reasoning_content = metadata_message.get("reasoning_content")
                if isinstance(reasoning_content, str):
                    return reasoning_content
                reasoning = metadata_message.get("reasoning")
                if isinstance(reasoning, str):
                    return reasoning
            return None

        output = metadata.get("output") if isinstance(metadata, dict) else None
        if isinstance(output, list):
            reasoning_parts: list[str] = []
            for item in output:
                if not isinstance(item, dict) or item.get("type") != "reasoning":
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "reasoning_text" and isinstance(part.get("text"), str):
                        reasoning_parts.append(part["text"])
            return "".join(reasoning_parts) if reasoning_parts else None

        if protocol == "deepseek_responses" and "output" not in metadata and isinstance(message.reasoning_content, str):
            return message.reasoning_content
        return None

    @classmethod
    def to_provider(cls, internal_messages: list[InternalMessage], **kwargs) -> list[dict[str, Any]]:
        target_source = build_provider_source(
            channel_id=kwargs.get("channel_id"),
            model_id=kwargs.get("model_id"),
            protocol=cls.protocol,
        )
        provider_messages: list[dict[str, Any]] = []

        for message in internal_messages:
            role = cls._role_value(message)
            content = cls._provider_content(message.content)
            item: dict[str, Any] = {"role": role, "content": content}
            tool_calls = message.tool_calls or []

            if role == MessageRole.ASSISTANT.value:
                reasoning_content = cls._assistant_reasoning(message, target_source)
                if not content and not tool_calls and not reasoning_content:
                    continue
                item["reasoning_content"] = reasoning_content if isinstance(reasoning_content, str) else ""

            if tool_calls:
                item["tool_calls"] = [
                    {
                        "id": tool_call.id,
                        "type": "function",
                        "function": {
                            "name": tool_call.name,
                            "arguments": json.dumps(tool_call.arguments, ensure_ascii=False),
                        },
                    }
                    for tool_call in tool_calls
                ]
            if message.tool_call_id:
                item["tool_call_id"] = message.tool_call_id
            provider_messages.append(item)

        return provider_messages

    @classmethod
    def _decode_json_object(cls, value: Any) -> dict[str, Any] | None:
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
            except (TypeError, ValueError):
                return None
            return decoded if isinstance(decoded, dict) else None
        return None

    @classmethod
    def _error_objects(cls, provider_error: Any) -> list[dict[str, Any]]:
        root = cls._decode_json_object(provider_error)
        if root is None:
            return []

        errors: list[dict[str, Any]] = []
        pending = [root]
        seen: set[int] = set()
        while pending:
            current = pending.pop()
            if id(current) in seen:
                continue
            seen.add(id(current))
            errors.append(current)
            nested = current.get("error")
            nested_object = cls._decode_json_object(nested)
            if nested_object is not None:
                pending.append(nested_object)
        return errors

    @classmethod
    def raise_error(cls, provider_error: Any, *, status: int | None = None) -> None:
        rejection = build_upstream_rejection(status=status, detail=provider_error)
        if rejection is not None:
            raise rejection

        for error in cls._error_objects(provider_error):
            code = error.get("code")
            if isinstance(code, str) and code.strip().lower() == "context_length_exceeded":
                provider_message = error.get("message")
                kwargs: dict[str, Any] = {"detail": provider_error}
                if status is not None:
                    kwargs["status"] = status
                raise LLMContextLengthException(
                    provider_message=provider_message if isinstance(provider_message, str) else None,
                    **kwargs,
                )

        error_key = ERR_LLM_API_RESPONSE_ERROR_WITH_STATUS if status is not None else ERR_LLM_API_RESPONSE_ERROR
        kwargs = {"detail": provider_error}
        if status is not None:
            kwargs["status"] = status
        raise LLMException(error_key, **kwargs)

    @classmethod
    async def list_models(
        cls,
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
            raise_error=cls.raise_error,
        )

    async def generate(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        temperature: float | None = None,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        timeout: float = 60.0,
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:
        payload = self._request_payload(
            model_id=model_id,
            channel_id=kwargs.get("channel_id"),
            messages=messages,
            stream=False,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            top_p=kwargs.get("top_p"),
            reasoning_effort=kwargs.get("reasoning_effort"),
        )
        response = await http_transport.post_json(
            url=f"{base_url.rstrip('/')}/chat/completions",
            headers=build_model_request_headers(api_key, custom_headers),
            payload=payload,
            timeout=timeout,
            http_proxy=http_proxy,
            raise_error=self.raise_error,
        )
        if "error" in response and response["error"] is not None:
            self.raise_error(response)
        return response

    @staticmethod
    def _normalize_finish_reason(raw_reason: Any) -> tuple[str | None, dict[str, Any] | None]:
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
    def _normalize_usage(cls, usage: Any) -> dict[str, Any]:
        normalized = dict(usage) if isinstance(usage, dict) else {}
        prompt_details = normalized.get("prompt_tokens_details")
        cached_tokens = prompt_details.get("cached_tokens") if isinstance(prompt_details, dict) else None
        if not isinstance(cached_tokens, int) or isinstance(cached_tokens, bool) or cached_tokens < 0:
            cached_tokens = normalized.get("prompt_cache_hit_tokens")
        if not isinstance(cached_tokens, int) or isinstance(cached_tokens, bool) or cached_tokens < 0:
            cached_tokens = 0

        def token_count(value: Any) -> int:
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0

        normalized.update(
            {
                "prompt_tokens": token_count(normalized.get("prompt_tokens")),
                "completion_tokens": token_count(normalized.get("completion_tokens")),
                "total_tokens": token_count(normalized.get("total_tokens")),
                "cached_tokens": cached_tokens,
            }
        )
        return normalized

    @classmethod
    def _tool_call_metadata(cls, tool_call: dict[str, Any]) -> dict[str, Any]:
        metadata = {key: value for key, value in tool_call.items() if key not in {"id", "type", "function"}}
        function = tool_call.get("function")
        if isinstance(function, dict):
            function_metadata = {key: value for key, value in function.items() if key not in {"name", "arguments"}}
            if function_metadata:
                metadata["function"] = function_metadata
        result: dict[str, Any] = {"protocol": cls.protocol}
        if metadata:
            result["tool_call"] = metadata
        return result

    @classmethod
    def _message_metadata(cls, message: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in message.items() if key not in {"content", "refusal", "tool_calls"}}

    @classmethod
    def _first_choice_and_message(cls, provider_response: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        if isinstance(provider_response, dict) and "error" in provider_response and provider_response["error"] is not None:
            cls.raise_error(provider_response)
        choices = provider_response.get("choices") if isinstance(provider_response, dict) else None
        if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise LLMException(ERR_LLM_EMPTY_RESPONSE)
        return choice, message

    @classmethod
    def from_provider(cls, provider_response: Any) -> InternalMessage:
        choice, message = cls._first_choice_and_message(provider_response)
        tool_calls: list[InternalToolCall] = []
        raw_tool_calls = message.get("tool_calls")
        if isinstance(raw_tool_calls, list):
            for raw_tool_call in raw_tool_calls:
                if not isinstance(raw_tool_call, dict):
                    continue
                function = raw_tool_call.get("function")
                if not isinstance(function, dict):
                    continue
                call_id = raw_tool_call.get("id")
                name = function.get("name")
                if not isinstance(call_id, str) or not call_id or not isinstance(name, str) or not name:
                    continue
                raw_arguments = function.get("arguments")
                arguments: dict[str, Any] = {}
                if isinstance(raw_arguments, dict):
                    arguments = dict(raw_arguments)
                elif isinstance(raw_arguments, str) and raw_arguments.strip():
                    try:
                        parsed_arguments = json.loads(raw_arguments)
                    except (TypeError, ValueError):
                        parsed_arguments = None
                    if isinstance(parsed_arguments, dict):
                        arguments = parsed_arguments
                tool_calls.append(
                    InternalToolCall(
                        id=call_id,
                        name=name,
                        arguments=arguments,
                        provider_metadata=cls._tool_call_metadata(raw_tool_call),
                    )
                )

        refusal = message.get("refusal") if isinstance(message.get("refusal"), str) else None
        content = message.get("content")
        if not content and refusal:
            content = refusal
        reasoning_content = message.get("reasoning_content")
        if not isinstance(reasoning_content, str):
            reasoning_content = message.get("reasoning")
        if not isinstance(reasoning_content, str):
            reasoning_content = None

        return InternalMessage(
            role=MessageRole.ASSISTANT,
            content=content,
            reasoning_content=reasoning_content,
            refusal=refusal,
            provider_metadata={
                "protocol": cls.protocol,
                "choice": {key: value for key, value in choice.items() if key not in {"message", "finish_reason"}},
                "message": cls._message_metadata(message),
            },
            tool_calls=tool_calls or None,
        )

    @classmethod
    def to_internal_response(cls, provider_response: Any, default_model: str) -> InternalResponse:
        choice, message = cls._first_choice_and_message(provider_response)
        internal_message = cls.from_provider(provider_response)
        finish_reason, finish_details = cls._normalize_finish_reason(choice.get("finish_reason"))
        if internal_message.refusal and finish_reason in {None, "stop"}:
            finish_reason = "refusal"
        response = provider_response if isinstance(provider_response, dict) else {}
        model = response.get("model")
        message_metadata = cls._message_metadata(message)
        choice_metadata = {key: value for key, value in choice.items() if key not in {"message", "finish_reason"}}
        return InternalResponse(
            message=internal_message,
            model=str(model) if model is not None else default_model,
            usage=cls._normalize_usage(response.get("usage")),
            finish_reason=finish_reason,
            finish_details=finish_details,
            provider_metadata={
                "protocol": cls.protocol,
                "response": {key: value for key, value in response.items() if key not in {"choices", "model", "usage"}},
                "choice": choice_metadata,
                "message": message_metadata,
            },
        )

    @classmethod
    def _stream_is_terminal(cls, event: Any) -> bool:
        if not isinstance(event, dict):
            return False
        choices = event.get("choices")
        return isinstance(choices, list) and any(isinstance(choice, dict) and choice.get("finish_reason") is not None for choice in choices)

    @classmethod
    def _stream_tool_call_delta(cls, tool_call: dict[str, Any]) -> dict[str, Any]:
        normalized: dict[str, Any] = {"type": "function", "provider_metadata": {"protocol": cls.protocol}}
        if "index" in tool_call:
            normalized["index"] = tool_call["index"]
        call_id = tool_call.get("id")
        if isinstance(call_id, str):
            normalized["id"] = call_id
        function = tool_call.get("function")
        if isinstance(function, dict):
            normalized_function: dict[str, Any] = {}
            name = function.get("name")
            arguments = function.get("arguments")
            if isinstance(name, str):
                normalized_function["name"] = name
            if isinstance(arguments, str):
                normalized_function["arguments"] = arguments
            elif isinstance(arguments, dict):
                normalized_function["arguments"] = json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
            if normalized_function:
                normalized["function"] = normalized_function
        return normalized

    async def generate_stream(
        self,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        temperature: float | None = None,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        timeout: float = 60.0,
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs,
    ) -> AsyncGenerator[dict[str, Any]]:
        payload = self._request_payload(
            model_id=model_id,
            channel_id=kwargs.get("channel_id"),
            messages=messages,
            stream=True,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            top_p=kwargs.get("top_p"),
            reasoning_effort=kwargs.get("reasoning_effort"),
        )
        response_metadata: dict[str, Any] = {}
        choice_metadata: dict[str, Any] = {}
        message_metadata: dict[str, Any] = {}
        reasoning_chunks: list[str] = []
        reasoning_seen = False

        def normalize_event(event: Any) -> tuple[dict[str, Any] | None, bool]:
            nonlocal reasoning_seen
            if not isinstance(event, dict):
                return None, False
            if "error" in event and event["error"] is not None:
                self.raise_error(event)

            choices = event.get("choices")
            normalized_choices: list[dict[str, Any]] = []
            has_output = False
            first_raw_choice: dict[str, Any] | None = None
            for raw_choice in choices if isinstance(choices, list) else []:
                if not isinstance(raw_choice, dict):
                    continue
                if first_raw_choice is None:
                    first_raw_choice = raw_choice
                    choice_metadata.update({key: value for key, value in raw_choice.items() if key not in {"delta", "finish_reason"}})
                raw_delta = raw_choice.get("delta")
                raw_delta = raw_delta if isinstance(raw_delta, dict) else {}
                delta: dict[str, Any] = {}
                if isinstance(raw_delta.get("role"), str):
                    delta["role"] = raw_delta["role"]
                content = raw_delta.get("content")
                if isinstance(content, str):
                    delta["content"] = content
                    has_output = has_output or bool(content)
                refusal = raw_delta.get("refusal")
                if isinstance(refusal, str):
                    delta["refusal"] = refusal
                    has_output = has_output or bool(refusal)

                reasoning_content = raw_delta.get("reasoning_content")
                if not isinstance(reasoning_content, str):
                    reasoning_content = raw_delta.get("reasoning")
                if isinstance(reasoning_content, str):
                    delta["reasoning_content"] = reasoning_content
                    reasoning_chunks.append(reasoning_content)
                    reasoning_seen = True
                    has_output = has_output or bool(reasoning_content)

                raw_tool_calls = raw_delta.get("tool_calls")
                if isinstance(raw_tool_calls, list):
                    tool_call_deltas = [self._stream_tool_call_delta(tool_call) for tool_call in raw_tool_calls if isinstance(tool_call, dict)]
                    if tool_call_deltas:
                        delta["tool_calls"] = tool_call_deltas
                        for tool_call in tool_call_deltas:
                            function = tool_call.get("function")
                            has_output = has_output or bool(tool_call.get("id")) or bool(isinstance(function, dict) and (function.get("name") or function.get("arguments")))

                message_metadata.update({key: value for key, value in raw_delta.items() if key not in {"content", "refusal", "tool_calls", "reasoning", "reasoning_content"}})
                if reasoning_seen:
                    message_metadata["reasoning_content"] = "".join(reasoning_chunks)

                normalized_choice: dict[str, Any] = {"delta": delta}
                if "index" in raw_choice:
                    normalized_choice["index"] = raw_choice["index"]
                if "finish_reason" in raw_choice:
                    normalized_choice["finish_reason"] = raw_choice["finish_reason"]
                if "logprobs" in raw_choice:
                    normalized_choice["logprobs"] = raw_choice["logprobs"]
                normalized_choices.append(normalized_choice)

            response_metadata.update({key: value for key, value in event.items() if key not in {"choices", "model", "usage", "finish_details", "provider_metadata", "message_provider_metadata"}})
            if "reasoning_content" in message_metadata:
                message_metadata["reasoning_content"] = "".join(reasoning_chunks)

            normalized: dict[str, Any] = {
                "choices": normalized_choices,
                "provider_metadata": {
                    "protocol": self.protocol,
                    "response": dict(response_metadata),
                    "choice": dict(choice_metadata),
                    "message": dict(message_metadata),
                },
            }
            if isinstance(event.get("model"), str):
                normalized["model"] = event["model"]
            if "usage" in event:
                normalized["usage"] = self._normalize_usage(event.get("usage"))

            finish_details = event.get("finish_details")
            normalized_finish_details = dict(finish_details) if isinstance(finish_details, dict) else {}
            if first_raw_choice is not None and first_raw_choice.get("finish_reason") is not None:
                normalized_finish_details["raw_finish_reason"] = first_raw_choice["finish_reason"]
            if normalized_finish_details:
                normalized["finish_details"] = normalized_finish_details

            message_provider_metadata = {
                "protocol": self.protocol,
                "choice": dict(choice_metadata),
                "message": dict(message_metadata),
            }
            if first_raw_choice is not None or self._stream_is_terminal(event):
                normalized["message_provider_metadata"] = message_provider_metadata
            return normalized, has_output

        async for chunk in http_transport.stream_sse_json(
            url=f"{base_url.rstrip('/')}/chat/completions",
            headers=build_model_request_headers(api_key, custom_headers),
            payload=payload,
            timeout=timeout,
            http_proxy=http_proxy,
            normalize_event=normalize_event,
            raise_error=self.raise_error,
        ):
            yield chunk
