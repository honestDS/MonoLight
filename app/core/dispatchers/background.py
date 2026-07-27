import asyncio
import json
import socket
from collections.abc import Awaitable, Callable
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import persist_pending_confirmation_bundle, update_confirmation_message_status
from app.core.audit.service import audit_tool_round
from app.core.constants import (
    ERR_AUDIT_EXECUTION_CLAIM_FAILED,
    ERR_BACKGROUND_FINAL_REPLY_TOOL_CALL_FORBIDDEN,
    ERR_BACKGROUND_TASK_NOT_FOUND,
    ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE,
    ERR_BACKGROUND_TOO_MANY_TOOL_CALLS,
    ERR_LLM_EMPTY_RESPONSE,
    ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN,
    ERR_TOOL_ROUND_PRECHECK_FAILED,
    MSG_BACKGROUND_FINAL_REPLY_FALLBACK_WITH_FILES,
    MSG_BACKGROUND_FINAL_REPLY_FALLBACK_WITHOUT_FILES,
)
from app.core.context import ContextManager
from app.core.crud.audit import audit_crud
from app.core.crud.background_task import background_task_crud
from app.core.crud.profile import profile_crud
from app.core.crud.user import user_crud
from app.core.exceptions import LLMException, ServerException
from app.core.i18n import get_current_locale, t
from app.core.log import get_logger
from app.core.prompts import (
    BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT,
    BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT,
    BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_FALLBACK_PROMPT,
)
from app.core.tools import get_tools_for_profile
from app.core.utils.assistant_files import build_assistant_files_content, parse_assistant_files_content
from app.core.utils.background_task_result import sanitize_execution_summary
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.channel_call import generate_chat_with_fallback
from app.core.utils.dispatcher.context_summary_checkpoint import apply_context_summary_checkpoint
from app.core.utils.dispatcher.helpers import (
    BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES,
    dump_background_proactive_history,
    extract_files_to_user,
    filter_background_proactive_tools,
    get_unsupported_background_proactive_tool_names,
    process_single_tool_with_isolated_db,
    validate_background_proactive_tool_calls,
)
from app.core.utils.dispatcher.inject_system_prompt import build_system_prompt, inject_system_prompt_text
from app.core.utils.dispatcher.markdown_instruction import materialize_latest_user_environment_prompt
from app.core.utils.dispatcher.prepare_messages import prepare_messages
from app.core.utils.dispatcher.process_single_tool import prevalidate_tool_round
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.message import InternalMessage, MessageRole
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)


def _tool_result_succeeded(content: str | None) -> bool:
    try:
        payload = json.loads(content or "{}")
    except (TypeError, ValueError):
        return True
    if not isinstance(payload, dict):
        return True
    return not (payload.get("error") or payload.get("status") == "failed" or (isinstance(payload.get("exit_code"), int) and payload["exit_code"] != 0))


