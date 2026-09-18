import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, MutableSet
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.channel_router import select_channel
from app.core.constants import ERR_CHAT_CHANNEL_NOT_FOUND, ERR_INTERNAL_SERVER_ERROR
from app.core.crud.account.user import user_crud
from app.core.crud.profile.profile import profile_crud
from app.core.dispatchers.interactive_generation import generate_interactive_turn
from app.core.dispatchers.interactive_state import build_interactive_dispatch_state
from app.core.dispatchers.memory import MemoryRecallContext, run_memory_recall_precheck
from app.core.exceptions import BaseBusinessException, LLMException, ServerException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.profile_selection import resolve_profile_for_session
from app.core.prompts import PROMPT_MAX_TURNS_REACHED
from app.core.tools import get_tools_for_profile
from app.core.utils.assistant_files import build_assistant_files_content as build_assistant_content
from app.core.utils.context_summary.common import ContextSummaryWorkValidityChecker
from app.core.utils.dispatcher.append_new_user_messages import append_new_user_messages
from app.core.utils.dispatcher.helpers import (
    dump_output_history,
    get_multimodal_from_entry,
    resolve_chat_params,
)
from app.core.utils.dispatcher.mark_initial_message_processed import mark_initial_message_processed
from app.core.utils.dispatcher.prepare_messages import prepare_messages
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_initial_message import save_initial_message
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.core.utils.message_assembler import MessageAssembler
from app.models.message import InternalMessage, MessageRole
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse

from .interactive_helpers import (
    _AdditionalUserMessagesContext,
    _fetch_additional_user_messages,
    _save_execution_checkpoint,
    memory_recall_needs_precheck,
    update_memory_recall_boundary,
)
from .interactive_tools import handle_interactive_tool_round

__all__ = [
    "dispatch_interactive",
]

logger = get_logger(__name__)


