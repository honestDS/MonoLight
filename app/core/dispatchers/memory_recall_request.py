import uuid
from typing import Any

from app.core.channel_router import select_channel
from app.core.constants import CONTEXT_WINDOW_TOKENS_PER_K
from app.core.context import ContextManager
from app.core.crud.session import session_crud
from app.core.dispatchers.memory_recall_types import MemoryRecallContext
from app.core.prompts import LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT
from app.core.tools.longterm_memory import (
    MANAGE_LONGTERM_MEMORY_TOOL_NAME,
    MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA,
    validate_longterm_memory_arguments,
)
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.context_summary_checkpoint import (
    apply_context_summary_checkpoint,
)
from app.core.utils.dispatcher.helpers import (
    get_multimodal_from_entry,
    reassemble_multimodal_messages,
    resolve_chat_params,
)
from app.core.utils.dispatcher.markdown_instruction import (
    materialize_latest_user_environment_prompt,
)
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.core.utils.request_token_baseline import (
    build_request_token_baseline,
    extract_provider_token_metrics,
    extract_session_total_output_tokens,
)
from app.models.channel import resolve_model_protocol
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient, estimate_request_context_tokens


async def select_initial_channel(context: MemoryRecallContext) -> bool:
    channel_ready = context.chat_channel_obj is not None and isinstance(context.model_entry, dict) and context.channel_rule is not None
    if channel_ready:
        if not context.chat_params:
            context.chat_params = resolve_chat_params(
                context.model_entry or {},
                context.chat_channel,
            )
        return True
    selection = await select_channel(
        context.db,
        context.chat_channel,
        "CHAT",
        call_context=f"chat_dispatch_{context.dispatcher_mode}_memory_recall",
        cursor_key=context.chat_cursor_key,
    )
    if not selection:
        return False
    context.chat_channel_obj, context.model_entry, context.channel_rule = selection
    context.chat_params = resolve_chat_params(context.model_entry, context.chat_channel)
    return True


async def fallback_channel(
    context: MemoryRecallContext,
    excluded_priorities: set[int],
) -> bool:
    selection = await select_channel(
        context.db,
        context.chat_channel,
        "CHAT",
        call_context=f"chat_dispatch_{context.dispatcher_mode}_memory_recall_retry",
        excluded_priorities=excluded_priorities,
        cursor_key=context.chat_cursor_key,
    )
    if not selection:
        return False
    context.chat_channel_obj, context.model_entry, context.channel_rule = selection
    context.chat_params = resolve_chat_params(context.model_entry, context.chat_channel)
    reassemble_multimodal_messages(
        context.messages,
        *get_multimodal_from_entry(context.model_entry),
    )
    return True


async def prepare_request_messages(
    context: MemoryRecallContext,
    messages: list[InternalMessage],
    *,
    is_main_context: bool,
) -> tuple[list[InternalMessage], dict[str, Any], str]:
    if context.upper_message_id is not None:
        await context.db.commit()
        messages = await apply_context_summary_checkpoint(
            context.db,
            session_id=context.session_id,
            uid=context.uid,
            profile=context.profile,
            cfg=context.cfg,
            messages=messages,
            trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
            fixed_upper_message_id=context.upper_message_id,
            context_window_k=context.chat_params["context_window_k"],
            max_tokens=context.chat_params["max_tokens"],
            tools=[MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA],
            work_validity_checker=context.context_summary_checker,
            lifecycle_event_callback=context.context_summary_callback,
            model_id=context.model_entry["model_id"],
            protocol=resolve_model_protocol(context.model_entry),
            previous_llm_request_metadata=context.latest_llm_request_metadata,
        )
        if is_main_context:
            context.messages = messages

    request_messages = await materialize_latest_user_environment_prompt(
        context.db,
        context.session_id,
        messages,
        context.chat_params["max_tokens"],
    )
    request_messages = ContextManager.trim_messages_for_model_request(
        messages=request_messages,
        uid=context.uid,
        session_id=context.session_id,
        context_window_k=context.chat_params["context_window_k"],
        max_tokens=context.chat_params["max_tokens"],
        tools=[MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA],
    )
    session = await session_crud.get_by_session_id(context.db, context.session_id)
    if session is not None and hasattr(context.db, "refresh"):
        await context.db.refresh(session)
    summary_revision = getattr(session, "context_summary_revision", 0) if session is not None else 0
    content_revision = getattr(session, "context_content_revision", 0) if session is not None else 0
    persisted_total = extract_session_total_output_tokens(
        getattr(session, "llm_request_metadata", None),
    )
    if context.session_total_output_tokens is None:
        context.session_total_output_tokens = persisted_total
    else:
        context.session_total_output_tokens = max(
            context.session_total_output_tokens,
            persisted_total,
        )

    model_id = context.model_entry["model_id"]
    protocol = resolve_model_protocol(context.model_entry)
    input_tokens = estimate_request_context_tokens(
        request_messages,
        [MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA],
    )
    response_id = str(uuid.uuid4())
    metadata = {
        "type": "llm_request_metadata",
        "turn": 0,
        "response_id": response_id,
        "input_tokens": input_tokens,
        "input_tokens_source": "estimated",
        "total_output_tokens": context.session_total_output_tokens,
        "context_window_tokens": max(
            1,
            int(context.chat_params["context_window_k"]) * CONTEXT_WINDOW_TOKENS_PER_K,
        ),
        "max_output_tokens": max(0, int(context.chat_params["max_tokens"])),
        **build_request_token_baseline(
            request_messages,
            [MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA],
            model_id=model_id,
            protocol=protocol,
            context_summary_revision=summary_revision,
            context_content_revision=content_revision,
        ),
    }
    context.latest_llm_request_metadata = metadata
    if context.stream_event_callback is not None:
        await context.stream_event_callback(dict(metadata))
    return request_messages, metadata, response_id


