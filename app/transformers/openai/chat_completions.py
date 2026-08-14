import json
from collections.abc import AsyncGenerator
from typing import (
    Any,
)

from app.core.constants import (
    ERR_LLM_EMPTY_RESPONSE,
)
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.model_request_headers import build_model_request_headers
from app.models.message import (
    FilePart,
    ImagePart,
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    MessageRole,
    TextPart,
)

from .base import BaseOpenAITransformer

logger = get_logger(__name__)


class OpenAIChatCompletionsTransformer(BaseOpenAITransformer):
    _PROTOCOL_METADATA = "openai_chat_completions"

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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs,
    ) -> dict[str, Any]:  # 返回原始响应字典，由 Dispatcher 或 BaseTransformer 处理最终封装

        headers = build_model_request_headers(api_key, custom_headers)
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
        parsed = await self._post_json(
            url=url,
            headers=headers,
            payload=payload,
            timeout=timeout,
            http_proxy=http_proxy,
            model_id=model_id,
            base_url=base_url,
        )
        parsed["usage"] = self._normalize_usage(parsed.get("usage"))
        return parsed

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
        http_proxy: str | None = None,
        custom_headers: dict[str, str] | None = None,
        **kwargs,
    ) -> AsyncGenerator[dict[str, Any]]:
        headers = build_model_request_headers(api_key, custom_headers)
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
        async for parsed in self._stream_sse_json(
            url=url,
            headers=headers,
            payload=payload,
            timeout=timeout,
            http_proxy=http_proxy,
            model_id=model_id,
            base_url=base_url,
            normalize_event=self._normalize_stream_event,
        ):
            yield parsed

    @classmethod
    def _normalize_stream_event(cls, event: Any) -> tuple[dict[str, Any] | None, bool]:
        if not isinstance(event, dict):
            return None, False

        parsed = dict(event)
        if "usage" in parsed:
            parsed["usage"] = cls._normalize_usage(parsed.get("usage"))
        return parsed, cls._stream_chunk_has_payload(parsed)

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
                # 兼容严格要求 assistant content 非空的提供商，仅修改发送给上游的副本。
                if msg.role.value == "assistant" and (
                    msg.content is None
                    or (isinstance(msg.content, str) and not msg.content.strip())
                    or (isinstance(msg.content, list) and not msg.content)
                ):
                    item["content"] = "[tool_call]"
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