async def dispatch_interactive(
    db: AsyncSession,
    message: str | list[dict[str, Any]],
    uid: str,
    session_id: str = "default",
    attachments: list[str] | None = None,
    active_tasks: MutableSet[asyncio.Task] | None = None,
    session_source: str = "http",
    persisted_initial_message: InternalMessage | None = None,
    history_before_id: int | None = None,
    frozen_user_message_ids: list[int] | None = None,
    final_message_dedupe_key: str | None = None,
    persisted_profile_id: int | None = None,
    stream_event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    context_summary_lifecycle_callback: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    additional_user_messages_fetcher: Callable[[], Awaitable[UserInputBatch | list[InternalMessage] | None]] | None = None,
    execution_resume_state: dict[str, Any] | None = None,
    execution_checkpoint_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    context_summary_work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
    expose_tool_call_content: bool = True,
    show_tool_calls: bool = True,
    additional_system_prompt: str | None = None,
    dispatcher_mode: Literal["non_stream", "stream"] = "non_stream",
    request_metadata_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    *,
    validate_initial_message_before_save: Callable[..., Awaitable[None]],
):
    try:
        dispatch_logger = logger if dispatcher_mode == "non_stream" else get_logger("app.core.dispatchers.stream")
        user = await user_crud.get_by_uid(db, uid)
        username = user.username if user else "Unknown"
        profile = await profile_crud.get_with_relations(db, persisted_profile_id) if persisted_profile_id is not None else await resolve_profile_for_session(db, uid=uid, session_id=session_id)

        if execution_resume_state is None:
            dispatch_logger.bind(uid=uid, session_id=session_id).info(
                t(
                    "LOG_DISPATCHER_USER_MESSAGE",
                    username=username,
                    message=message,
                    attachments=str(attachments),
                )
            )

        await validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)

        initial_msg = persisted_initial_message or await save_initial_message(db, session_id, uid, profile, message, attachments, source=session_source)

        queue_managed = persisted_initial_message is not None
        additional_user_messages_context = _AdditionalUserMessagesContext(
            db=db,
            session_id=session_id,
            uid=uid,
            queue_managed=queue_managed,
            fetcher=additional_user_messages_fetcher,
        )
        state = build_interactive_dispatch_state(
            db=db,
            uid=uid,
            session_id=session_id,
            active_tasks=active_tasks,
            session_source=session_source,
            frozen_user_message_ids=frozen_user_message_ids,
            final_message_dedupe_key=final_message_dedupe_key,
            context_summary_lifecycle_callback=context_summary_lifecycle_callback,
            context_summary_work_validity_checker=context_summary_work_validity_checker,
            expose_tool_call_content=expose_tool_call_content,
            show_tool_calls=show_tool_calls,
            dispatcher_mode=dispatcher_mode,
            request_metadata_callback=request_metadata_callback,
            stream_event_callback=stream_event_callback,
            dispatch_logger=dispatch_logger,
            username=username,
            profile=profile,
            initial_msg=initial_msg,
            queue_managed=queue_managed,
            additional_user_messages_context=additional_user_messages_context,
            execution_resume_state=execution_resume_state,
            execution_checkpoint_callback=execution_checkpoint_callback,
        )

        initial_memory_recall_boundary = max(frozen_user_message_ids) if frozen_user_message_ids else initial_msg.id
        resumed_memory_recall_boundary = execution_resume_state.get("memory_recall_boundary_message_id") if execution_resume_state else None
        resumed_memory_recall_status = execution_resume_state.get("memory_recall_status") if execution_resume_state else None

        while True:
            try:
                state.cfg = await validate_profile_and_cfg(db, profile)
                state.memory_enabled = bool(getattr(getattr(state.cfg, "memory", None), "enabled", False))
                if not state.memory_enabled:
                    state.checkpoint_state.memory_recall_boundary_message_id = None
                    state.checkpoint_state.memory_recall_status = None
                elif state.checkpoint_state.memory_recall_boundary_message_id is None:
                    update_memory_recall_boundary(state.checkpoint_state, initial_memory_recall_boundary)
                    if resumed_memory_recall_boundary == initial_memory_recall_boundary:
                        state.checkpoint_state.memory_recall_status = resumed_memory_recall_status

                if state.is_first_iter:
                    await mark_initial_message_processed(db, state.initial_msg.id)

                state.chat_channel = state.cfg.channel.chat_channel
                state.chat_cursor_key = f"{profile.id}:CHAT"
                selection = await select_channel(
                    db,
                    state.chat_channel,
                    "CHAT",
                    call_context=f"chat_dispatch_{dispatcher_mode}",
                    cursor_key=state.chat_cursor_key,
                )
                if not selection:
                    raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

                state.chat_channel_obj, state.model_entry, state.channel_rule = selection
                state.img_understanding, state.audio_understanding, state.video_understanding = get_multimodal_from_entry(state.model_entry)
                state.chat_params = resolve_chat_params(state.model_entry, state.chat_channel)
                state.tools, state.allowed_knowledge_base_ids = await get_tools_for_profile(db, profile)

                if state.execution_resume_state is not None:
                    state.messages = [InternalMessage.model_validate(item) for item in state.execution_resume_state.get("messages", [])]
                    state.current_turn = int(state.execution_resume_state.get("current_turn", 0))
                    state.execution_resume_state = None
                else:
                    state.messages = await prepare_messages(
                        db,
                        session_id,
                        uid,
                        profile,
                        state.cfg,
                        state.initial_msg,
                        message,
                        state.is_first_iter,
                        context_window_k=state.chat_params["context_window_k"],
                        max_tokens=state.chat_params["max_tokens"],
                        tools=state.tools,
                        history_before_id=history_before_id,
                        additional_system_prompt=additional_system_prompt,
                    )
                    state.current_turn = 0

                for idx, current_message in enumerate(state.messages):
                    if current_message.role == MessageRole.USER and (current_message.attachments or isinstance(current_message.content, list)):
                        is_history = idx != len(state.messages) - 1
                        state.messages[idx] = MessageAssembler.assemble(
                            current_message,
                            image_understanding=state.img_understanding,
                            audio_understanding=state.audio_understanding,
                            video_understanding=state.video_understanding,
                            is_history=is_history,
                        )

                max_turns = state.cfg.tool.max_turns

                while state.current_turn <= max_turns:
                    new_user_batch = await _fetch_additional_user_messages(state.additional_user_messages_context, state.chat_params["max_tokens"])
                    if new_user_batch is not None:
                        state.current_turn = 0
                        append_new_user_messages(
                            state.cfg,
                            state.messages,
                            new_user_batch.messages,
                            state.img_understanding,
                            state.audio_understanding,
                            state.video_understanding,
                        )
                        state.checkpoint_state.upper_message_id = new_user_batch.summary_boundary_message_id
                        if state.memory_enabled:
                            update_memory_recall_boundary(state.checkpoint_state, new_user_batch.latest_message_id)

                    if state.memory_enabled and memory_recall_needs_precheck(state.checkpoint_state):
                        state.checkpoint_state.memory_recall_status = "pending"
                        await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)
                        memory_recall_result = await run_memory_recall_precheck(
                            MemoryRecallContext(
                                db=db,
                                uid=uid,
                                session_id=session_id,
                                profile=profile,
                                cfg=state.cfg,
                                username=username,
                                messages=state.messages,
                                turn_messages=state.turn_messages,
                                current_user_boundary_message_id=state.checkpoint_state.memory_recall_boundary_message_id,
                                upper_message_id=state.checkpoint_state.upper_message_id,
                                chat_channel=state.chat_channel,
                                chat_cursor_key=state.chat_cursor_key,
                                chat_channel_obj=state.chat_channel_obj,
                                model_entry=state.model_entry,
                                channel_rule=state.channel_rule,
                                chat_params=state.chat_params,
                                dispatcher_mode=dispatcher_mode,
                                stream_event_callback=stream_event_callback,
                                show_tool_calls=show_tool_calls,
                                expose_tool_call_content=expose_tool_call_content,
                                context_summary_callback=context_summary_lifecycle_callback,
                                context_summary_checker=context_summary_work_validity_checker,
                                allowed_knowledge_base_ids=state.allowed_knowledge_base_ids,
                                latest_llm_request_metadata=state.latest_llm_request_metadata,
                                total_output_tokens=state.checkpoint_state.total_output_tokens,
                                session_total_output_tokens=state.checkpoint_state.session_total_output_tokens,
                                session_total_input_tokens=state.checkpoint_state.session_total_input_tokens,
                                session_total_cached_tokens=state.checkpoint_state.session_total_cached_tokens,
                            )
                        )
                        state.messages = memory_recall_result.messages
                        state.turn_messages = memory_recall_result.turn_messages
                        state.checkpoint_state.turn_messages = state.turn_messages
                        state.chat_channel = memory_recall_result.chat_channel
                        state.chat_cursor_key = memory_recall_result.chat_cursor_key
                        state.chat_channel_obj = memory_recall_result.chat_channel_obj
                        state.model_entry = memory_recall_result.model_entry
                        state.channel_rule = memory_recall_result.channel_rule
                        state.chat_params = memory_recall_result.chat_params
                        state.latest_llm_request_metadata = memory_recall_result.latest_llm_request_metadata
                        state.checkpoint_state.total_output_tokens = memory_recall_result.total_output_tokens
                        state.checkpoint_state.session_total_output_tokens = memory_recall_result.session_total_output_tokens
                        state.checkpoint_state.session_total_input_tokens = memory_recall_result.session_total_input_tokens
                        state.checkpoint_state.session_total_cached_tokens = memory_recall_result.session_total_cached_tokens
                        state.img_understanding, state.audio_understanding, state.video_understanding = get_multimodal_from_entry(state.model_entry)
                        state.checkpoint_state.memory_recall_status = memory_recall_result.status
                        await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)

                    state.current_turn += 1

                    if state.current_turn == max_turns:
                        summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
                        state.messages.append(InternalMessage(role=MessageRole.USER, content=summary_notice))
                        current_tools = state.tools
                        current_tool_choice = "none"
                    else:
                        current_tools = state.tools
                        current_tool_choice = "auto"

                    response_id = str(uuid.uuid4())
                    generation_result = await generate_interactive_turn(
                        state,
                        current_tools=current_tools,
                        tool_choice=current_tool_choice,
                        response_id=response_id,
                    )
                    ai_msg = generation_result.message

                    if not ai_msg.tool_calls and state.files_to_user:
                        ai_msg.content = build_assistant_content(ai_msg.content, state.files_to_user)

                    state.dispatch_logger.bind(uid=uid, session_id=session_id).info(
                        t(
                            "LOG_DISPATCHER_LLM_RESPONSE",
                            username=username,
                            turn=state.current_turn,
                            content=ai_msg.content or "[工具调用]",
                        )
                    )

                    state.messages.append(ai_msg)
                    state.turn_messages.append(ai_msg)

                    new_user_batch = await _fetch_additional_user_messages(state.additional_user_messages_context, state.chat_params["max_tokens"]) if not ai_msg.tool_calls else None
                    saved_msg = await save_assistant_message(
                        db,
                        session_id,
                        uid,
                        profile.id,
                        ai_msg,
                        dedupe_key=final_message_dedupe_key if not ai_msg.tool_calls and new_user_batch is None else None,
                        created_at=generation_result.attempt_started_at if stream_event_callback is not None else None,
                    )
                    if stream_event_callback is not None and not (ai_msg.tool_calls and not show_tool_calls):
                        turn_end_content = saved_msg.content if saved_msg is not None else ai_msg.content
                        if ai_msg.tool_calls:
                            turn_end_content = ai_msg.content if expose_tool_call_content else None
                        if isinstance(turn_end_content, str) and not turn_end_content.strip():
                            turn_end_content = None
                        turn_end_event: dict[str, Any] = {
                            "type": "turn_end",
                            "turn": state.current_turn,
                            "response_id": response_id,
                        }
                        saved_message_id = getattr(saved_msg, "id", None)
                        if saved_message_id is not None:
                            turn_end_event["message_id"] = saved_message_id
                        turn_end_values = {
                            "content": turn_end_content,
                            "reasoning_content": ai_msg.reasoning_content,
                            "finish_reason": generation_result.finish_reason,
                            "finish_details": generation_result.finish_details,
                            "refusal": generation_result.refusal,
                            "provider_metadata": generation_result.provider_metadata,
                            "message_provider_metadata": generation_result.message_provider_metadata,
                        }
                        turn_end_event.update({key: value for key, value in turn_end_values.items() if value is not None})
                        await stream_event_callback(turn_end_event)

                    if not ai_msg.tool_calls:
                        state.final_ai_content = ai_msg.content
                        state.final_reasoning_content = ai_msg.reasoning_content
                        state.final_finish_reason = generation_result.finish_reason
                        state.final_finish_details = generation_result.finish_details
                        state.final_provider_metadata = generation_result.provider_metadata
                        state.final_refusal = generation_result.refusal
                        state.final_message_provider_metadata = generation_result.message_provider_metadata
                        if new_user_batch is None:
                            break

                        state.dispatch_logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_NON_STREAM_RESPONSE_CONTINUE"))
                        append_new_user_messages(
                            state.cfg,
                            state.messages,
                            new_user_batch.messages,
                            state.img_understanding,
                            state.audio_understanding,
                            state.video_understanding,
                        )
                        state.checkpoint_state.upper_message_id = new_user_batch.summary_boundary_message_id
                        if state.memory_enabled:
                            update_memory_recall_boundary(state.checkpoint_state, new_user_batch.latest_message_id)

                        state.current_turn = 0
                        await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)
                        continue

                    tool_response = await handle_interactive_tool_round(
                        state,
                        ai_msg=ai_msg,
                        saved_msg=saved_msg,
                        response_id=response_id,
                    )
                    if tool_response is not None:
                        return tool_response

            finally:
                state.is_first_iter = False

            if state.queue_managed:
                break
            new_user_batch = await _fetch_additional_user_messages(state.additional_user_messages_context, state.chat_params["max_tokens"])
            if new_user_batch is None:
                break
            state.checkpoint_state.upper_message_id = new_user_batch.summary_boundary_message_id
            if state.memory_enabled:
                update_memory_recall_boundary(state.checkpoint_state, new_user_batch.latest_message_id)

        response = LLMResponse(
            choices=[
                LLMChoice(
                    message=LLMChoiceMessage(
                        role=MessageRole.ASSISTANT,
                        content=state.final_ai_content,
                        reasoning_content=state.final_reasoning_content,
                        refusal=state.final_refusal,
                        provider_metadata=state.final_message_provider_metadata,
                    ),
                    finish_reason=state.final_finish_reason or "stop",
                    finish_details=state.final_finish_details,
                    provider_metadata=state.final_provider_metadata,
                    created_at=time.time(),
                )
            ],
            history=dump_output_history(
                state.turn_messages,
                show_tool_calls=show_tool_calls,
            ),
            files=state.files_to_user or None,
        ).model_dump()
        if state.latest_llm_request_metadata is not None:
            response["llm_request_metadata"] = state.latest_llm_request_metadata
        return response

    except BaseBusinessException:
        raise
    except Exception as e:
        dispatch_logger.bind(uid=uid, session_id=session_id).error(t("LOG_DISPATCHER_ERROR"), exc_info=True)
        raise ServerException(message=ERR_INTERNAL_SERVER_ERROR, cause=str(e))