class BackgroundDispatcherMixin:
    @staticmethod
    def _build_virtual_tool_feedback_messages(ai_msg: InternalMessage, payload: dict[str, Any]) -> list[InternalMessage]:
        if not ai_msg.tool_calls:
            return []

        feedback_messages = [ai_msg]
        for tool_call in ai_msg.tool_calls:
            feedback_payload = {
                **payload,
                "tool_call": {
                    "id": tool_call.id,
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                },
            }
            feedback_messages.append(
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=tool_call.id,
                    content=json.dumps(feedback_payload, ensure_ascii=False),
                )
            )
        return feedback_messages

    @staticmethod
    async def _build_final_correction_request(
        retry_chat_params,
        *,
        db: AsyncSession,
        messages: list[InternalMessage],
        uid: str,
        session_id: str,
    ) -> list[InternalMessage]:
        return ContextManager.trim_messages_for_model_request(
            messages=await materialize_latest_user_environment_prompt(
                db,
                session_id,
                messages,
                retry_chat_params["max_tokens"],
            ),
            uid=uid,
            session_id=session_id,
            context_window_k=retry_chat_params["context_window_k"],
            max_tokens=retry_chat_params["max_tokens"],
            tools=None,
        )

    @classmethod
    async def _generate_reply_from_history(
        cls,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile,
        call_context: str,
        allow_tools: bool,
        extra_messages: list[InternalMessage] | None = None,
        submission_context: list[InternalMessage] | None = None,
        additional_system_prompt: str | None = None,
        persist_response: bool = True,
        initial_trigger_mode: ContextSummaryTriggerMode | None = None,
        initial_fixed_upper_message_id: int | None = None,
        restrict_tools_to_background_allowlist: bool = True,
        reply_source: str = "background_task",
        final_message_dedupe_key: str | None = None,
        audit_execution_binding_callback: Callable[[dict[str, Any] | None], Awaitable[None]] | None = None,
        request_metadata_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    ) -> tuple[InternalMessage, list[InternalMessage], list[dict[str, Any]]]:
        """根据历史生成后台回复，并持久化可能执行工具的审计绑定。"""
        user = await user_crud.get_by_uid(db, uid)
        username = user.username if user else "Unknown"
        cfg = await validate_profile_and_cfg(db, profile)
        chat_channel = cfg.channel.chat_channel
        chat_cursor_key = f"{profile.id}:CHAT"
        messages: list[InternalMessage] = []
        cleaned_additional_system_prompt = additional_system_prompt.strip() if isinstance(additional_system_prompt, str) else ""

        tools = None
        allowed_knowledge_base_ids = None
        allowed_tool_names = None
        if allow_tools:
            profile_tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile, allow_background=False)
            if restrict_tools_to_background_allowlist:
                profile_tools = filter_background_proactive_tools(profile_tools)
                allowed_tool_names = BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES
            else:
                allowed_tool_names = {tool["function"]["name"] for tool in profile_tools if isinstance(tool.get("function", {}).get("name"), str)}
            tools = profile_tools or None

        async def build_initial_request(chat_params):
            nonlocal messages
            if submission_context is None:
                messages = await prepare_messages(
                    db,
                    session_id,
                    uid,
                    profile,
                    cfg,
                    None,
                    "",
                    False,
                    context_window_k=chat_params["context_window_k"],
                    max_tokens=chat_params["max_tokens"],
                    tools=tools,
                    additional_system_prompt=cleaned_additional_system_prompt or None,
                )
            else:
                system_prompt = await build_system_prompt(db, profile)
                if cleaned_additional_system_prompt:
                    system_prompt = f"{system_prompt}\n\n{cleaned_additional_system_prompt}" if system_prompt.strip() else cleaned_additional_system_prompt
                messages = inject_system_prompt_text(
                    [message.model_copy(deep=True) for message in submission_context],
                    system_prompt,
                )
            if extra_messages:
                messages.extend(message.model_copy(deep=True) for message in extra_messages)
            if initial_trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE and isinstance(initial_fixed_upper_message_id, int) and not isinstance(initial_fixed_upper_message_id, bool) and initial_fixed_upper_message_id > 0:
                messages = await apply_context_summary_checkpoint(
                    db,
                    session_id=session_id,
                    uid=uid,
                    profile=profile,
                    cfg=cfg,
                    messages=messages,
                    trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
                    fixed_upper_message_id=initial_fixed_upper_message_id,
                    context_window_k=chat_params["context_window_k"],
                    max_tokens=chat_params["max_tokens"],
                    tools=tools,
                )
            return ContextManager.trim_messages_for_model_request(
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
                tools=tools,
            )

        logger.bind(uid=uid, session_id=session_id, reply_source=reply_source, allow_tools=allow_tools).info(t("LOG_PROACTIVE_REPLY_GENERATION_STARTED"))
        response, _chat_channel_obj, model_entry, _channel_rule, chat_params = await generate_chat_with_fallback(
            db,
            chat_channel=chat_channel,
            request_builder=build_initial_request,
            call_context=call_context,
            cursor_key=chat_cursor_key,
            uid=uid,
            session_id=session_id,
            tools=tools,
            request_metadata_callback=request_metadata_callback,
        )
        ai_msg = response.message

        if allow_tools and ai_msg.tool_calls:
            unsupported_tool_names = get_unsupported_background_proactive_tool_names(ai_msg.tool_calls, allowed_tool_names=allowed_tool_names)
            if unsupported_tool_names:
                logger.bind(uid=uid, session_id=session_id, reply_source=reply_source, unsupported_tools=unsupported_tool_names).warning(t("LOG_BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_RETRY"))
                correction_messages = cls._build_virtual_tool_feedback_messages(
                    ai_msg,
                    {
                        "type": "background_proactive_tool_correction",
                        "error": "Unsupported tool call in background proactive reply.",
                        "instruction": BACKGROUND_PROACTIVE_TOOL_CORRECTION_PROMPT,
                        "unsupported_tool_calls": unsupported_tool_names,
                        "allowed_tool_calls": sorted(allowed_tool_names or BACKGROUND_PROACTIVE_ALLOWED_TOOL_NAMES),
                    },
                )
                correction_context_messages = [*messages, *correction_messages]

                async def build_correction_request(retry_chat_params):
                    return ContextManager.trim_messages_for_model_request(
                        messages=await materialize_latest_user_environment_prompt(
                            db,
                            session_id,
                            correction_context_messages,
                            retry_chat_params["max_tokens"],
                        ),
                        uid=uid,
                        session_id=session_id,
                        context_window_k=retry_chat_params["context_window_k"],
                        max_tokens=retry_chat_params["max_tokens"],
                        tools=tools,
                    )

                retry_response, _chat_channel_obj, model_entry, _channel_rule, chat_params = await generate_chat_with_fallback(
                    db,
                    chat_channel=chat_channel,
                    request_builder=build_correction_request,
                    call_context=f"{call_context}_tool_correction",
                    cursor_key=chat_cursor_key,
                    uid=uid,
                    session_id=session_id,
                    tools=tools,
                    request_metadata_callback=request_metadata_callback,
                )
                ai_msg = retry_response.message
                if not ai_msg.tool_calls and not (ai_msg.content or "").strip():
                    raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)
                remaining_unsupported_tool_names = get_unsupported_background_proactive_tool_names(ai_msg.tool_calls or [], allowed_tool_names=allowed_tool_names)
                if remaining_unsupported_tool_names:
                    logger.bind(uid=uid, session_id=session_id, reply_source=reply_source, unsupported_tools=remaining_unsupported_tool_names).warning(t("LOG_BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_TEXT_ONLY"))
                    text_only_messages = cls._build_virtual_tool_feedback_messages(
                        ai_msg,
                        {
                            "type": "background_proactive_text_only_fallback",
                            "error": "Unsupported tool call in background proactive retry.",
                            "instruction": BACKGROUND_PROACTIVE_TEXT_ONLY_FALLBACK_PROMPT,
                            "unsupported_tool_calls": remaining_unsupported_tool_names,
                        },
                    )
                    text_only_context_messages = [*correction_context_messages, *text_only_messages]

                    async def build_text_only_request(retry_chat_params):
                        return ContextManager.trim_messages_for_model_request(
                            messages=await materialize_latest_user_environment_prompt(
                                db,
                                session_id,
                                text_only_context_messages,
                                retry_chat_params["max_tokens"],
                            ),
                            uid=uid,
                            session_id=session_id,
                            context_window_k=retry_chat_params["context_window_k"],
                            max_tokens=retry_chat_params["max_tokens"],
                            tools=None,
                        )

                    text_only_response, _chat_channel_obj, model_entry, _channel_rule, chat_params = await generate_chat_with_fallback(
                        db,
                        chat_channel=chat_channel,
                        request_builder=build_text_only_request,
                        call_context=f"{call_context}_text_only",
                        cursor_key=chat_cursor_key,
                        uid=uid,
                        session_id=session_id,
                        tools=None,
                        require_content=True,
                        request_metadata_callback=request_metadata_callback,
                    )
                    ai_msg = text_only_response.message
                    if ai_msg.tool_calls:
                        ai_msg = InternalMessage(role=MessageRole.ASSISTANT, content=BACKGROUND_PROACTIVE_UNSUPPORTED_TOOL_FALLBACK_PROMPT)
                    if not (ai_msg.content or "").strip():
                        raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

        safe_content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
        ai_msg.content = safe_content
        logger.bind(uid=uid, session_id=session_id, reply_source=reply_source).info(t("LOG_DISPATCHER_LLM_RESPONSE", username=username, turn=0, content=ai_msg.content or "[工具调用]"))
        messages.append(ai_msg)
        turn_messages = [ai_msg]
        if persist_response:
            await save_assistant_message(
                db,
                session_id,
                uid,
                profile.id,
                ai_msg,
                dedupe_key=final_message_dedupe_key if not ai_msg.tool_calls else None,
            )
        if not allow_tools or not ai_msg.tool_calls:
            return ai_msg, turn_messages, []

        validate_background_proactive_tool_calls(ai_msg.tool_calls, allowed_tool_names=allowed_tool_names)
        if len(ai_msg.tool_calls) > cfg.tool.max_parallel_tools:
            raise LLMException(message=ERR_BACKGROUND_TOO_MANY_TOOL_CALLS, count=len(ai_msg.tool_calls))

        audit_round = None
        audit_claim_token = None
        audit_execution_ids: dict[str, int] = {}
        precheck_errors = prevalidate_tool_round(
            ai_msg.tool_calls,
            cfg,
            allow_background_submission=False,
            tool_schemas=tools,
        )
        if precheck_errors:
            tool_responses = [
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=tool_call.id,
                    content=precheck_errors.get(tool_call.id)
                    or json.dumps(
                        {
                            "status": "failed",
                            "tool_name": tool_call.name,
                            "error": t(ERR_TOOL_ROUND_PRECHECK_FAILED),
                        },
                        ensure_ascii=False,
                    ),
                )
                for tool_call in ai_msg.tool_calls
            ]
        else:
            audit_round = await audit_tool_round(
                db,
                cfg=cfg,
                tool_calls=ai_msg.tool_calls,
                source_assistant_message_id=ai_msg.id,
                uid=uid,
                operator_username=username,
                session_id=session_id,
                source=reply_source,
                language=get_current_locale(),
            )
            if audit_round is not None and not audit_round.may_execute:
                tool_responses = list(audit_round.tool_results)
            else:
                claimed_record = None
                if audit_round is not None:
                    claimed_record, audit_claim_token = await audit_crud.claim_passed_for_execution(
                        db,
                        audit_record_id=audit_round.audit_record_id,
                    )
                    if claimed_record is not None and audit_claim_token is not None:
                        if audit_execution_binding_callback is not None:
                            try:
                                await audit_execution_binding_callback(
                                    {
                                        "audit_record_id": audit_round.audit_record_id,
                                        "claim_token": audit_claim_token,
                                    }
                                )
                            except asyncio.CancelledError:
                                await audit_crud.mark_execution_unknown(
                                    db,
                                    audit_record_id=audit_round.audit_record_id,
                                    claim_token=audit_claim_token,
                                    error_reason=t(ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN),
                                )
                                await update_confirmation_message_status(db, audit_record_id=audit_round.audit_record_id)
                                raise
                            except Exception:
                                await audit_crud.mark_execution_unknown(
                                    db,
                                    audit_record_id=audit_round.audit_record_id,
                                    claim_token=audit_claim_token,
                                    error_reason=t(ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN),
                                )
                                await update_confirmation_message_status(db, audit_record_id=audit_round.audit_record_id)
                                raise
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
                claim_failed = audit_round is not None and (audit_claim_token is None or len(audit_execution_ids) != len(ai_msg.tool_calls))
                if claim_failed:
                    claim_closed = True
                    for execution_id in audit_execution_ids.values():
                        await audit_crud.finish_execution_attempt(
                            db,
                            execution_record_id=execution_id,
                            status=AuditExecutionStatus.CANCELLED,
                            error=t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
                        )
                    if claimed_record is not None and claimed_record.execution_claim_token:
                        claim_closed = await audit_crud.finish_execution_round(
                            db,
                            audit_record_id=audit_round.audit_record_id,
                            claim_token=claimed_record.execution_claim_token,
                            status=AuditRecordStatus.FAILED,
                            error_reason=t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
                        )
                        if not claim_closed:
                            raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
                        await update_confirmation_message_status(db, audit_record_id=audit_round.audit_record_id)
                    if audit_execution_binding_callback is not None and claim_closed:
                        await audit_execution_binding_callback(None)
                    audit_claim_token = None
                    tool_responses = [
                        InternalMessage(
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
                        for tool_call in ai_msg.tool_calls
                    ]
                else:
                    tool_responses = await asyncio.gather(
                        *[
                            process_single_tool_with_isolated_db(
                                tool_call,
                                profile,
                                cfg,
                                messages,
                                username,
                                session_id,
                                0,
                                uid,
                                allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                                context_window_k=chat_params["context_window_k"],
                                allow_background_submission=False,
                            )
                            for tool_call in ai_msg.tool_calls
                        ]
                    )

        if audit_round is not None and audit_claim_token is not None:
            audit_all_succeeded = True
            for tool_response in tool_responses:
                execution_succeeded = _tool_result_succeeded(tool_response.content)
                audit_all_succeeded = audit_all_succeeded and execution_succeeded
                await audit_crud.finish_execution_attempt(
                    db,
                    execution_record_id=audit_execution_ids[tool_response.tool_call_id],
                    status=AuditExecutionStatus.SUCCEEDED if execution_succeeded else AuditExecutionStatus.FAILED,
                    result_summary=sanitize_execution_summary(tool_response.content, redact_text=True),
                    error=None if execution_succeeded else sanitize_execution_summary(tool_response.content, redact_text=True),
                )
            await audit_crud.finish_execution_round(
                db,
                audit_record_id=audit_round.audit_record_id,
                claim_token=audit_claim_token,
                status=AuditRecordStatus.SUCCEEDED if audit_all_succeeded else AuditRecordStatus.FAILED,
                error_reason=None if audit_all_succeeded else "一个或多个工具执行失败",
            )
            await update_confirmation_message_status(db, audit_record_id=audit_round.audit_record_id)
            if audit_execution_binding_callback is not None:
                await audit_execution_binding_callback(None)

        files_to_user = extract_files_to_user(tool_responses)
        confirmation_message = None
        if audit_round is not None and audit_round.confirmation_payload is not None:
            stored_tool_responses, confirmation_message = await persist_pending_confirmation_bundle(
                db,
                audit_record_id=audit_round.audit_record_id,
                uid=uid,
                session_id=session_id,
                profile_id=profile.id,
                tool_results=tool_responses,
                confirmation_payload=audit_round.confirmation_payload,
                dedupe_key=final_message_dedupe_key,
            )
            messages.extend(stored_tool_responses)
            turn_messages.extend(stored_tool_responses)
        else:
            for tool_response in tool_responses:
                await save_tool_response(db, session_id, uid, profile.id, tool_response, messages, turn_messages)

        if audit_round is not None and audit_round.confirmation_payload is not None:
            confirmation_content = json.dumps(audit_round.confirmation_payload, ensure_ascii=False)
            await update_confirmation_message_status(db, audit_record_id=audit_round.audit_record_id)
            if confirmation_message is None:
                confirmation_message = InternalMessage(role=MessageRole.ASSISTANT, content=confirmation_content)
            turn_messages.append(confirmation_message)
            return confirmation_message, turn_messages, []

        async def build_final_request(final_chat_params):
            nonlocal messages
            if initial_trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE and isinstance(initial_fixed_upper_message_id, int) and not isinstance(initial_fixed_upper_message_id, bool) and initial_fixed_upper_message_id > 0:
                messages = await apply_context_summary_checkpoint(
                    db,
                    session_id=session_id,
                    uid=uid,
                    profile=profile,
                    cfg=cfg,
                    messages=messages,
                    trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
                    fixed_upper_message_id=initial_fixed_upper_message_id,
                    context_window_k=final_chat_params["context_window_k"],
                    max_tokens=final_chat_params["max_tokens"],
                    tools=None,
                )
            return ContextManager.trim_messages_for_model_request(
                messages=await materialize_latest_user_environment_prompt(
                    db,
                    session_id,
                    messages,
                    final_chat_params["max_tokens"],
                ),
                uid=uid,
                session_id=session_id,
                context_window_k=final_chat_params["context_window_k"],
                max_tokens=final_chat_params["max_tokens"],
                tools=None,
            )

        final_response, _chat_channel_obj, model_entry, _channel_rule, chat_params = await generate_chat_with_fallback(
            db,
            chat_channel=chat_channel,
            request_builder=build_final_request,
            call_context=f"{call_context}_final",
            cursor_key=chat_cursor_key,
            uid=uid,
            session_id=session_id,
            tools=None,
            require_content_or_tools=False,
            request_metadata_callback=request_metadata_callback,
        )
        final_msg = final_response.message
        if final_msg.tool_calls:
            repeated_tool_names = sorted({tool_call.name for tool_call in final_msg.tool_calls})
            logger.bind(
                uid=uid,
                session_id=session_id,
                reply_source=reply_source,
                repeated_tools=repeated_tool_names,
            ).warning(t("LOG_BACKGROUND_PROACTIVE_FINAL_TOOL_RETRY"))
            final_correction_messages = cls._build_virtual_tool_feedback_messages(
                final_msg,
                {
                    "type": "background_proactive_final_tool_correction",
                    "error": t(ERR_BACKGROUND_FINAL_REPLY_TOOL_CALL_FORBIDDEN),
                    "instruction": BACKGROUND_PROACTIVE_FINAL_TOOL_CORRECTION_PROMPT,
                    "ignored_tool_calls": repeated_tool_names,
                },
            )
            final_correction_context_messages = [*messages, *final_correction_messages]
            build_final_correction_request = partial(
                cls._build_final_correction_request,
                db=db,
                messages=final_correction_context_messages,
                uid=uid,
                session_id=session_id,
            )

            corrected_response, _chat_channel_obj, model_entry, _channel_rule, chat_params = await generate_chat_with_fallback(
                db,
                chat_channel=chat_channel,
                request_builder=build_final_correction_request,
                call_context=f"{call_context}_final_tool_correction",
                cursor_key=chat_cursor_key,
                uid=uid,
                session_id=session_id,
                tools=None,
                require_content_or_tools=True,
                request_metadata_callback=request_metadata_callback,
            )
            final_msg = corrected_response.message
            if final_msg.tool_calls:
                logger.bind(
                    uid=uid,
                    session_id=session_id,
                    reply_source=reply_source,
                    repeated_tools=sorted({tool_call.name for tool_call in final_msg.tool_calls}),
                ).warning(t("LOG_BACKGROUND_PROACTIVE_FINAL_TOOL_IGNORED"))
                final_msg.tool_calls = []
                fallback_message = MSG_BACKGROUND_FINAL_REPLY_FALLBACK_WITH_FILES if files_to_user else MSG_BACKGROUND_FINAL_REPLY_FALLBACK_WITHOUT_FILES
                final_msg.content = t(fallback_message)
        final_text, _untrusted_files = parse_assistant_files_content(final_msg.content)
        final_msg.content = final_text
        if not final_text.strip() and not files_to_user:
            raise LLMException(message=ERR_LLM_EMPTY_RESPONSE)

        logger.bind(uid=uid, session_id=session_id, reply_source=reply_source).info(t("LOG_DISPATCHER_LLM_RESPONSE", username=username, turn=1, content=final_text))
        if files_to_user:
            final_msg.content = build_assistant_files_content(final_text, files_to_user)
        messages.append(final_msg)
        turn_messages.append(final_msg)
        if persist_response:
            await save_assistant_message(
                db,
                session_id,
                uid,
                profile.id,
                final_msg,
                dedupe_key=final_message_dedupe_key,
            )
        return final_msg, turn_messages, files_to_user

    @classmethod
    async def dispatch_proactive_reply(cls, task_id: int) -> dict[str, Any]:
        async with AsyncSessionLocal() as db:
            task = await background_task_crud.get(db, task_id)
            if not task:
                raise ServerException(message=ERR_BACKGROUND_TASK_NOT_FOUND)
            task_uid = task.uid
            task_session_id = task.session_id
            task_profile_id = task.profile_id

            profile = await profile_crud.get_with_relations(db, task_profile_id)
            if not profile or profile.uid != task_uid:
                logger.bind(
                    task_id=task_id,
                    uid=task_uid,
                    session_id=task_session_id,
                    profile_id=task_profile_id,
                ).error(t("LOG_BACKGROUND_TASK_PROFILE_UNAVAILABLE"))
                raise ServerException(message=ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE)
            ai_msg, turn_messages, files = await cls._generate_reply_from_history(
                db,
                uid=task_uid,
                session_id=task_session_id,
                profile=profile,
                call_context="background_task_proactive_reply",
                allow_tools=True,
                reply_source="background_task",
            )
            content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
            return {
                "uid": task_uid,
                "session_id": task_session_id,
                "content": content,
                "files": files,
                "history": dump_background_proactive_history(turn_messages),
            }
