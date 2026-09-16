import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from app.core.channel_router import select_channel
from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_LLM_EMPTY_RESPONSE,
    ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED,
)
from app.core.context import ContextManager
from app.core.crud.session.session import session_crud
from app.core.dispatchers.interactive_state import InteractiveDispatchState
from app.core.exceptions import ApiKeyException, LLMException
from app.core.i18n import t
from app.core.log import channel_log_extra
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.context_summary_checkpoint import apply_context_summary_checkpoint
from app.core.utils.dispatcher.helpers import (
    format_exception_message,
    get_multimodal_from_entry,
    reassemble_multimodal_messages,
    resolve_chat_params,
)
from app.core.utils.dispatcher.markdown_instruction import materialize_user_environment_prompts, refresh_latest_user_max_output_tokens_instruction
from app.core.utils.http_proxy import get_channel_http_proxy
from app.core.utils.model_request_headers import get_model_custom_headers
from app.core.utils.request_token_baseline import (
    accumulate_session_cache_metrics,
    build_provider_request_usage_metadata,
    build_request_token_baseline,
    build_session_cache_metrics,
    estimate_incremental_input_tokens,
    extract_provider_token_metrics,
    extract_reusable_token_metrics,
    extract_session_total_output_tokens,
    merge_session_cache_token_totals,
)
from app.core.utils.time import get_local_time
from app.models.channel import resolve_model_protocol
from app.models.message import InternalMessage
from app.providers.llm.client import LLMClient, estimate_request_context_tokens

from .interactive_helpers import (
    _AgentLoopStreamState,
    _emit_agent_loop_output,
    _handle_stream_content,
    build_pending_multimodal_input_message,
    collect_pending_multimodal_file_inputs,
)

__all__ = [
    "InteractiveGenerationResult",
    "generate_interactive_turn",
]


@dataclass
class InteractiveGenerationResult:
    message: InternalMessage
    attempt_started_at: datetime
    finish_reason: str | None
    finish_details: dict[str, Any] | None
    provider_metadata: dict[str, Any] | None
    refusal: str | None
    message_provider_metadata: dict[str, Any] | None


