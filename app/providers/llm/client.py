import json
import uuid
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import (
    Any,
)

from app.core.constants import (
    ERR_CHANNEL_MODEL_LIST_UNSUPPORTED,
    ERR_LLM_STREAM_TOOL_CALL_AMBIGUOUS,
    ERR_LLM_UNSUPPORTED_PROTOCOL,
)
from app.core.exceptions import LLMException
from app.core.log import get_logger
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import (
    InternalMessage,
    InternalResponse,
    InternalToolCall,
    MessageRole,
)
from app.transformers.openai import OpenAIChatCompletionsTransformer, OpenAIResponsesTransformer

logger = get_logger(__name__)

_OPENAI_STREAM_APPEND_STRING_METADATA_FIELDS = frozenset({"reasoning_content"})


def _merge_metadata(
    current: dict[str, Any] | None,
    incoming: Any,
    append_string_fields: frozenset[str] = frozenset(),
) -> dict[str, Any] | None:
    if not isinstance(incoming, dict):
        return current
    merged = dict(current or {})
    for key, value in incoming.items():
        existing = merged.get(key)
        if isinstance(existing, dict) and isinstance(value, dict):
            merged[key] = _merge_metadata(existing, value, append_string_fields) or {}
        elif isinstance(existing, list) and isinstance(value, list):
            merged[key] = [*existing, *value]
        elif isinstance(existing, str) and isinstance(value, str) and key in append_string_fields:
            merged[key] = existing + value
        else:
            merged[key] = value
    return merged