async def generate(
    context: MemoryRecallContext,
    request_messages: list[InternalMessage],
    metadata: dict[str, Any],
) -> Any:
    await context.db.commit()
    channel = context.chat_channel_obj
    model_entry = context.model_entry or {}
    generation_kwargs = {
        "api_key": channel.get_decrypted_api_key(),
        "base_url": channel.base_url,
        "model_id": model_entry["model_id"],
        "messages": request_messages,
        "temperature": context.chat_params["temperature"],
        "top_p": context.chat_params["top_p"],
        "max_tokens": context.chat_params["max_tokens"],
        "tools": [MANAGE_LONGTERM_MEMORY_TOOL_SCHEMA],
        "protocol": resolve_model_protocol(model_entry),
        "timeout": context.chat_params["chat_timeout"],
        "http_proxy": get_channel_http_proxy(channel),
        "custom_headers": get_model_custom_headers(model_entry),
        "request_context_tokens": metadata["input_tokens"],
    }
    if context.dispatcher_mode == "stream":

        async def discard_content(_content: str) -> None:
            return None

        return await LLMClient.generate_with_stream_callback(
            **generation_kwargs,
            on_content=discard_content,
        )
    return await LLMClient.generate(**generation_kwargs)


async def update_output_metadata(context: MemoryRecallContext, response: Any) -> None:
    provider_metrics = extract_provider_token_metrics(getattr(response, "usage", None))
    if "output_tokens" in provider_metrics:
        output_tokens = provider_metrics["output_tokens"]
        context.total_output_tokens += output_tokens
        context.session_total_output_tokens = (context.session_total_output_tokens or 0) + output_tokens
        provider_metrics["output_tokens"] = context.total_output_tokens
        provider_metrics["total_output_tokens"] = context.session_total_output_tokens
    if context.latest_llm_request_metadata is None:
        return
    changed = any(context.latest_llm_request_metadata.get(key) != value for key, value in provider_metrics.items())
    context.latest_llm_request_metadata.update(provider_metrics)
    if changed and context.stream_event_callback is not None:
        await context.stream_event_callback(dict(context.latest_llm_request_metadata))


def response_is_valid(response: Any) -> bool:
    message = getattr(response, "message", None)
    if not isinstance(message, InternalMessage) or message.role != MessageRole.ASSISTANT:
        return False
    if _has_content(message.content) or _has_content(message.refusal) or len(message.tool_calls or []) != 1:
        return False
    tool_call = message.tool_calls[0]
    operation, error = validate_longterm_memory_arguments(tool_call.arguments)
    return tool_call.name == MANAGE_LONGTERM_MEMORY_TOOL_NAME and operation == "recall" and error is None


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def build_correction_messages(
    base_messages: list[InternalMessage],
    response: Any,
) -> list[InternalMessage]:
    correction_messages = [message.model_copy(deep=True) for message in base_messages]
    response_message = getattr(response, "message", None)
    if isinstance(response_message, InternalMessage):
        correction_messages.append(response_message.model_copy(deep=True))
        for tool_call in response_message.tool_calls or []:
            correction_messages.append(
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=tool_call.id,
                    content='{"status":"ignored"}',
                )
            )
    correction_messages.append(
        InternalMessage(
            role=MessageRole.USER,
            content=LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT,
        )
    )
    return correction_messages


__all__ = [
    "build_correction_messages",
    "fallback_channel",
    "generate",
    "prepare_request_messages",
    "response_is_valid",
    "select_initial_channel",
    "update_output_metadata",
]
