import uuid
from typing import Any

from app.core.channel_router import select_channel
from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    MEMORY_RECALL_PRECHECK_HISTORY_USER_ROUNDS,
)
from app.core.context import ContextManager
from app.core.crud.session.session import session_crud
from app.core.prompts import (
    LONGTERM_MEMORY_RECALL_CORRECTION_PROMPT,
    LONGTERM_MEMORY_RECALL_PRECHECK_PROMPT,
)
from app.core.tools.longterm_memory import (
    MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME,
    MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_SCHEMA,
    validate_longterm_memory_arguments,
)
from app.core.utils.context_messages import is_context_summary_message
from app.core.utils.dispatcher.helpers import (
    get_multimodal_from_entry,
    reassemble_multimodal_messages,
    resolve_chat_params,
)
from app.core.utils.dispatcher.markdown_instruction import (
    materialize_user_environment_prompts,
    refresh_latest_user_max_output_tokens_instruction,
)
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.llm_request_params import build_memory_recall_precheck_generation_params
from app.core.utils.model_request_headers import get_model_custom_headers
from app.core.utils.request_token_baseline import (
    accumulate_session_cache_metrics,
    build_request_token_baseline,
    build_session_cache_metrics,
    extract_provider_token_metrics,
    extract_session_total_output_tokens,
    merge_session_cache_token_totals,
)
from app.models.channel import resolve_model_protocol
from app.models.message import InternalMessage, MessageRole
from app.providers.llm.client import LLMClient, estimate_request_context_tokens

from .types import MemoryRecallContext


def build_precheck_request_messages(messages: list[InternalMessage]) -> list[InternalMessage]:
    summary_messages = [message for message in messages if is_context_summary_message(message)]
    latest_summary = [summary_messages[-1].model_copy(deep=True)] if summary_messages else []

    dialogue: list[InternalMessage] = []
    for message in messages:
        if message.role not in {MessageRole.USER, MessageRole.ASSISTANT} or is_context_summary_message(message):
            continue
        if message.role == MessageRole.ASSISTANT and message.tool_calls and not _has_content(message.content):
            continue
        dialogue.append(
            message.model_copy(
                update={
                    "reasoning_content": None,
                    "provider_metadata": None,
                    "tool_calls": None,
                },
                deep=True,
            )
        )
    user_indexes = [index for index, message in enumerate(dialogue) if message.role == MessageRole.USER]
    if not user_indexes:
        recent_dialogue = dialogue
    else:
        history_user_count = min(MEMORY_RECALL_PRECHECK_HISTORY_USER_ROUNDS + 1, len(user_indexes))
        start_index = user_indexes[-history_user_count]
        recent_dialogue = dialogue[start_index:]

    return [*latest_summary, *recent_dialogue]


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
    previous_max_tokens = context.chat_params.get("max_tokens")
    context.chat_channel_obj, context.model_entry, context.channel_rule = selection
    context.chat_params = resolve_chat_params(context.model_entry, context.chat_channel)
    if previous_max_tokens is not None and context.chat_params["max_tokens"] != previous_max_tokens:
        await refresh_latest_user_max_output_tokens_instruction(
            context.db,
            context.messages,
            context.chat_params["max_tokens"],
        )
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
    precheck_messages = build_precheck_request_messages(messages) if is_main_context else messages
    if is_main_context:
        precheck_messages = [
            InternalMessage(role=MessageRole.SYSTEM, content=LONGTERM_MEMORY_RECALL_PRECHECK_PROMPT),
            *precheck_messages,
        ]
    request_messages = materialize_user_environment_prompts(precheck_messages)
    protocol = resolve_model_protocol(context.model_entry)
    precheck_generation_params = build_memory_recall_precheck_generation_params(
        model_entry=context.model_entry,
        protocol=protocol,
    )
    request_messages = ContextManager.trim_messages_for_model_request(
        messages=request_messages,
        uid=context.uid,
        session_id=context.session_id,
        context_window_k=context.chat_params["context_window_k"],
        max_tokens=precheck_generation_params["max_tokens"],
        tools=[MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_SCHEMA],
    )
    session = await session_crud.get_by_session_id(context.db, context.session_id)
    if session is not None and hasattr(context.db, "refresh"):
        await context.db.refresh(session)
    context.session_total_input_tokens, context.session_total_cached_tokens = merge_session_cache_token_totals(
        getattr(session, "llm_request_metadata", None),
        total_input_tokens=context.session_total_input_tokens,
        total_cached_tokens=context.session_total_cached_tokens,
    )
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
        [MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_SCHEMA],
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
        "max_output_tokens": max(0, int(precheck_generation_params["max_tokens"])),
        **build_request_token_baseline(
            request_messages,
            [MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_SCHEMA],
            model_id=model_id,
            protocol=protocol,
            context_summary_revision=summary_revision,
            context_content_revision=content_revision,
        ),
        **build_session_cache_metrics(
            context.session_total_input_tokens,
            context.session_total_cached_tokens,
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
    protocol = resolve_model_protocol(model_entry)
    generation_kwargs = {
        "api_key": channel.get_decrypted_api_key(),
        "base_url": channel.base_url,
        "model_id": model_entry["model_id"],
        "messages": request_messages,
        **build_memory_recall_precheck_generation_params(
            model_entry=model_entry,
            protocol=protocol,
        ),
        "tools": [MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_SCHEMA],
        "protocol": protocol,
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
    context.session_total_input_tokens, context.session_total_cached_tokens = accumulate_session_cache_metrics(
        provider_metrics,
        total_input_tokens=context.session_total_input_tokens,
        total_cached_tokens=context.session_total_cached_tokens,
    )
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
    knowledge_query = tool_call.arguments.get("knowledge_query")
    return tool_call.name == MANAGE_MEMORY_AND_KNOWLEDGE_TOOL_NAME and operation == "recall" and error is None and isinstance(knowledge_query, str) and bool(knowledge_query.strip())


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def build_correction_messages(
    base_messages: list[InternalMessage],
    response: Any,
) -> list[InternalMessage]:
    correction_messages = [message.model_copy(deep=True) for message in build_precheck_request_messages(base_messages)]
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
