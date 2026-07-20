import asyncio
import json
import socket
import time
import uuid
from collections.abc import Awaitable, Callable, MutableSet
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.audit.service import audit_tool_round
from app.core.channel_router import select_channel
from app.core.constants import (
    ERR_AUDIT_EXECUTION_CLAIM_FAILED,
    ERR_CHAT_CHANNEL_NOT_FOUND,
    ERR_INTERNAL_SERVER_ERROR,
    ERR_LLM_EMPTY_RESPONSE,
    ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN,
    ERR_TOOL_ROUND_PRECHECK_FAILED,
    SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY,
)
from app.core.context import ContextManager
from app.core.crud.audit import audit_crud
from app.core.crud.profile import profile_crud
from app.core.crud.user import user_crud
from app.core.exceptions import ApiKeyException, BaseBusinessException, LLMException, ServerException
from app.core.i18n import get_current_locale, t
from app.core.log import channel_log_extra, get_logger
from app.core.prompts import PROMPT_MAX_TURNS_REACHED
from app.core.tools import get_tools_for_profile
from app.core.tools.send_file_to_user import sanitize_files_to_user_result
from app.core.utils.assistant_files import build_assistant_files_content as build_assistant_content
from app.core.utils.background_task_result import sanitize_execution_summary
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.context_summary.common import (
    ContextSummaryWorkValidityChecker,
)
from app.core.utils.dispatcher.append_new_user_messages import append_new_user_messages
from app.core.utils.dispatcher.context_summary_checkpoint import apply_context_summary_checkpoint
from app.core.utils.dispatcher.fetch_and_merge_new_user_messages import fetch_and_merge_new_user_messages
from app.core.utils.dispatcher.handle_parallel_tool_limit import handle_parallel_tool_limit
from app.core.utils.dispatcher.helpers import (
    dump_output_history,
    extract_files_to_user,
    format_exception_message,
    get_multimodal_from_entry,
    process_single_tool_with_isolated_db,
    reassemble_multimodal_messages,
    resolve_chat_params,
)
from app.core.utils.dispatcher.mark_initial_message_processed import mark_initial_message_processed
from app.core.utils.dispatcher.markdown_instruction import append_environment_prompt_instruction, build_max_output_tokens_instruction, materialize_latest_user_environment_prompt
from app.core.utils.dispatcher.prepare_messages import prepare_messages
from app.core.utils.dispatcher.process_single_tool import get_queued_background_task_id, prevalidate_tool_round
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_initial_message import save_initial_message
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.core.utils.message_assembler import MessageAssembler
from app.core.utils.time import get_local_time
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.message import InternalMessage, MessageRole, MessageType
from app.providers.llm.client import LLMClient
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse

logger = get_logger(__name__)


class AuditExecutionStatePersistenceError(ServerException):
    def __init__(self, cause: str) -> None:
        super().__init__(message=ERR_AUDIT_EXECUTION_CLAIM_FAILED, cause=cause)


def _tool_result_succeeded(content: str | None) -> bool:
    try:
        payload = json.loads(content or "{}")
    except (TypeError, ValueError):
        return True
    if not isinstance(payload, dict):
        return True
    return not (payload.get("error") or payload.get("status") == "failed" or (isinstance(payload.get("exit_code"), int) and payload["exit_code"] != 0))


