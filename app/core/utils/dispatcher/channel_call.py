import uuid
from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_CHAT_CHANNEL_NOT_FOUND,
    ERR_LLM_EMPTY_RESPONSE,
)
from app.core.exceptions import ApiKeyException, LLMContextLengthException, LLMException
from app.core.i18n import t
from app.core.log import channel_log_extra, get_logger
from app.core.utils.dispatcher.helpers import resolve_chat_params
from app.core.utils.dispatcher.provider_state import discard_mismatched_provider_state
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.core.utils.request_token_baseline import (
    build_provider_request_usage_metadata,
    extract_provider_token_metrics,
)
from app.models.channel import ChannelConfig, ChannelRule, ModelChannel, resolve_model_protocol
from app.models.message import InternalMessage, InternalResponse
from app.providers.llm.client import LLMClient

logger = get_logger(__name__)


ChatRequestBuilder = Callable[[dict[str, Any], ModelChannel, dict[str, Any]], list[InternalMessage] | Awaitable[list[InternalMessage]]]
RequestMetadataCallback = Callable[[dict[str, Any]], Awaitable[None]]
ContextLengthRecoveryCallback = Callable[[dict[str, Any], ModelChannel, dict[str, Any]], bool | Awaitable[bool]]


async def _resolve_request_messages(
    builder: ChatRequestBuilder,
    chat_params: dict[str, Any],
    chat_channel_obj: ModelChannel,
    model_entry: dict[str, Any],
) -> list[InternalMessage]:
    request_messages = builder(chat_params, chat_channel_obj, model_entry)
    if hasattr(request_messages, "__await__"):
        return await request_messages
    return request_messages


async def generate_chat_with_fallback(
    db: AsyncSession,
    *,
    chat_channel: ChannelConfig,
    request_builder: ChatRequestBuilder,
    call_context: str,
    cursor_key: str | None,
    uid: str,
    session_id: str,
    tools: list[dict[str, Any]] | None = None,
    require_content_or_tools: bool = True,
    require_content: bool = False,
    request_metadata_callback: RequestMetadataCallback | None = None,
    context_length_recovery_callback: ContextLengthRecoveryCallback | None = None,
) -> tuple[InternalResponse, ModelChannel, dict[str, Any], ChannelRule, dict[str, Any]]:
    excluded_priorities: set[int] = set()
    context_length_recovery_priorities: set[int] = set()
    selection = await select_channel(db, chat_channel, "CHAT", call_context=call_context, cursor_key=cursor_key)
    if not selection:
        raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

    while True:
        chat_channel_obj, model_entry, channel_rule = selection
        chat_params = resolve_chat_params(model_entry, chat_channel)
        if chat_params.get("reasoning_effort") is not None:
            chat_params["temperature"] = None
            chat_params["top_p"] = None
        try:
            request_messages = await _resolve_request_messages(
                request_builder,
                chat_params,
                chat_channel_obj,
                model_entry,
            )
            await discard_mismatched_provider_state(
                db,
                session_id=session_id,
                uid=uid,
                messages=request_messages,
                channel_id=chat_channel_obj.id,
                model_id=model_entry["model_id"],
                protocol=resolve_model_protocol(model_entry),
            )
            await db.commit()
            provider_request_id = str(uuid.uuid4())
            response = await LLMClient.generate(
                api_key=chat_channel_obj.get_decrypted_api_key(),
                base_url=chat_channel_obj.base_url,
                model_id=model_entry["model_id"],
                channel_id=chat_channel_obj.id,
                messages=request_messages,
                temperature=chat_params["temperature"],
                top_p=chat_params["top_p"],
                reasoning_effort=chat_params.get("reasoning_effort"),
                max_tokens=chat_params["max_tokens"],
                tools=tools,
                protocol=resolve_model_protocol(model_entry),
                timeout=chat_params["chat_timeout"],
                http_proxy=get_channel_http_proxy(chat_channel_obj),
                custom_headers=get_model_custom_headers(model_entry),
            )
            ai_msg = response.message
            if request_metadata_callback is not None:
                provider_token_metrics = extract_provider_token_metrics(getattr(response, "usage", None))
                await request_metadata_callback(
                    {
                        "type": "llm_request_metadata",
                        "input_tokens": provider_token_metrics.get("input_tokens", 0),
                        "input_tokens_source": provider_token_metrics.get("input_tokens_source", "estimated"),
                        "context_window_tokens": max(1, int(chat_params["context_window_k"]) * CONTEXT_WINDOW_TOKENS_PER_K),
                        "max_output_tokens": max(0, int(chat_params["max_tokens"])),
                        **({"channel_id": chat_channel_obj.id} if isinstance(chat_channel_obj.id, int) and not isinstance(chat_channel_obj.id, bool) and chat_channel_obj.id > 0 else {}),
                        "model_id": model_entry["model_id"],
                        "protocol": resolve_model_protocol(model_entry),
                        **provider_token_metrics,
                        **build_provider_request_usage_metadata(provider_request_id, provider_token_metrics),
                    }
                )
            if require_content and not (ai_msg.content or "").strip():
                raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
            if require_content_or_tools and not ai_msg.tool_calls and not ai_msg.generated_images and not (ai_msg.content or "").strip():
                raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
            return response, chat_channel_obj, model_entry, channel_rule, chat_params
        except ApiKeyException:
            raise
        except LLMException as exc:
            current_priority = channel_rule.priority
            context_recovery_required = isinstance(exc, LLMContextLengthException)
            if context_recovery_required and context_length_recovery_callback is not None and current_priority not in context_length_recovery_priorities:
                context_length_recovery_priorities.add(current_priority)
                try:
                    recovered = context_length_recovery_callback(
                        chat_params,
                        chat_channel_obj,
                        model_entry,
                    )
                    if hasattr(recovered, "__await__"):
                        recovered = await recovered
                except Exception:
                    recovered = False
                if recovered:
                    continue

            excluded_priorities.add(current_priority)
            logger.bind(
                uid=uid,
                session_id=session_id,
                **channel_log_extra(chat_channel_obj, model_entry),
            ).warning(t("LOG_DISPATCHER_NON_STREAM_CHANNEL_FAILED", error=t(exc.message, default=exc.message, **exc.kwargs)))
            selection = await select_channel(
                db,
                chat_channel,
                "CHAT",
                call_context=f"{call_context}_retry",
                excluded_priorities=excluded_priorities,
                cursor_key=cursor_key,
            )
            if not selection:
                if isinstance(exc, LLMContextLengthException):
                    raise LLMContextLengthException(provider_message=exc.provider_message) from exc
                raise
