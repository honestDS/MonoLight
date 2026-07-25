from collections.abc import Awaitable, Callable
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.constants import ERR_CHAT_CHANNEL_NOT_FOUND, ERR_LLM_EMPTY_RESPONSE
from app.core.exceptions import ApiKeyException, LLMException
from app.core.i18n import t
from app.core.log import channel_log_extra, get_logger
from app.core.utils.dispatcher.helpers import resolve_chat_params
from app.core.utils.request_token_baseline import extract_provider_token_metrics
from app.models.channel import ChannelConfig, ChannelRule, ModelChannel
from app.models.message import InternalMessage, InternalResponse
from app.providers.llm.client import LLMClient, estimate_request_context_tokens

logger = get_logger(__name__)


ChatRequestBuilder = Callable[[dict[str, Any]], list[InternalMessage] | Awaitable[list[InternalMessage]]]
RequestMetadataCallback = Callable[[dict[str, Any]], Awaitable[None]]


async def _resolve_request_messages(builder: ChatRequestBuilder, chat_params: dict[str, Any]) -> list[InternalMessage]:
    request_messages = builder(chat_params)
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
) -> tuple[InternalResponse, ModelChannel, dict[str, Any], ChannelRule, dict[str, Any]]:
    excluded_priorities: set[int] = set()
    selection = await select_channel(db, chat_channel, "CHAT", call_context=call_context, cursor_key=cursor_key)
    if not selection:
        raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

    while True:
        chat_channel_obj, model_entry, channel_rule = selection
        chat_params = resolve_chat_params(model_entry, chat_channel)
        try:
            request_messages = await _resolve_request_messages(request_builder, chat_params)
            estimated_input_tokens = 0
            request_context_kwargs: dict[str, Any] = {}
            if request_metadata_callback is not None:
                estimated_input_tokens = estimate_request_context_tokens(request_messages, tools)
                request_context_kwargs["request_context_tokens"] = estimated_input_tokens
            await db.commit()
            response = await LLMClient.generate(
                api_key=chat_channel_obj.get_decrypted_api_key(),
                base_url=chat_channel_obj.base_url,
                model_id=model_entry["model_id"],
                messages=request_messages,
                temperature=chat_params["temperature"],
                top_p=chat_params["top_p"],
                max_tokens=chat_params["max_tokens"],
                tools=tools,
                protocol=getattr(chat_channel_obj, "protocol", "openai"),
                timeout=chat_params["chat_timeout"],
                **request_context_kwargs,
            )
            ai_msg = response.message
            if require_content and not (ai_msg.content or "").strip():
                raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
            if require_content_or_tools and not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
            if request_metadata_callback is not None:
                await request_metadata_callback(
                    {
                        "type": "llm_request_metadata",
                        "input_tokens": estimated_input_tokens,
                        "input_tokens_source": "estimated",
                        "context_window_tokens": max(1, int(chat_params["context_window_k"]) * 1024),
                        "max_output_tokens": max(0, int(chat_params["max_tokens"])),
                        **extract_provider_token_metrics(getattr(response, "usage", None)),
                    }
                )
            return response, chat_channel_obj, model_entry, channel_rule, chat_params
        except ApiKeyException:
            raise
        except LLMException as exc:
            excluded_priorities.add(channel_rule.priority)
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
                raise