class InteractiveDispatcherMixin:
    @classmethod
    async def _dispatch_interactive(
        cls,
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
        additional_user_messages_fetcher: Callable[[], Awaitable[list[InternalMessage]]] | None = None,
        execution_resume_state: dict[str, Any] | None = None,
        execution_checkpoint_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        context_summary_work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
        expose_tool_call_content: bool = True,
        dispatcher_mode: Literal["non_stream", "stream"] = "non_stream",
    ):
        try:
            dispatch_logger = logger if dispatcher_mode == "non_stream" else get_logger("app.core.dispatchers.stream")
            user = await user_crud.get_by_uid(db, uid)
            username = user.username if user else "Unknown"
            profile = await profile_crud.get_with_relations(db, persisted_profile_id) if persisted_profile_id is not None else await profile_crud.get_active(db, uid=uid)

            if execution_resume_state is None:
                dispatch_logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_USER_MESSAGE", username=username, message=message, attachments=str(attachments)))

            await cls.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)

            # 1. 初始保存消息；队列消费者可传入已经冻结并持久化的输入边界
            initial_msg = persisted_initial_message or await save_initial_message(db, session_id, uid, profile, message, attachments, source=session_source)

            queue_managed = persisted_initial_message is not None

            async def fetch_additional_user_messages(max_tokens: int) -> list[InternalMessage]:
                if additional_user_messages_fetcher is not None:
                    new_messages = await additional_user_messages_fetcher()
                elif queue_managed:
                    return []
                else:
                    new_messages = await fetch_and_merge_new_user_messages(db, session_id, uid)
                max_tokens_instruction = build_max_output_tokens_instruction(max_tokens)
                for new_message in new_messages:
                    append_environment_prompt_instruction(new_message, max_tokens_instruction)
                return new_messages

            final_ai_content = ""
            turn_messages = [InternalMessage.model_validate(item) for item in execution_resume_state.get("turn_messages", [])] if execution_resume_state else []
            files_to_user = list(execution_resume_state.get("files_to_user", [])) if execution_resume_state else []
            is_first_iter = execution_resume_state is None
            checkpoint_mode = ContextSummaryTriggerMode.USER_MESSAGE
            checkpoint_upper_id = min(frozen_user_message_ids) if frozen_user_message_ids else initial_msg.id
            if execution_resume_state is not None:
                saved_checkpoint_mode = execution_resume_state.get("context_summary_trigger_mode")
                saved_checkpoint_upper_id = execution_resume_state.get("context_summary_fixed_upper_message_id")
                if saved_checkpoint_mode in ContextSummaryTriggerMode._value2member_map_:
                    checkpoint_mode = ContextSummaryTriggerMode(saved_checkpoint_mode)
                if isinstance(saved_checkpoint_upper_id, int):
                    checkpoint_upper_id = saved_checkpoint_upper_id

            async def save_execution_checkpoint(
                messages: list[InternalMessage],
                current_turn: int,
                *,
                active_audit_execution: dict[str, Any] | None = None,
                update_active_audit_execution: bool = False,
            ) -> None:
                if execution_checkpoint_callback is None:
                    return
                checkpoint = {
                    "messages": [item.model_dump(mode="json") for item in messages],
                    "turn_messages": [item.model_dump(mode="json") for item in turn_messages],
                    "files_to_user": files_to_user,
                    "current_turn": current_turn,
                    "context_summary_trigger_mode": checkpoint_mode.value,
                    "context_summary_fixed_upper_message_id": checkpoint_upper_id,
                }
                if update_active_audit_execution:
                    checkpoint[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = active_audit_execution
                await execution_checkpoint_callback(checkpoint)

            async def mark_claimed_audit_execution_unknown(audit_record_id: int, claim_token: str) -> None:
                await audit_crud.mark_execution_unknown(
                    db,
                    audit_record_id=audit_record_id,
                    claim_token=claim_token,
                    error_reason=t(ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN),
                )
                await update_confirmation_message_status(db, audit_record_id=audit_record_id)

            while True:
                try:
                    cfg = await validate_profile_and_cfg(db, profile)

                    if is_first_iter:
                        await mark_initial_message_processed(db, initial_msg.id)

                    # ========== 渠道路由选择 ==========
                    chat_channel = cfg.channel.chat_channel
                    chat_cursor_key = f"{profile.id}:CHAT"
                    selection = await select_channel(db, chat_channel, "CHAT", call_context=f"chat_dispatch_{dispatcher_mode}", cursor_key=chat_cursor_key)
                    if not selection:
                        raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

                    chat_channel_obj, model_entry, _channel_rule = selection
                    img_understanding, audio_understanding, video_understanding = get_multimodal_from_entry(model_entry)
                    chat_params = resolve_chat_params(model_entry, chat_channel)
                    tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile)

                    if execution_resume_state is not None:
                        messages = [InternalMessage.model_validate(item) for item in execution_resume_state.get("messages", [])]
                        current_turn = int(execution_resume_state.get("current_turn", 0))
                        execution_resume_state = None
                    else:
                        messages = await prepare_messages(
                            db,
                            session_id,
                            uid,
                            profile,
                            cfg,
                            initial_msg,
                            message,
                            is_first_iter,
                            context_window_k=chat_params["context_window_k"],
                            max_tokens=chat_params["max_tokens"],
                            tools=tools,
                            history_before_id=history_before_id,
                        )
                        current_turn = 0

                    # 重新组装带附件的多模态消息（使用模型实际的多模态能力）
                    for idx, m in enumerate(messages):
                        if m.role == MessageRole.USER and (m.attachments or isinstance(m.content, list)):
                            is_history = idx != len(messages) - 1
                            messages[idx] = MessageAssembler.assemble(
                                m,
                                image_understanding=img_understanding,
                                audio_understanding=audio_understanding,
                                video_understanding=video_understanding,
                                is_history=is_history,
                            )

                    max_turns = cfg.tool.max_turns

                    while current_turn <= max_turns:
                        new_user_msgs = await fetch_additional_user_messages(chat_params["max_tokens"])
                        if new_user_msgs:
                            current_turn = 0
                            append_new_user_messages(cfg, messages, new_user_msgs, img_understanding, audio_understanding, video_understanding)
                            fixed_user_upper_id = max(
                                (item.id for item in new_user_msgs if item.id is not None),
                                default=None,
                            )
                            if fixed_user_upper_id is not None:
                                checkpoint_mode = ContextSummaryTriggerMode.USER_MESSAGE
                                checkpoint_upper_id = fixed_user_upper_id

                        current_turn += 1

                        if current_turn == max_turns:
                            summary_notice = PROMPT_MAX_TURNS_REACHED.format(max_turns=max_turns)
                            notice_msg = InternalMessage(role=MessageRole.USER, content=summary_notice)
                            messages.append(notice_msg)
                            current_tools = None
                        else:
                            current_tools = tools

                        response_id = str(uuid.uuid4())
                        excluded_priorities: set[int] = set()
                        while True:
                            emitted_stream_content = False
                            buffered_content_chunks: list[str] = []
                            try:
                                if checkpoint_upper_id is not None:
                                    messages = await apply_context_summary_checkpoint(
                                        db,
                                        session_id=session_id,
                                        uid=uid,
                                        profile=profile,
                                        cfg=cfg,
                                        messages=messages,
                                        trigger_mode=checkpoint_mode,
                                        fixed_upper_message_id=checkpoint_upper_id,
                                        context_window_k=chat_params["context_window_k"],
                                        max_tokens=chat_params["max_tokens"],
                                        tools=current_tools,
                                        work_validity_checker=context_summary_work_validity_checker,
                                        lifecycle_event_callback=context_summary_lifecycle_callback,
                                    )
                                request_messages = ContextManager.trim_messages_for_model_request(
                                    messages=await materialize_latest_user_environment_prompt(
                                        db,
                                        session_id,
                                        messages,
                                        chat_params["max_tokens"],
                                    ),
                                    uid=uid,
                                    session_id=session_id,
                                    context_window_k=chat_params["context_window_k"],
                                    max_tokens=chat_params["max_tokens"],
                                    tools=current_tools,
                                )
                                generation_kwargs = {
                                    "api_key": chat_channel_obj.get_decrypted_api_key(),
                                    "base_url": chat_channel_obj.base_url,
                                    "model_id": model_entry["model_id"],
                                    "messages": request_messages,
                                    "temperature": chat_params["temperature"],
                                    "top_p": chat_params["top_p"],
                                    "max_tokens": chat_params["max_tokens"],
                                    "tools": current_tools,
                                    "protocol": getattr(chat_channel_obj, "protocol", "openai"),
                                    "timeout": chat_params["chat_timeout"],
                                }
                                await db.commit()
                                attempt_started_at = get_local_time()
                                if stream_event_callback is None:
                                    response = await LLMClient.generate(**generation_kwargs)
                                else:

                                    async def on_content(content: str) -> None:
                                        nonlocal emitted_stream_content
                                        if not expose_tool_call_content:
                                            buffered_content_chunks.append(content)
                                        else:
                                            await stream_event_callback(
                                                {
                                                    "type": "content",
                                                    "content": content,
                                                    "turn": current_turn,
                                                    "response_id": response_id,
                                                }
                                            )
                                            emitted_stream_content = True

                                    response = await LLMClient.generate_with_stream_callback(
                                        **generation_kwargs,
                                        on_content=on_content,
                                    )
                                # 空响应（无内容且无工具调用）也视为渠道异常，纳入降级重试
                                ai_msg = response.message
                                if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                                    raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
                                if not expose_tool_call_content and not ai_msg.tool_calls:
                                    for content_chunk in buffered_content_chunks:
                                        await stream_event_callback(
                                            {
                                                "type": "content",
                                                "content": content_chunk,
                                                "turn": current_turn,
                                                "response_id": response_id,
                                            }
                                        )
                                break
                            except ApiKeyException:
                                raise
                            except LLMException as exc:
                                # 已推送内容后不可切换渠道重试，否则前端会拼接两个渠道的部分回复
                                if emitted_stream_content:
                                    raise
                                # 仅捕获 LLM 调用相关异常（连接失败/超时/状态码错误/空响应等）做降级，
                                # 组装、协议转换或代码缺陷类异常向上抛出，避免掩盖真实问题
                                excluded_priorities.add(_channel_rule.priority)
                                dispatch_logger.bind(
                                    uid=uid,
                                    session_id=session_id,
                                    **channel_log_extra(chat_channel_obj, model_entry),
                                ).warning(t("LOG_DISPATCHER_NON_STREAM_CHANNEL_FAILED", error=format_exception_message(exc)))
                                selection = await select_channel(db, chat_channel, "CHAT", call_context=f"chat_dispatch_{dispatcher_mode}_retry", excluded_priorities=excluded_priorities, cursor_key=chat_cursor_key)
                                if not selection:
                                    raise
                                chat_channel_obj, model_entry, _channel_rule = selection
                                img_understanding, audio_understanding, video_understanding = get_multimodal_from_entry(model_entry)
                                chat_params = resolve_chat_params(model_entry, chat_channel)
                                # 保留当前工具调用过程，仅按新渠道的多模态能力重新组装；
                                # 下一次请求会使用新渠道预算裁剪同一份完整消息列表。
                                reassemble_multimodal_messages(messages, img_understanding, audio_understanding, video_understanding)

                        if not ai_msg.tool_calls and files_to_user:
                            ai_msg.content = build_assistant_content(ai_msg.content, files_to_user)

                        dispatch_logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_LLM_RESPONSE", username=username, turn=current_turn, content=ai_msg.content or "[工具调用]"))

                        messages.append(ai_msg)
                        turn_messages.append(ai_msg)

                        new_user_msgs = await fetch_additional_user_messages(chat_params["max_tokens"]) if not ai_msg.tool_calls else []
                        saved_msg = await save_assistant_message(
                            db,
                            session_id,
                            uid,
                            profile.id,
                            ai_msg,
                            dedupe_key=final_message_dedupe_key if not ai_msg.tool_calls and not new_user_msgs else None,
                            created_at=attempt_started_at if stream_event_callback is not None else None,
                        )
                        if stream_event_callback is not None and saved_msg is not None:
                            turn_end_content = saved_msg.content
                            if ai_msg.tool_calls:
                                turn_end_content = ai_msg.content if expose_tool_call_content else None
                            await stream_event_callback(
                                {
                                    "type": "turn_end",
                                    "response_id": response_id,
                                    "content": turn_end_content,
                                }
                            )

                        if not ai_msg.tool_calls:
                            final_ai_content = ai_msg.content
                            if not new_user_msgs:
                                break

                            dispatch_logger.bind(uid=uid, session_id=session_id).info(t("LOG_DISPATCHER_NON_STREAM_RESPONSE_CONTINUE"))
                            append_new_user_messages(cfg, messages, new_user_msgs, img_understanding, audio_understanding, video_understanding)
                            fixed_user_upper_id = max(
                                (item.id for item in new_user_msgs if item.id is not None),
                                default=None,
                            )
                            if fixed_user_upper_id is not None:
                                checkpoint_mode = ContextSummaryTriggerMode.USER_MESSAGE
                                checkpoint_upper_id = fixed_user_upper_id

                            current_turn = 0
                            await save_execution_checkpoint(messages, current_turn)
                            continue

                        if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
                            fixed_tool_upper_id = await handle_parallel_tool_limit(db, session_id, uid, profile, cfg, ai_msg, messages, turn_messages)
                            if fixed_tool_upper_id is not None:
                                checkpoint_mode = ContextSummaryTriggerMode.TOOL_RESULT
                                checkpoint_upper_id = fixed_tool_upper_id
                            await save_execution_checkpoint(messages, current_turn)
                            continue

                        precheck_errors = prevalidate_tool_round(ai_msg.tool_calls, cfg, tool_schemas=tools)
                        if precheck_errors:
                            persisted_tool_result_ids = []
                            for tool_call in ai_msg.tool_calls:
                                content = precheck_errors.get(tool_call.id)
                                if content is None:
                                    content = json.dumps(
                                        {
                                            "status": "failed",
                                            "tool_name": tool_call.name,
                                            "error": t(ERR_TOOL_ROUND_PRECHECK_FAILED),
                                        },
                                        ensure_ascii=False,
                                    )
                                tool_result = InternalMessage(
                                    role=MessageRole.TOOL,
                                    tool_call_id=tool_call.id,
                                    content=content,
                                )
                                stored_tool_result = await save_tool_response(
                                    db,
                                    session_id,
                                    uid,
                                    profile.id,
                                    tool_result,
                                    messages,
                                    turn_messages,
                                )
                                if stored_tool_result.id is not None:
                                    persisted_tool_result_ids.append(stored_tool_result.id)
                            if persisted_tool_result_ids:
                                checkpoint_mode = ContextSummaryTriggerMode.TOOL_RESULT
                                checkpoint_upper_id = max(persisted_tool_result_ids)
                            await save_execution_checkpoint(messages, current_turn)
                            continue

                        if stream_event_callback is not None:
                            for tool_call in ai_msg.tool_calls:
                                await stream_event_callback(
                                    {
                                        "type": "tool_start",
                                        "name": tool_call.name,
                                        "arguments": tool_call.arguments,
                                        "tool_call_id": tool_call.id,
                                        "response_id": response_id,
                                    }
                                )

                        audit_round = await audit_tool_round(
                            db,
                            cfg=cfg,
                            tool_calls=ai_msg.tool_calls,
                            source_assistant_message_id=saved_msg.id,
                            uid=uid,
                            operator_username=username,
                            session_id=session_id,
                            source=session_source,
                            language=get_current_locale(),
                        )
                        if audit_round is not None and not audit_round.may_execute:
                            persisted_tool_result_ids = []
                            for tool_result in audit_round.tool_results:
                                stored_tool_result = await save_tool_response(db, session_id, uid, profile.id, tool_result, messages, turn_messages)
                                if stored_tool_result.id is not None:
                                    persisted_tool_result_ids.append(stored_tool_result.id)
                                if stream_event_callback is not None:
                                    tool_call = next((item for item in ai_msg.tool_calls if item.id == tool_result.tool_call_id), None)
                                    await stream_event_callback(
                                        {
                                            "type": "tool_end",
                                            "name": tool_call.name if tool_call else "unknown",
                                            "result": sanitize_files_to_user_result(tool_result.content),
                                            "tool_call_id": tool_result.tool_call_id,
                                            "response_id": response_id,
                                        }
                                    )
                            if audit_round.confirmation_payload is not None:
                                confirmation_content = json.dumps(audit_round.confirmation_payload, ensure_ascii=False)
                                await save_message(
                                    db,
                                    session_id,
                                    uid,
                                    MessageRole.ASSISTANT,
                                    MessageType.AUDIT_CONFIRMATION,
                                    audit_round.confirmation_payload,
                                    profile.id,
                                    is_processed=True,
                                    dedupe_key=final_message_dedupe_key,
                                )
                                await update_confirmation_message_status(db, audit_record_id=audit_round.audit_record_id)
                                final_ai_content = confirmation_content
                                return LLMResponse(
                                    choices=[LLMChoice(message=LLMChoiceMessage(role=MessageRole.ASSISTANT, content=confirmation_content), finish_reason=True, created_at=time.time())],
                                    history=dump_output_history(turn_messages),
                                    files=files_to_user or None,
                                ).model_dump()
                            if persisted_tool_result_ids:
                                checkpoint_mode = ContextSummaryTriggerMode.TOOL_RESULT
                                checkpoint_upper_id = max(persisted_tool_result_ids)
                            await save_execution_checkpoint(messages, current_turn)
                            continue

                        audit_claim_token = None
                        audit_execution_ids: dict[str, int] = {}
                        audit_execution_checkpoint_state: dict[str, Any] | None = None
                        audit_all_succeeded = True
                        if audit_round is not None:
                            claimed_record = None
                            try:
                                claimed_record, audit_claim_token = await audit_crud.claim_passed_for_execution(
                                    db,
                                    audit_record_id=audit_round.audit_record_id,
                                )
                                if claimed_record is not None and audit_claim_token is not None:
                                    audit_details = await audit_crud.list_tool_details(db, audit_round.audit_record_id)
                                    detail_by_call_id = {detail.original_tool_call_id: detail for detail in audit_details}
                                    for tool_call in ai_msg.tool_calls:
                                        detail = detail_by_call_id.get(tool_call.id)
                                        if detail is None:
                                            audit_claim_token = None
                                            break
                                        execution = await audit_crud.create_execution_attempt(
                                            db,
                                            audit_record_id=audit_round.audit_record_id,
                                            audit_tool_detail_id=detail.id,
                                            claim_token=audit_claim_token,
                                            execution_node=socket.gethostname(),
                                            new_tool_call_id=tool_call.id,
                                        )
                                        if execution is None:
                                            audit_claim_token = None
                                            break
                                        audit_execution_ids[tool_call.id] = execution.id
                                if audit_claim_token is None or len(audit_execution_ids) != len(ai_msg.tool_calls):
                                    for execution_id in audit_execution_ids.values():
                                        await audit_crud.finish_execution_attempt(
                                            db,
                                            execution_record_id=execution_id,
                                            status=AuditExecutionStatus.CANCELLED,
                                            error=t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
                                        )
                                    if claimed_record is not None and claimed_record.execution_claim_token:
                                        await audit_crud.finish_execution_round(
                                            db,
                                            audit_record_id=audit_round.audit_record_id,
                                            claim_token=claimed_record.execution_claim_token,
                                            status=AuditRecordStatus.FAILED,
                                            error_reason=t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
                                        )
                                        await update_confirmation_message_status(db, audit_record_id=audit_round.audit_record_id)
                                    persisted_tool_result_ids = []
                                    for tool_call in ai_msg.tool_calls:
                                        tool_result = InternalMessage(
                                            role=MessageRole.TOOL,
                                            tool_call_id=tool_call.id,
                                            content=json.dumps(
                                                {
                                                    "status": "failed",
                                                    "tool_name": tool_call.name,
                                                    "error": t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
                                                },
                                                ensure_ascii=False,
                                            ),
                                        )
                                        stored_tool_result = await save_tool_response(
                                            db,
                                            session_id,
                                            uid,
                                            profile.id,
                                            tool_result,
                                            messages,
                                            turn_messages,
                                        )
                                        if stored_tool_result.id is not None:
                                            persisted_tool_result_ids.append(stored_tool_result.id)
                                        if stream_event_callback is not None:
                                            await stream_event_callback(
                                                {
                                                    "type": "tool_end",
                                                    "name": tool_call.name,
                                                    "result": sanitize_files_to_user_result(tool_result.content),
                                                    "tool_call_id": tool_call.id,
                                                    "response_id": response_id,
                                                }
                                            )
                                    if persisted_tool_result_ids:
                                        checkpoint_mode = ContextSummaryTriggerMode.TOOL_RESULT
                                        checkpoint_upper_id = max(persisted_tool_result_ids)
                                    await save_execution_checkpoint(messages, current_turn)
                                    continue

                                if execution_checkpoint_callback is not None:
                                    audit_execution_checkpoint_state = {
                                        "audit_record_id": audit_round.audit_record_id,
                                        "claim_token": audit_claim_token,
                                    }
                                    await save_execution_checkpoint(
                                        messages,
                                        current_turn,
                                        active_audit_execution=audit_execution_checkpoint_state,
                                        update_active_audit_execution=True,
                                    )
                            except asyncio.CancelledError:
                                if audit_claim_token is not None:
                                    await mark_claimed_audit_execution_unknown(audit_round.audit_record_id, audit_claim_token)
                                    raise AuditExecutionStatePersistenceError(cause=t(ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN))
                                raise
                            except Exception as exc:
                                if audit_claim_token is not None:
                                    await mark_claimed_audit_execution_unknown(audit_round.audit_record_id, audit_claim_token)
                                    raise AuditExecutionStatePersistenceError(cause=str(exc)) from exc
                                raise

                        # 在任何工具开始前持久化完整调用意图。恢复时未落库的工具结果会被视为
                        # 执行状态未知并交给模型核实，而不会重新执行原工具。
                        await save_execution_checkpoint(messages, current_turn)

                        sem = asyncio.Semaphore(cfg.tool.executor_max_workers)

                        async def wrapped_tool_call(tc):
                            async with sem:
                                task = asyncio.create_task(
                                    process_single_tool_with_isolated_db(
                                        tc,
                                        profile,
                                        cfg,
                                        messages,
                                        username,
                                        session_id,
                                        current_turn,
                                        uid,
                                        allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                                        context_window_k=chat_params["context_window_k"],
                                    )
                                )

                                if active_tasks is not None:
                                    active_tasks.add(task)

                                try:
                                    return await task
                                finally:
                                    if active_tasks is not None:
                                        active_tasks.discard(task)

                        tasks = [asyncio.create_task(wrapped_tool_call(tc)) for tc in ai_msg.tool_calls]
                        persisted_tool_result_ids: list[int] = []
                        try:
                            for completed_task in asyncio.as_completed(tasks):
                                tool_res = await completed_task
                                if audit_claim_token is not None:
                                    execution_id = audit_execution_ids[tool_res.tool_call_id]
                                    queued_task_id = get_queued_background_task_id(tool_res.content)
                                    if queued_task_id is None:
                                        execution_succeeded = _tool_result_succeeded(tool_res.content)
                                        audit_all_succeeded = audit_all_succeeded and execution_succeeded
                                        await audit_crud.finish_execution_attempt(
                                            db,
                                            execution_record_id=execution_id,
                                            status=AuditExecutionStatus.SUCCEEDED if execution_succeeded else AuditExecutionStatus.FAILED,
                                            result_summary=sanitize_execution_summary(tool_res.content, redact_text=True),
                                            error=None if execution_succeeded else sanitize_execution_summary(tool_res.content, redact_text=True),
                                        )
                                    elif audit_execution_checkpoint_state is not None:
                                        audit_execution_checkpoint_state["handoff_state"] = "persisted"
                                        task_ids = audit_execution_checkpoint_state.setdefault("background_task_ids", [])
                                        if queued_task_id not in task_ids:
                                            task_ids.append(queued_task_id)
                                files_to_user.extend(extract_files_to_user([tool_res]))
                                stored_tool_res = await save_tool_response(db, session_id, uid, profile.id, tool_res, messages, turn_messages)
                                if stored_tool_res.id is not None:
                                    persisted_tool_result_ids.append(stored_tool_res.id)
                                if audit_execution_checkpoint_state is not None and get_queued_background_task_id(tool_res.content) is not None:
                                    await save_execution_checkpoint(
                                        messages,
                                        current_turn,
                                        active_audit_execution=audit_execution_checkpoint_state,
                                        update_active_audit_execution=True,
                                    )
                                else:
                                    await save_execution_checkpoint(messages, current_turn)
                                if stream_event_callback is not None:
                                    tool_call = next((item for item in ai_msg.tool_calls if item.id == tool_res.tool_call_id), None)
                                    await stream_event_callback(
                                        {
                                            "type": "tool_end",
                                            "name": tool_call.name if tool_call else "unknown",
                                            "result": sanitize_files_to_user_result(tool_res.content),
                                            "tool_call_id": tool_res.tool_call_id,
                                            "response_id": response_id,
                                        }
                                    )
                        finally:
                            for task in tasks:
                                if not task.done():
                                    task.cancel()
                            await asyncio.gather(*tasks, return_exceptions=True)

                        if audit_round is not None and audit_claim_token is not None:
                            if hasattr(db, "execute"):
                                execution_round_status = await audit_crud.finish_execution_round_if_complete(
                                    db,
                                    audit_record_id=audit_round.audit_record_id,
                                    claim_token=audit_claim_token,
                                )
                            else:
                                legacy_round_finished = await audit_crud.finish_execution_round(
                                    db,
                                    audit_record_id=audit_round.audit_record_id,
                                    claim_token=audit_claim_token,
                                    status=AuditRecordStatus.SUCCEEDED if audit_all_succeeded else AuditRecordStatus.FAILED,
                                    error_reason=None if audit_all_succeeded else t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
                                )
                                if not legacy_round_finished:
                                    raise AuditExecutionStatePersistenceError(cause=t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
                                execution_round_status = AuditRecordStatus.SUCCEEDED if audit_all_succeeded else AuditRecordStatus.FAILED
                            if execution_round_status is not None and execution_checkpoint_callback is not None:
                                await save_execution_checkpoint(
                                    messages,
                                    current_turn,
                                    active_audit_execution=None,
                                    update_active_audit_execution=True,
                                )
                            if execution_round_status is not None:
                                await update_confirmation_message_status(db, audit_record_id=audit_round.audit_record_id)

                        if persisted_tool_result_ids:
                            checkpoint_mode = ContextSummaryTriggerMode.TOOL_RESULT
                            checkpoint_upper_id = max(persisted_tool_result_ids)
                            await save_execution_checkpoint(messages, current_turn)

                finally:
                    is_first_iter = False

                if queue_managed:
                    break
                new_user_msgs = await fetch_additional_user_messages(chat_params["max_tokens"])
                if not new_user_msgs:
                    break

            return LLMResponse(
                choices=[LLMChoice(message=LLMChoiceMessage(role=MessageRole.ASSISTANT, content=final_ai_content), finish_reason=True, created_at=time.time())],
                history=dump_output_history(
                    turn_messages,
                    expose_tool_call_content=expose_tool_call_content,
                ),
                files=files_to_user or None,
            ).model_dump()

        except BaseBusinessException:
            raise
        except Exception as e:
            dispatch_logger.bind(uid=uid, session_id=session_id).error(t("LOG_DISPATCHER_ERROR"), exc_info=True)
            raise ServerException(message=ERR_INTERNAL_SERVER_ERROR, cause=str(e))