def _openai_stream_metadata(
    chunk: dict[str, Any],
    choice: dict[str, Any] | None,
    delta: dict[str, Any] | None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    response_metadata = {key: value for key, value in chunk.items() if key not in {"choices", "model", "usage", "finish_details", "provider_metadata", "message_provider_metadata"}}
    choice_metadata = {key: value for key, value in (choice or {}).items() if key != "delta"}
    choice_metadata.pop("finish_reason", None)
    message_metadata = {key: value for key, value in (delta or {}).items() if key not in {"content", "refusal", "tool_calls"}}
    provider_metadata = {
        "protocol": OpenAIChatCompletionsTransformer._PROTOCOL_METADATA,
        "response": response_metadata,
        "choice": choice_metadata,
        "message": message_metadata,
    }
    return provider_metadata, {
        "protocol": OpenAIChatCompletionsTransformer._PROTOCOL_METADATA,
        "choice": choice_metadata,
        "message": message_metadata,
    }


def estimate_request_context_tokens(
    messages: list[InternalMessage],
    tools: list[dict[str, Any]] | None,
) -> int:
    message_payload = [
        message.model_dump(
            mode="json",
            exclude={"id", "attachments", "created_at", "environment_prompt"},
            exclude_none=True,
        )
        for message in messages
    ]
    serialized_context = json.dumps(
        {
            "messages": message_payload,
            "tools": tools or [],
        },
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )
    return estimate_tokens(serialized_context)


def _log_request_context(
    *,
    model_id: str,
    protocol: str,
    messages: list[InternalMessage],
    tools: list[dict[str, Any]] | None,
    max_tokens: int,
    streaming: bool,
    request_context_tokens: int | None = None,
) -> None:
    role_counts: dict[str, int] = {}
    for message in messages:
        role = message.role.value if hasattr(message.role, "value") else str(message.role)
        role_counts[role] = role_counts.get(role, 0) + 1
    message_count = len(messages)
    tool_count = len(tools or [])
    estimated_context_tokens = request_context_tokens if isinstance(request_context_tokens, int) and not isinstance(request_context_tokens, bool) and request_context_tokens >= 0 else estimate_request_context_tokens(messages, tools)
    logger.bind(
        model_id=model_id,
        protocol=protocol,
        streaming=streaming,
        message_count=message_count,
        role_counts=role_counts,
        tool_count=tool_count,
        estimated_context_tokens=estimated_context_tokens,
        max_output_tokens=max_tokens,
    ).debug(
        "LLM request context: model={model_id}, protocol={protocol}, streaming={streaming}, messages={message_count}, roles={role_counts}, tools={tool_count}, estimated_tokens={estimated_context_tokens}, max_output_tokens={max_tokens}",
        model_id=model_id,
        protocol=protocol,
        streaming=streaming,
        message_count=message_count,
        role_counts=role_counts,
        tool_count=tool_count,
        estimated_context_tokens=estimated_context_tokens,
        max_tokens=max_tokens,
    )


@dataclass(slots=True)
class _StreamToolCallState:
    order: int
    index: int | None
    provider_id: str | None = None
    name: str = ""
    argument_chunks: list[str] = field(default_factory=list)
    provider_metadata: dict[str, Any] | None = None

    @property
    def argument_text(self) -> str:
        return "".join(self.argument_chunks)

    def append_delta(self, *, name: str, arguments: str, provider_metadata: dict[str, Any] | None = None) -> None:
        if name and not self.name:
            self.name = name
        if arguments:
            self.argument_chunks.append(arguments)
        self.provider_metadata = _merge_metadata(self.provider_metadata, provider_metadata)


class _StreamToolCallAssembler:
    """Assemble provider tool-call deltas without content-based deduplication."""

    def __init__(self, protocol: str = "openai") -> None:
        self._protocol = protocol
        self._states: list[_StreamToolCallState] = []
        self._states_by_index: dict[int, _StreamToolCallState] = {}
        self._states_by_provider_id: dict[str, list[_StreamToolCallState]] = {}

    def add(self, tool_calls: list[dict[str, Any]]) -> None:
        if not tool_calls:
            return
        if any(not isinstance(tool_call, dict) for tool_call in tool_calls):
            raise self._ambiguous_error()
        if any(self._has_no_identity(tool_call) for tool_call in tool_calls) and len(tool_calls) > 1:
            raise self._ambiguous_error()

        for tool_call in tool_calls:
            index = self._get_index(tool_call)
            provider_id = self._get_provider_id(tool_call)
            name, arguments = self._get_function_delta(tool_call)
            provider_metadata = self._get_provider_metadata(tool_call)
            state, is_snapshot_replay = self._find_state(
                index=index,
                provider_id=provider_id,
                name=name,
                arguments=arguments,
            )
            if state is None:
                state = self._create_state(index=index, provider_id=provider_id)
            elif provider_id and state.provider_id is None:
                self._bind_provider_id(state, provider_id)
            state.provider_metadata = _merge_metadata(state.provider_metadata, provider_metadata)
            if not is_snapshot_replay:
                state.append_delta(name=name, arguments=arguments, provider_metadata=None)

    def build(self) -> list[InternalToolCall]:
        tool_calls: list[InternalToolCall] = []
        for state in sorted(self._states, key=self._sort_key):
            if not state.name:
                continue
            arguments: dict[str, Any] = {}
            parsed_arguments = self._parse_json_object(state.argument_text)
            if parsed_arguments is not None:
                arguments = parsed_arguments
            tool_calls.append(
                InternalToolCall(
                    id=state.provider_id or f"call_{state.index if state.index is not None else state.order}",
                    name=state.name,
                    arguments=arguments,
                    provider_metadata=state.provider_metadata,
                )
            )
        return tool_calls

    def _find_state(
        self,
        *,
        index: int | None,
        provider_id: str | None,
        name: str,
        arguments: str,
    ) -> tuple[_StreamToolCallState | None, bool]:
        if index is not None:
            state = self._states_by_index.get(index)
            if state is not None:
                return state, False
            if provider_id:
                replay_states = [candidate for candidate in self._states_by_provider_id.get(provider_id, []) if self._is_snapshot_replay(candidate, index=index, name=name, arguments=arguments)]
                if len(replay_states) > 1:
                    raise self._ambiguous_error()
                if replay_states:
                    self._states_by_index[index] = replay_states[0]
                    return replay_states[0], True
            return None, False

        if provider_id:
            states = self._states_by_provider_id.get(provider_id, [])
            if len(states) > 1:
                raise self._ambiguous_error()
            if states:
                return states[0], False
            if len(self._states) == 1 and self._states[0].provider_id is None:
                return self._states[0], False
            return None, False

        if len(self._states) <= 1:
            return (self._states[0], False) if self._states else (None, False)
        raise self._ambiguous_error()

    def _create_state(self, *, index: int | None, provider_id: str | None) -> _StreamToolCallState:
        state = _StreamToolCallState(
            order=len(self._states),
            index=index,
            provider_id=provider_id,
        )
        self._states.append(state)
        if index is not None:
            self._states_by_index[index] = state
        if provider_id:
            self._states_by_provider_id.setdefault(provider_id, []).append(state)
        return state

    def _bind_provider_id(self, state: _StreamToolCallState, provider_id: str) -> None:
        state.provider_id = provider_id
        self._states_by_provider_id.setdefault(provider_id, []).append(state)

    def _is_snapshot_replay(
        self,
        state: _StreamToolCallState,
        *,
        index: int,
        name: str,
        arguments: str,
    ) -> bool:
        if state.index == index or not state.name or (name and name != state.name):
            return False
        existing_arguments = self._parse_json_object(state.argument_text)
        incoming_arguments = self._parse_json_object(arguments)
        return existing_arguments is not None and incoming_arguments == existing_arguments

    @staticmethod
    def _get_index(tool_call: dict[str, Any]) -> int | None:
        raw_index = tool_call.get("index")
        if raw_index is None:
            return None
        try:
            return int(raw_index)
        except (TypeError, ValueError) as exc:
            raise _StreamToolCallAssembler._ambiguous_error() from exc

    @staticmethod
    def _get_provider_id(tool_call: dict[str, Any]) -> str | None:
        provider_id = tool_call.get("id")
        return str(provider_id) if provider_id else None

    @staticmethod
    def _get_function_delta(tool_call: dict[str, Any]) -> tuple[str, str]:
        function = tool_call.get("function") or {}
        if not isinstance(function, dict):
            return "", ""
        name = function.get("name")
        raw_arguments = function.get("arguments")
        if raw_arguments is None:
            arguments = ""
        elif isinstance(raw_arguments, str):
            arguments = raw_arguments
        else:
            arguments = json.dumps(raw_arguments, ensure_ascii=False, separators=(",", ":"))
        return (str(name) if name else ""), arguments

    def _get_provider_metadata(self, tool_call: dict[str, Any]) -> dict[str, Any] | None:
        explicit_metadata = tool_call.get("provider_metadata")
        metadata = dict(explicit_metadata) if isinstance(explicit_metadata, dict) else None
        if self._protocol == "openai_responses":
            return metadata

        tool_call_metadata = {key: value for key, value in tool_call.items() if key not in {"index", "id", "function", "provider_metadata"}}
        function = tool_call.get("function")
        if isinstance(function, dict):
            function_metadata = {key: value for key, value in function.items() if key not in {"name", "arguments"}}
            if function_metadata:
                tool_call_metadata["function"] = function_metadata
        if tool_call_metadata:
            protocol = OpenAIChatCompletionsTransformer._PROTOCOL_METADATA if self._protocol == "openai" else self._protocol
            metadata = _merge_metadata(
                metadata,
                {"protocol": protocol, "tool_call": tool_call_metadata},
            )
        return metadata

    @staticmethod
    def _parse_json_object(arguments: str) -> dict[str, Any] | None:
        if not arguments.strip():
            return None
        try:
            parsed_arguments = json.loads(arguments)
        except (TypeError, ValueError):
            return None
        return parsed_arguments if isinstance(parsed_arguments, dict) else None

    @staticmethod
    def _has_no_identity(tool_call: dict[str, Any]) -> bool:
        return tool_call.get("index") is None and not tool_call.get("id")

    @staticmethod
    def _sort_key(state: _StreamToolCallState) -> tuple[bool, int, int]:
        return state.index is None, state.index if state.index is not None else state.order, state.order

    @staticmethod
    def _ambiguous_error() -> LLMException:
        return LLMException(message=ERR_LLM_STREAM_TOOL_CALL_AMBIGUOUS)


class LLMClient:
    _transformers = {
        "openai": OpenAIChatCompletionsTransformer(),
        "openai_responses": OpenAIResponsesTransformer(),
    }

    @staticmethod
    def normalize_tool_calls(tool_calls: list[InternalToolCall] | None) -> list[InternalToolCall] | None:
        if not tool_calls:
            return None

        return [
            tool_call.model_copy(
                update={"id": f"call_{uuid.uuid4().hex}"},
                deep=True,
            )
            for tool_call in tool_calls
        ]

    @classmethod
    async def list_models(
        cls,
        api_key: str,
        base_url: str,
        protocol: str = "openai",
        timeout: float = 30.0,
        http_proxy: str | None = None,
        **kwargs,
    ) -> list[dict[str, Any]]:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            raise LLMException(ERR_CHANNEL_MODEL_LIST_UNSUPPORTED, protocol=protocol)

        return await transformer.list_models(
            api_key=api_key,
            base_url=base_url,
            timeout=timeout,
            http_proxy=http_proxy,
            **kwargs,
        )

    @classmethod
    async def generate_stream(
        cls,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        protocol: str = "openai",
        timeout: float = 60.0,
        request_context_tokens: int | None = None,
        http_proxy: str | None = None,
        **kwargs,
    ) -> AsyncGenerator[dict[str, Any]]:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            raise LLMException(message=ERR_LLM_UNSUPPORTED_PROTOCOL, protocol=protocol)

        _log_request_context(
            model_id=model_id,
            protocol=protocol,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            streaming=True,
            request_context_tokens=request_context_tokens,
        )
        async for chunk in transformer.generate_stream(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            timeout=timeout,
            http_proxy=http_proxy,
            **kwargs,
        ):
            yield chunk

    @classmethod
    async def generate_with_stream_callback(
        cls,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        on_content: Callable[[str], Awaitable[None]],
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        protocol: str = "openai",
        timeout: float = 60.0,
        request_context_tokens: int | None = None,
        http_proxy: str | None = None,
        **kwargs,
    ) -> InternalResponse:
        content_chunks: list[str] = []
        refusal_chunks: list[str] = []
        normalized_protocol = protocol.lower()
        tool_call_assembler = _StreamToolCallAssembler(normalized_protocol)
        model = model_id
        usage: dict[str, Any] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        finish_reason: str | None = None
        finish_details: dict[str, Any] | None = None
        provider_metadata: dict[str, Any] | None = None
        message_provider_metadata: dict[str, Any] | None = None

        async for chunk in cls.generate_stream(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            protocol=protocol,
            timeout=timeout,
            request_context_tokens=request_context_tokens,
            http_proxy=http_proxy,
            **kwargs,
        ):
            if isinstance(chunk.get("model"), str):
                model = chunk["model"]
            if isinstance(chunk.get("usage"), dict):
                usage = chunk["usage"]
            choices = chunk.get("choices") or []
            choice = choices[0] if choices and isinstance(choices[0], dict) else None
            delta = choice.get("delta") if isinstance(choice, dict) else None
            delta = delta if isinstance(delta, dict) else {}

            if normalized_protocol == "openai":
                chunk_provider_metadata, chunk_message_metadata = _openai_stream_metadata(chunk, choice, delta)
                provider_metadata = _merge_metadata(
                    provider_metadata,
                    chunk_provider_metadata,
                    _OPENAI_STREAM_APPEND_STRING_METADATA_FIELDS,
                )
                message_provider_metadata = _merge_metadata(
                    message_provider_metadata,
                    chunk_message_metadata,
                    _OPENAI_STREAM_APPEND_STRING_METADATA_FIELDS,
                )
            provider_metadata = _merge_metadata(provider_metadata, chunk.get("provider_metadata"))
            message_provider_metadata = _merge_metadata(message_provider_metadata, chunk.get("message_provider_metadata"))

            if isinstance(choice, dict) and choice.get("finish_reason") is not None:
                finish_reason, raw_finish_details = OpenAIChatCompletionsTransformer._normalize_finish_reason(choice.get("finish_reason"))
                finish_details = _merge_metadata(finish_details, raw_finish_details)
            finish_details = _merge_metadata(finish_details, chunk.get("finish_details"))

            if not choice:
                continue
            content = delta.get("content")
            refusal = delta.get("refusal")
            if isinstance(content, str) and content:
                content_chunks.append(content)
                await on_content(content)
            if isinstance(refusal, str) and refusal:
                refusal_chunks.append(refusal)
                if not content:
                    await on_content(refusal)
            tool_call_assembler.add(delta.get("tool_calls") or [])

        tool_calls = tool_call_assembler.build()
        content = "".join(content_chunks)
        refusal = "".join(refusal_chunks) or None
        if refusal and finish_reason in {None, "stop"}:
            finish_reason = "refusal"

        return InternalResponse(
            message=InternalMessage(
                role=MessageRole.ASSISTANT,
                content=content or refusal,
                refusal=refusal,
                provider_metadata=message_provider_metadata,
                tool_calls=cls.normalize_tool_calls(tool_calls),
            ),
            model=model,
            usage=usage,
            finish_reason=finish_reason,
            finish_details=finish_details,
            provider_metadata=provider_metadata,
        )

    @classmethod
    async def generate(
        cls,
        api_key: str,
        base_url: str,
        model_id: str,
        messages: list[InternalMessage],
        temperature: float = 0.7,
        max_tokens: int = 0,
        tools: list[dict[str, Any]] | None = None,
        tool_choice: str = "auto",
        protocol: str = "openai",
        timeout: float = 60.0,
        request_context_tokens: int | None = None,
        http_proxy: str | None = None,
        **kwargs,
    ) -> InternalResponse:
        transformer = cls._transformers.get(protocol.lower())
        if not transformer:
            raise LLMException(message=ERR_LLM_UNSUPPORTED_PROTOCOL, protocol=protocol)

        _log_request_context(
            model_id=model_id,
            protocol=protocol,
            messages=messages,
            tools=tools,
            max_tokens=max_tokens,
            streaming=False,
            request_context_tokens=request_context_tokens,
        )
        raw_response = await transformer.generate(
            api_key=api_key,
            base_url=base_url,
            model_id=model_id,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            tools=tools,
            tool_choice=tool_choice,
            timeout=timeout,
            http_proxy=http_proxy,
            **kwargs,
        )

        internal_response = transformer.to_internal_response(raw_response, default_model=model_id)
        normalized_message = internal_response.message.model_copy(
            update={"tool_calls": cls.normalize_tool_calls(internal_response.message.tool_calls)},
            deep=True,
        )
        return internal_response.model_copy(update={"message": normalized_message}, deep=True)
