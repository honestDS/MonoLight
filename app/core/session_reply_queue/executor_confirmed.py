import json
import socket
import uuid
from dataclasses import dataclass
from typing import Any

from app.core.audit.confirmation import (
    get_pending_tool_results,
    notify_confirmation_tool_results,
    replace_pending_tool_result,
    update_confirmation_message_status,
)
from app.core.audit.integrity import verify_persisted_tool_round
from app.core.audit.service import audit_tool_round, is_audit_configured
from app.core.constants import (
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_AUDIT_EXECUTION_CLAIM_FAILED,
    ERR_AUDIT_SOURCE_MESSAGE_VERIFICATION_FAILED,
    ERR_TOOL_ROUND_PRECHECK_FAILED,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.session import session_crud
from app.core.i18n import get_current_locale, t
from app.core.prompts import AUDIT_SOURCE_MESSAGE_INVALID_PROMPT
from app.core.session_reply_queue.executor_audit import (
    _confirmed_file_snapshots_changed,
    _persist_confirmed_work_audit_execution_binding,
)
from app.core.session_reply_queue.executor_common import _response_content, _result_message_dedupe_key
from app.core.session_reply_queue.executor_interactive import _dispatch_interactive_work
from app.core.session_reply_queue.executor_metadata import _generate_reply_with_request_metadata
from app.core.tools import get_tools_for_profile
from app.core.utils.assistant_files import parse_assistant_files_content
from app.core.utils.background_task_result import serialize_execution_summary
from app.core.utils.dispatcher.helpers import dump_background_proactive_history
from app.core.utils.dispatcher.process_single_tool import (
    get_handed_off_terminal_session_id,
    get_queued_background_task_id,
    prevalidate_tool_round,
    process_single_tool,
)
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.session_todo_snapshot import persist_session_todo_snapshot_on_tool_results
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem

__all__ = []


def _resolve_confirmed_tool_context_window_k(session) -> int:
    metadata = getattr(session, "llm_request_metadata", None)
    context_window_tokens = metadata.get("context_window_tokens") if isinstance(metadata, dict) else None
    if isinstance(context_window_tokens, int) and not isinstance(context_window_tokens, bool) and context_window_tokens > 0:
        return max(1, context_window_tokens // CONTEXT_WINDOW_TOKENS_PER_K)
    return 4


async def _source_invalid_confirmed_tool_response(
    db,
    *,
    work: SessionReplyWorkItem,
    audit_record_id: int,
    record,
    claim_token: str,
    profile,
) -> dict[str, Any]:
    if record is not None and claim_token:
        await audit_crud.mark_source_message_invalid(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
            error_reason="原工具调用记录校验失败",
        )
        await update_confirmation_message_status(db, audit_record_id=audit_record_id)
    if profile is None:
        raise RuntimeError(t(ERR_AUDIT_SOURCE_MESSAGE_VERIFICATION_FAILED))
    ai_msg, turn_messages, files, llm_request_metadata = await _generate_reply_with_request_metadata(
        db,
        work=work,
        uid=work.uid,
        session_id=work.session_id,
        profile=profile,
        call_context="confirmed_tool_source_invalid",
        allow_tools=False,
        extra_messages=[InternalMessage(role=MessageRole.USER, content=AUDIT_SOURCE_MESSAGE_INVALID_PROMPT)],
        reply_source="confirmed_tool_execution",
        final_message_dedupe_key=_result_message_dedupe_key(work),
    )
    content = parse_assistant_files_content(ai_msg.content)
    response = {"content": content, "history": dump_background_proactive_history(turn_messages), "files": files}
    if llm_request_metadata is not None:
        response["llm_request_metadata"] = llm_request_metadata
    return response


@dataclass
class _ConfirmedToolResultReplacementState:
    db: Any
    pending_tool_results: Any
    audit_record_id: int
    messages: list[InternalMessage]
    turn_messages: list[InternalMessage]
    replaced_tool_results: bool = False


async def _append_confirmed_tool_result(
    replacement_state: _ConfirmedToolResultReplacementState,
    original_tool_call_id: str,
    tool_result: InternalMessage,
) -> InternalMessage:
    sanitized_content = await replace_pending_tool_result(
        replacement_state.db,
        pending_message=replacement_state.pending_tool_results[original_tool_call_id],
        original_tool_call_id=original_tool_call_id,
        content=tool_result.content,
        audit_record_id=replacement_state.audit_record_id,
    )
    stored_tool_result = tool_result.model_copy(deep=True)
    stored_tool_result.content = sanitized_content
    stored_tool_result.id = replacement_state.pending_tool_results[original_tool_call_id].id
    replacement_state.messages.append(stored_tool_result)
    replacement_state.turn_messages.append(stored_tool_result)
    replacement_state.replaced_tool_results = True
    return stored_tool_result


async def _execute_confirmed_tools(db, work: SessionReplyWorkItem, worker_id: str = "") -> dict[str, Any]:
    """校验并执行已确认工具，同时完整关闭审计执行整轮。"""
    audit_record_id = int(work.source_id)
    claim_token = str((work.execution_state or {}).get("audit_claim_token") or "")
    record = await audit_crud.get_record(db, audit_record_id)
    details = await audit_crud.list_tool_details(db, audit_record_id)
    execution_state = work.execution_state or {}
    decision_message_id = execution_state.get("decision_message_id")
    decision_raw_message = getattr(record, "decision_raw_message", None)
    source_message = await db.get(Message, record.source_assistant_message_id) if record is not None else None
    source_tool_calls: list[InternalToolCall] = []
    source_valid = bool(
        record
        and claim_token
        and record.status == AuditRecordStatus.EXECUTING
        and record.execution_claim_token == claim_token
        and source_message
        and source_message.uid == work.uid
        and source_message.session_id == work.session_id
        and source_message.role == MessageRole.ASSISTANT
        and source_message.type == MessageType.TOOL_CALL
        and isinstance(decision_message_id, int)
        and not isinstance(decision_message_id, bool)
        and decision_message_id > 0
        and getattr(record, "decision_message_id", None) == decision_message_id
        and isinstance(decision_raw_message, str)
        and decision_raw_message.strip()
    )
    if source_valid:
        try:
            source_internal = InternalMessage.model_validate(json.loads(source_message.content or "{}"))
            source_tool_calls = list(source_internal.tool_calls or [])
            source_valid = verify_persisted_tool_round(
                expected_round_sha256=record.round_arguments_hash,
                expected_tool_calls=[
                    {
                        "original_tool_call_id": detail.original_tool_call_id,
                        "turn_index": detail.turn_index,
                        "tool_name": detail.tool_name,
                        "arguments_hash": detail.arguments_hash,
                    }
                    for detail in details
                ],
                tool_calls=[{"id": item.id, "name": item.name, "arguments": item.arguments} for item in source_tool_calls],
                uid=work.uid,
                session_id=work.session_id,
                working_directory=record.working_directory,
            )
        except Exception:
            source_valid = False

    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        source_valid = False

    if not source_valid:
        return await _source_invalid_confirmed_tool_response(
            db,
            work=work,
            audit_record_id=audit_record_id,
            record=record,
            claim_token=claim_token,
            profile=profile,
        )

    cfg = await validate_profile_and_cfg(db, profile)
    session = await session_crud.get_by_session_id(db, work.session_id)
    confirmed_tool_context_window_k = _resolve_confirmed_tool_context_window_k(session)
    files_changed = _confirmed_file_snapshots_changed(details, working_directory=record.working_directory)
    pending_tool_results = await get_pending_tool_results(
        db,
        uid=work.uid,
        session_id=work.session_id,
        source_assistant_message_id=source_message.id,
        before_message_id=decision_message_id,
        tool_call_ids=[tool_call.id for tool_call in source_tool_calls],
        audit_record_id=audit_record_id,
    )
    if pending_tool_results is None:
        return await _source_invalid_confirmed_tool_response(
            db,
            work=work,
            audit_record_id=audit_record_id,
            record=record,
            claim_token=claim_token,
            profile=profile,
        )

    async def _commit_and_notify_confirmation_tool_results(notify_audit_record_id: int) -> None:
        await db.commit()
        await notify_confirmation_tool_results(db, audit_record_id=notify_audit_record_id)

    reaudit_round = None
    if files_changed and is_audit_configured(cfg):
        reaudit_round = await audit_tool_round(
            db,
            cfg=cfg,
            tool_calls=source_tool_calls,
            source_assistant_message_id=source_message.id,
            uid=work.uid,
            operator_username=record.operator_username,
            session_id=work.session_id,
            source=record.source,
            language=get_current_locale(),
            working_directory=record.working_directory,
        )
    if reaudit_round is not None:
        await audit_crud.cancel_execution_for_file_reaudit(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
            error_reason="命令直接引用的文件已变化，原确认失效",
        )
        await update_confirmation_message_status(db, audit_record_id=audit_record_id)
        if not reaudit_round.may_execute:
            messages = [source_internal]
            turn_messages = [source_internal]
            reaudit_results_by_call_id: dict[str, InternalMessage] = {}
            for tool_result in reaudit_round.tool_results:
                tool_call_id = tool_result.tool_call_id
                if tool_result.role != MessageRole.TOOL or not isinstance(tool_call_id, str) or tool_call_id in reaudit_results_by_call_id:
                    raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
                reaudit_results_by_call_id[tool_call_id] = tool_result
            if set(reaudit_results_by_call_id) != {tool_call.id for tool_call in source_tool_calls}:
                raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
            for original_call in source_tool_calls:
                tool_result = reaudit_results_by_call_id[original_call.id]
                sanitized_content = await replace_pending_tool_result(
                    db,
                    pending_message=pending_tool_results[original_call.id],
                    original_tool_call_id=original_call.id,
                    content=tool_result.content,
                    audit_record_id=reaudit_round.audit_record_id,
                )
                stored_tool_result = tool_result.model_copy(deep=True)
                stored_tool_result.content = sanitized_content
                stored_tool_result.id = pending_tool_results[original_call.id].id
                messages.append(stored_tool_result)
                turn_messages.append(stored_tool_result)
            await persist_session_todo_snapshot_on_tool_results(
                db,
                uid=work.uid,
                session_id=work.session_id,
                tool_results=[message for message in turn_messages if message.role == MessageRole.TOOL],
            )
            if reaudit_round.confirmation_payload is not None:
                confirmation_content = json.dumps(reaudit_round.confirmation_payload, ensure_ascii=False)
                await save_message(
                    db,
                    work.session_id,
                    work.uid,
                    MessageRole.ASSISTANT,
                    MessageType.AUDIT_CONFIRMATION,
                    reaudit_round.confirmation_payload,
                    profile.id,
                    is_processed=True,
                    dedupe_key=_result_message_dedupe_key(work),
                )
                status_updated = await update_confirmation_message_status(db, audit_record_id=reaudit_round.audit_record_id)
                if status_updated is False:
                    await _commit_and_notify_confirmation_tool_results(reaudit_round.audit_record_id)
                turn_messages.append(InternalMessage(role=MessageRole.ASSISTANT, content=confirmation_content))
                return {
                    "content": confirmation_content,
                    "history": dump_background_proactive_history(turn_messages),
                    "files": [],
                }
            ai_msg, final_messages, files, llm_request_metadata = await _generate_reply_with_request_metadata(
                db,
                work=work,
                uid=work.uid,
                session_id=work.session_id,
                profile=profile,
                call_context="confirmed_tool_file_reaudit_blocked",
                allow_tools=False,
                reply_source="confirmed_tool_execution",
                final_message_dedupe_key=_result_message_dedupe_key(work),
            )
            content = parse_assistant_files_content(ai_msg.content)
            response = {
                "content": content,
                "history": dump_background_proactive_history([*turn_messages, *final_messages]),
                "files": files,
            }
            if llm_request_metadata is not None:
                response["llm_request_metadata"] = llm_request_metadata
            return response
        record, claim_token = await audit_crud.claim_passed_for_execution(
            db,
            audit_record_id=reaudit_round.audit_record_id,
        )
        if record is None or claim_token is None:
            raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
        audit_record_id = reaudit_round.audit_record_id
        await _persist_confirmed_work_audit_execution_binding(
            db,
            work=work,
            worker_id=worker_id,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
        )
        details = await audit_crud.list_tool_details(db, audit_record_id)

    _tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile)
    detail_by_original_id = {detail.original_tool_call_id: detail for detail in details}
    confirmed_calls = [InternalToolCall(id=f"call_{uuid.uuid4().hex}", name=item.name, arguments=dict(item.arguments or {})) for item in source_tool_calls]
    confirmed_message = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=confirmed_calls)
    messages = [confirmed_message]
    turn_messages = [confirmed_message]
    replacement_state = _ConfirmedToolResultReplacementState(
        db=db,
        pending_tool_results=pending_tool_results,
        audit_record_id=audit_record_id,
        messages=messages,
        turn_messages=turn_messages,
    )

    executions_by_original_call_id = {}
    for original_call, confirmed_call in zip(source_tool_calls, confirmed_calls, strict=True):
        detail = detail_by_original_id[original_call.id]
        execution = await audit_crud.create_execution_attempt(
            db,
            audit_record_id=audit_record_id,
            audit_tool_detail_id=detail.id,
            claim_token=claim_token,
            execution_node=socket.gethostname(),
            new_tool_call_id=confirmed_call.id,
        )
        if execution is None:
            break
        executions_by_original_call_id[original_call.id] = execution

    precheck_errors = prevalidate_tool_round(confirmed_calls, cfg, tool_schemas=_tools)
    all_attempts_created = len(executions_by_original_call_id) == len(source_tool_calls)
    all_succeeded = all_attempts_created and not precheck_errors
    execution_round_status = None
    if not all_succeeded:
        cancellation_error = t(ERR_TOOL_ROUND_PRECHECK_FAILED) if precheck_errors else t(ERR_AUDIT_EXECUTION_CLAIM_FAILED)
        for execution in executions_by_original_call_id.values():
            await audit_crud.finish_execution_attempt(
                db,
                execution_record_id=execution.id,
                status=AuditExecutionStatus.CANCELLED,
                error=cancellation_error,
            )
        for original_call, confirmed_call in zip(source_tool_calls, confirmed_calls, strict=True):
            error_content = precheck_errors.get(confirmed_call.id)
            if error_content is None:
                error_content = json.dumps(
                    {
                        "status": "failed",
                        "tool_name": confirmed_call.name,
                        "error": t(ERR_TOOL_ROUND_PRECHECK_FAILED) if precheck_errors else t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
                    },
                    ensure_ascii=False,
                )
            await _append_confirmed_tool_result(
                replacement_state,
                original_call.id,
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=confirmed_call.id,
                    content=error_content,
                ),
            )
        if precheck_errors or not all_attempts_created:
            round_closed = await audit_crud.finish_execution_round(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
                status=AuditRecordStatus.FAILED,
                error_reason=cancellation_error,
            )
            if not round_closed:
                raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
            execution_round_status = AuditRecordStatus.FAILED
    else:
        for original_call, confirmed_call in zip(source_tool_calls, confirmed_calls, strict=True):
            detail = detail_by_original_id[original_call.id]
            execution = executions_by_original_call_id[original_call.id]
            tool_result = await process_single_tool(
                confirmed_call,
                db,
                profile,
                cfg,
                messages,
                record.operator_username,
                work.session_id,
                detail.turn_index,
                work.uid,
                allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                context_window_k=confirmed_tool_context_window_k,
                tool_call_count=len(confirmed_calls),
            )
            await _append_confirmed_tool_result(replacement_state, original_call.id, tool_result)
            try:
                result_payload = json.loads(tool_result.content or "{}")
            except (TypeError, ValueError):
                result_payload = {}
            terminal_session_id = get_handed_off_terminal_session_id(tool_result.content) if confirmed_call.name == "execute_shell" else None
            if get_queued_background_task_id(tool_result.content) is None and terminal_session_id is None:
                succeeded = not (isinstance(result_payload, dict) and (result_payload.get("error") or result_payload.get("status") == "failed" or (isinstance(result_payload.get("exit_code"), int) and result_payload["exit_code"] != 0)))
                all_succeeded = all_succeeded and succeeded
                result_summary = serialize_execution_summary(
                    tool_result.content,
                    max_chars=1000,
                )
                await audit_crud.finish_execution_attempt(
                    db,
                    execution_record_id=execution.id,
                    status=AuditExecutionStatus.SUCCEEDED if succeeded else AuditExecutionStatus.FAILED,
                    result_summary=result_summary,
                    error=None if succeeded else result_summary,
                )

    if execution_round_status is None:
        execution_round_status = await audit_crud.finish_execution_round_if_complete(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
        )
    if execution_round_status is not None:
        status_updated = await update_confirmation_message_status(db, audit_record_id=audit_record_id)
        if replacement_state.replaced_tool_results and status_updated is False:
            await _commit_and_notify_confirmation_tool_results(audit_record_id)
    elif replacement_state.replaced_tool_results:
        await _commit_and_notify_confirmation_tool_results(audit_record_id)

    await persist_session_todo_snapshot_on_tool_results(
        db,
        uid=work.uid,
        session_id=work.session_id,
        tool_results=[message for message in replacement_state.turn_messages if message.role == MessageRole.TOOL],
    )

    guidance_prompt = (work.execution_state or {}).get("guidance_prompt")
    initial_message = InternalMessage(
        id=decision_message_id,
        role=MessageRole.USER,
        content=decision_raw_message,
        guidance_prompt=guidance_prompt if isinstance(guidance_prompt, str) else None,
    )
    interactive_response = await _dispatch_interactive_work(
        db,
        work=work,
        worker_id=worker_id,
        message=decision_raw_message,
        initial_message=initial_message,
        history_before_id=decision_message_id,
        frozen_user_message_ids=[decision_message_id],
        attachments=None,
        allow_additional_user_messages=True,
        execution_resume_state=None,
    )
    content = parse_assistant_files_content(_response_content(interactive_response))
    history = interactive_response.get("history", [])
    files = interactive_response.get("files") or []
    response = {
        "content": content,
        "history": history if isinstance(history, list) else [],
        "files": files if isinstance(files, list) else [],
    }
    if interactive_response.get("llm_request_metadata") is not None:
        response["llm_request_metadata"] = interactive_response["llm_request_metadata"]
    response_id = interactive_response.get("response_id")
    if isinstance(response_id, str) and response_id:
        response["response_id"] = response_id
    return response