async def generate_interactive_turn(
    state: InteractiveDispatchState,
    *,
    current_tools,
    response_id: str,
) -> InteractiveGenerationResult:
    excluded_priorities: set[int] = set()
    emitted_agent_loop_start = False
    stream_state = _AgentLoopStreamState(
        callback=state.stream_event_callback,
        current_turn=state.current_turn,
        response_id=response_id,
        expose_tool_call_content=state.expose_tool_call_content,
        show_tool_calls=state.show_tool_calls,
    )

    while True:
        stream_state.emitted_stream_content = False
        stream_state.buffered_content_chunks.clear()
        try:
            if state.checkpoint_state.upper_message_id is not None:
                state.messages = await apply_context_summary_checkpoint(
                    state.db,
                    session_id=state.session_id,
                    uid=state.uid,
                    profile=state.profile,
                    cfg=state.cfg,
                    messages=state.messages,
                    trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
                    fixed_upper_message_id=state.checkpoint_state.upper_message_id,
                    context_window_k=state.chat_params["context_window_k"],
                    max_tokens=state.chat_params["max_tokens"],
                    tools=current_tools,
                    work_validity_checker=state.context_summary_work_validity_checker,
                    lifecycle_event_callback=state.context_summary_lifecycle_callback,
                    model_id=state.model_entry["model_id"],
                    protocol=resolve_model_protocol(state.model_entry),
                    previous_llm_request_metadata=(state.latest_llm_request_metadata if isinstance(state.latest_llm_request_metadata, dict) and state.latest_llm_request_metadata.get("input_tokens_source") == "provider" else None),
                )
            pending_file_inputs = collect_pending_multimodal_file_inputs(state.messages)
            if pending_file_inputs and not state.img_understanding:
                raise LLMException(message=ERR_LLM_MULTIMODAL_INPUT_UNSUPPORTED)
            request_messages = materialize_user_environment_prompts(state.messages)
            pending_multimodal_message = build_pending_multimodal_input_message(
                pending_file_inputs,
                image_understanding=state.img_understanding,
                audio_understanding=state.audio_understanding,
                video_understanding=state.video_understanding,
            )
            if pending_multimodal_message is not None:
                request_messages.append(pending_multimodal_message)
            request_messages = ContextManager.trim_messages_for_model_request(
                messages=request_messages,
                uid=state.uid,
                session_id=state.session_id,
                context_window_k=state.chat_params["context_window_k"],
                max_tokens=state.chat_params["max_tokens"],
                tools=current_tools,
            )
            model_id = state.model_entry["model_id"]
            protocol = resolve_model_protocol(state.model_entry)
            generation_kwargs = {
                "api_key": state.chat_channel_obj.get_decrypted_api_key(),
                "base_url": state.chat_channel_obj.base_url,
                "model_id": model_id,
                "messages": request_messages,
                "temperature": state.chat_params["temperature"],
                "top_p": state.chat_params["top_p"],
                "reasoning_effort": state.chat_params.get("reasoning_effort"),
                "max_tokens": state.chat_params["max_tokens"],
                "tools": current_tools,
                "protocol": protocol,
                "timeout": state.chat_params["chat_timeout"],
                "http_proxy": get_channel_http_proxy(state.chat_channel_obj),
                "custom_headers": get_model_custom_headers(state.model_entry),
            }
            previous_in_memory_llm_request_metadata = state.latest_llm_request_metadata
            session = None
            if hasattr(state.db, "execute"):
                session = await session_crud.get_by_session_id(state.db, state.session_id)
                if session is not None:
                    await state.db.refresh(session)
            context_summary_revision = session.context_summary_revision if session is not None else 0
            context_content_revision = session.context_content_revision if session is not None else 0
            previous_session_llm_request_metadata = session.llm_request_metadata if session is not None else None
            state.checkpoint_state.session_total_input_tokens, state.checkpoint_state.session_total_cached_tokens = merge_session_cache_token_totals(
                previous_session_llm_request_metadata,
                total_input_tokens=state.checkpoint_state.session_total_input_tokens,
                total_cached_tokens=state.checkpoint_state.session_total_cached_tokens,
            )
            persisted_session_total_output_tokens = extract_session_total_output_tokens(previous_session_llm_request_metadata)
            if state.checkpoint_state.session_total_output_tokens is None:
                state.checkpoint_state.session_total_output_tokens = persisted_session_total_output_tokens
            else:
                state.checkpoint_state.session_total_output_tokens = max(
                    state.checkpoint_state.session_total_output_tokens,
                    persisted_session_total_output_tokens,
                )
            previous_input_token_baseline_metadata = previous_in_memory_llm_request_metadata if isinstance(previous_in_memory_llm_request_metadata, dict) and previous_in_memory_llm_request_metadata.get("input_tokens_source") == "provider" else previous_session_llm_request_metadata
            previous_display_token_metadata = previous_in_memory_llm_request_metadata if isinstance(previous_in_memory_llm_request_metadata, dict) else previous_input_token_baseline_metadata
            incremental_input_tokens = estimate_incremental_input_tokens(
                request_messages,
                current_tools,
                previous_input_token_baseline_metadata,
                model_id=model_id,
                protocol=protocol,
                context_summary_revision=context_summary_revision,
                context_content_revision=context_content_revision,
            )
            estimated_input_tokens = incremental_input_tokens if incremental_input_tokens is not None else estimate_request_context_tokens(request_messages, current_tools)
            generation_kwargs["request_context_tokens"] = estimated_input_tokens
            state.latest_llm_request_metadata = {
                "type": "llm_request_metadata",
                "turn": state.current_turn,
                "response_id": response_id,
                "input_tokens": estimated_input_tokens,
                "input_tokens_source": "estimated",
                "total_output_tokens": state.checkpoint_state.session_total_output_tokens,
                "context_window_tokens": max(1, int(state.chat_params["context_window_k"]) * CONTEXT_WINDOW_TOKENS_PER_K),
                "max_output_tokens": max(0, int(state.chat_params["max_tokens"])),
                **build_request_token_baseline(
                    request_messages,
                    current_tools,
                    model_id=model_id,
                    protocol=protocol,
                    context_summary_revision=context_summary_revision,
                    context_content_revision=context_content_revision,
                ),
                **extract_reusable_token_metrics(previous_display_token_metadata),
                **build_session_cache_metrics(
                    state.checkpoint_state.session_total_input_tokens,
                    state.checkpoint_state.session_total_cached_tokens,
                ),
            }
            if state.stream_event_callback is not None:
                await state.stream_event_callback(dict(state.latest_llm_request_metadata))
            await state.db.commit()
            provider_request_id = str(uuid.uuid4())
            attempt_started_at = get_local_time()
            if state.stream_event_callback is None:
                response = await LLMClient.generate(**generation_kwargs)
            else:
                if not emitted_agent_loop_start:
                    await state.stream_event_callback(
                        {
                            "type": "agent_loop_start",
                            "turn": state.current_turn,
                            "response_id": response_id,
                        }
                    )
                    emitted_agent_loop_start = True

                response = await LLMClient.generate_with_stream_callback(
                    **generation_kwargs,
                    on_content=partial(_handle_stream_content, stream_state),
                )
            ai_msg = response.message
            response_finish_reason = getattr(response, "finish_reason", None)
            response_finish_details = getattr(response, "finish_details", None)
            response_provider_metadata = getattr(response, "provider_metadata", None)
            ai_refusal = getattr(ai_msg, "refusal", None)
            ai_provider_metadata = getattr(ai_msg, "provider_metadata", None)
            provider_token_metrics = extract_provider_token_metrics(getattr(response, "usage", None))
            provider_request_usage_metadata = build_provider_request_usage_metadata(provider_request_id, provider_token_metrics)
            state.checkpoint_state.session_total_input_tokens, state.checkpoint_state.session_total_cached_tokens = accumulate_session_cache_metrics(
                provider_token_metrics,
                total_input_tokens=state.checkpoint_state.session_total_input_tokens,
                total_cached_tokens=state.checkpoint_state.session_total_cached_tokens,
            )
            if "output_tokens" in provider_token_metrics:
                state.checkpoint_state.total_output_tokens += provider_token_metrics["output_tokens"]
                state.checkpoint_state.session_total_output_tokens += provider_token_metrics["output_tokens"]
                provider_token_metrics["output_tokens"] = state.checkpoint_state.total_output_tokens
                provider_token_metrics["total_output_tokens"] = state.checkpoint_state.session_total_output_tokens
            metadata_changed = any(state.latest_llm_request_metadata.get(field) != value for field, value in provider_token_metrics.items())
            state.latest_llm_request_metadata.update(provider_token_metrics)
            if state.request_metadata_callback is not None:
                await state.request_metadata_callback({**state.latest_llm_request_metadata, **provider_request_usage_metadata})
            if metadata_changed and state.stream_event_callback is not None:
                await state.stream_event_callback(dict(state.latest_llm_request_metadata))
            has_content = bool(ai_msg.content.strip()) if isinstance(ai_msg.content, str) else bool(ai_msg.content)
            has_refusal = bool(ai_refusal.strip()) if isinstance(ai_refusal, str) else False
            legal_empty_finish_reasons = {"length", "content_filter", "refusal", "incomplete"}
            if not ai_msg.tool_calls and not has_content and not has_refusal and response_finish_reason not in legal_empty_finish_reasons:
                raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
            hidden_tool_round = bool(ai_msg.tool_calls) and not state.show_tool_calls
            if not hidden_tool_round:
                await _emit_agent_loop_output(stream_state)
            if state.stream_event_callback is not None and state.show_tool_calls and not hidden_tool_round and state.expose_tool_call_content and not stream_state.emitted_stream_content and isinstance(ai_msg.content, str) and ai_msg.content.strip():
                await state.stream_event_callback(
                    {
                        "type": "content",
                        "content": ai_msg.content,
                        "turn": state.current_turn,
                        "response_id": response_id,
                    }
                )
                stream_state.emitted_stream_content = True
            if state.stream_event_callback is not None and (not state.expose_tool_call_content or not state.show_tool_calls) and not ai_msg.tool_calls:
                buffered_content_chunks = stream_state.buffered_content_chunks or ([ai_msg.content] if isinstance(ai_msg.content, str) and ai_msg.content else [])
                if "".join(buffered_content_chunks).strip():
                    for content_chunk in buffered_content_chunks:
                        await state.stream_event_callback(
                            {
                                "type": "content",
                                "content": content_chunk,
                                "turn": state.current_turn,
                                "response_id": response_id,
                            }
                        )
            return InteractiveGenerationResult(
                message=ai_msg,
                attempt_started_at=attempt_started_at,
                finish_reason=response_finish_reason,
                finish_details=response_finish_details,
                provider_metadata=response_provider_metadata,
                refusal=ai_refusal,
                message_provider_metadata=ai_provider_metadata,
            )
        except ApiKeyException:
            raise
        except LLMException as exc:
            if stream_state.emitted_stream_content:
                raise
            excluded_priorities.add(state.channel_rule.priority)
            state.dispatch_logger.bind(
                uid=state.uid,
                session_id=state.session_id,
                **channel_log_extra(state.chat_channel_obj, state.model_entry),
            ).warning(t("LOG_DISPATCHER_NON_STREAM_CHANNEL_FAILED", error=format_exception_message(exc)))
            selection = await select_channel(
                state.db,
                state.chat_channel,
                "CHAT",
                call_context=f"chat_dispatch_{state.dispatcher_mode}_retry",
                excluded_priorities=excluded_priorities,
                cursor_key=state.chat_cursor_key,
            )
            if not selection:
                raise
            previous_max_tokens = state.chat_params["max_tokens"]
            state.chat_channel_obj, state.model_entry, state.channel_rule = selection
            state.img_understanding, state.audio_understanding, state.video_understanding = get_multimodal_from_entry(state.model_entry)
            state.chat_params = resolve_chat_params(state.model_entry, state.chat_channel)
            if state.chat_params["max_tokens"] != previous_max_tokens:
                await refresh_latest_user_max_output_tokens_instruction(
                    state.db,
                    state.messages,
                    state.chat_params["max_tokens"],
                )
            reassemble_multimodal_messages(
                state.messages,
                state.img_understanding,
                state.audio_understanding,
                state.video_understanding,
            )
