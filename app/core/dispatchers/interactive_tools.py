import asyncio
import json
import socket
import time
from typing import Any

from app.core.audit.confirmation import (
    persist_cancelled_pending_audit_results,
    persist_pending_confirmation_bundle,
    supersede_persisted_pending_confirmation_bundle,
    update_confirmation_message_status,
)
from app.core.audit.service import audit_tool_round
from app.core.constants import (
    ERR_AUDIT_EXECUTION_CLAIM_FAILED,
    ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN,
    ERR_TOOL_ROUND_PRECHECK_FAILED,
)
from app.core.crud.audit.audit import audit_crud
from app.core.dispatchers.interactive_state import InteractiveDispatchState
from app.core.exceptions import ServerException
from app.core.i18n import get_current_locale, t
from app.core.utils.background_task_result import serialize_execution_summary
from app.core.utils.dispatcher.append_new_user_messages import append_new_user_messages
from app.core.utils.dispatcher.handle_parallel_tool_limit import handle_parallel_tool_limit
from app.core.utils.dispatcher.helpers import dump_output_history, extract_files_to_user
from app.core.utils.dispatcher.process_single_tool import (
    get_handed_off_terminal_session_id,
    get_queued_background_task_id,
    prevalidate_tool_round,
)
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.session_todo_snapshot import persist_session_todo_snapshot_on_tool_results
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.message import InternalMessage, MessageRole
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse

from .interactive_helpers import (
    _execute_isolated_tool_call,
    _fetch_additional_user_messages,
    _find_tool_call_by_id,
    _mark_claimed_audit_execution_unknown,
    _ParallelToolExecutionContext,
    _save_execution_checkpoint,
    _tool_result_succeeded,
    update_memory_recall_boundary,
)

__all__ = [
    "AuditExecutionStatePersistenceError",
    "handle_interactive_tool_round",
]


class AuditExecutionStatePersistenceError(ServerException):
    def __init__(self, cause: str) -> None:
        super().__init__(message=ERR_AUDIT_EXECUTION_CLAIM_FAILED, cause=cause)


async def handle_interactive_tool_round(
    state: InteractiveDispatchState,
    *,
    ai_msg: InternalMessage,
    saved_msg,
    response_id: str,
) -> dict[str, Any] | None:
    if len(ai_msg.tool_calls) > state.cfg.tool.max_parallel_tools:
        await handle_parallel_tool_limit(
            state.db,
            state.session_id,
            state.uid,
            state.profile,
            state.cfg,
            ai_msg,
            state.messages,
            state.turn_messages,
        )
        await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)
        return None

    precheck_errors = prevalidate_tool_round(ai_msg.tool_calls, state.cfg, tool_schemas=state.tools)
    if precheck_errors:
        stored_tool_results: list[InternalMessage] = []
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
            stored_tool_results.append(
                await save_tool_response(
                    state.db,
                    state.session_id,
                    state.uid,
                    state.profile.id,
                    tool_result,
                    state.messages,
                    state.turn_messages,
                )
            )
        await persist_session_todo_snapshot_on_tool_results(
            state.db,
            uid=state.uid,
            session_id=state.session_id,
            tool_results=stored_tool_results,
        )
        await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)
        return None

    if state.stream_event_callback is not None and state.show_tool_calls:
        for tool_call_index, tool_call in enumerate(ai_msg.tool_calls):
            await state.stream_event_callback(
                {
                    "type": "tool_start",
                    "name": tool_call.name,
                    "arguments": tool_call.arguments,
                    "tool_call_id": tool_call.id,
                    "response_id": response_id,
                    "tool_call_index": tool_call_index,
                    "tool_call_count": len(ai_msg.tool_calls),
                }
            )

    audit_round = await audit_tool_round(
        state.db,
        cfg=state.cfg,
        tool_calls=ai_msg.tool_calls,
        source_assistant_message_id=saved_msg.id,
        uid=state.uid,
        operator_username=state.username,
        session_id=state.session_id,
        source=state.session_source,
        language=get_current_locale(),
    )
    if audit_round is not None and not audit_round.may_execute:
        if audit_round.confirmation_payload is not None:
            new_user_batch = await _fetch_additional_user_messages(state.additional_user_messages_context, state.chat_params["max_tokens"])
            if new_user_batch is not None:
                stored_tool_results = await persist_cancelled_pending_audit_results(
                    state.db,
                    audit_record_id=audit_round.audit_record_id,
                    uid=state.uid,
                    session_id=state.session_id,
                    profile_id=state.profile.id,
                    tool_results=audit_round.tool_results,
                )
                await persist_session_todo_snapshot_on_tool_results(
                    state.db,
                    uid=state.uid,
                    session_id=state.session_id,
                    tool_results=stored_tool_results,
                )
                for stored_tool_result in stored_tool_results:
                    state.messages.append(stored_tool_result)
                    state.turn_messages.append(stored_tool_result)
                    if state.stream_event_callback is not None and state.show_tool_calls:
                        tool_call = _find_tool_call_by_id(ai_msg.tool_calls, stored_tool_result.tool_call_id)
                        await state.stream_event_callback(
                            {
                                "type": "tool_end",
                                "name": tool_call.name if tool_call else "unknown",
                                "result": stored_tool_result.content,
                                "tool_call_id": stored_tool_result.tool_call_id,
                                "response_id": response_id,
                            }
                        )
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
                return None
            stored_tool_results, _confirmation_message = await persist_pending_confirmation_bundle(
                state.db,
                audit_record_id=audit_round.audit_record_id,
                uid=state.uid,
                session_id=state.session_id,
                profile_id=state.profile.id,
                tool_results=audit_round.tool_results,
                confirmation_payload=audit_round.confirmation_payload,
                dedupe_key=state.final_message_dedupe_key,
            )
            new_user_batch = await _fetch_additional_user_messages(state.additional_user_messages_context, state.chat_params["max_tokens"])
            if new_user_batch is not None:
                stored_tool_results = await supersede_persisted_pending_confirmation_bundle(
                    state.db,
                    audit_record_id=audit_round.audit_record_id,
                    uid=state.uid,
                    session_id=state.session_id,
                )
                await persist_session_todo_snapshot_on_tool_results(
                    state.db,
                    uid=state.uid,
                    session_id=state.session_id,
                    tool_results=stored_tool_results,
                )
                for stored_tool_result in stored_tool_results:
                    state.messages.append(stored_tool_result)
                    state.turn_messages.append(stored_tool_result)
                    if state.stream_event_callback is not None and state.show_tool_calls:
                        tool_call = _find_tool_call_by_id(ai_msg.tool_calls, stored_tool_result.tool_call_id)
                        await state.stream_event_callback(
                            {
                                "type": "tool_end",
                                "name": tool_call.name if tool_call else "unknown",
                                "result": stored_tool_result.content,
                                "tool_call_id": stored_tool_result.tool_call_id,
                                "response_id": response_id,
                            }
                        )
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
                return None
            await persist_session_todo_snapshot_on_tool_results(
                state.db,
                uid=state.uid,
                session_id=state.session_id,
                tool_results=stored_tool_results,
            )
            for tool_result, stored_tool_result in zip(audit_round.tool_results, stored_tool_results, strict=True):
                state.messages.append(stored_tool_result)
                state.turn_messages.append(stored_tool_result)
                if state.stream_event_callback is not None and state.show_tool_calls:
                    tool_call = _find_tool_call_by_id(ai_msg.tool_calls, tool_result.tool_call_id)
                    await state.stream_event_callback(
                        {
                            "type": "tool_end",
                            "name": tool_call.name if tool_call else "unknown",
                            "result": tool_result.content,
                            "tool_call_id": tool_result.tool_call_id,
                            "response_id": response_id,
                        }
                    )
        else:
            stored_tool_results: list[InternalMessage] = []
            for tool_result in audit_round.tool_results:
                stored_tool_result = await save_tool_response(
                    state.db,
                    state.session_id,
                    state.uid,
                    state.profile.id,
                    tool_result,
                    state.messages,
                    state.turn_messages,
                )
                stored_tool_results.append(stored_tool_result)
                if state.stream_event_callback is not None and state.show_tool_calls:
                    tool_call = _find_tool_call_by_id(ai_msg.tool_calls, tool_result.tool_call_id)
                    await state.stream_event_callback(
                        {
                            "type": "tool_end",
                            "name": tool_call.name if tool_call else "unknown",
                            "result": tool_result.content,
                            "tool_call_id": tool_result.tool_call_id,
                            "response_id": response_id,
                        }
                    )
            await persist_session_todo_snapshot_on_tool_results(
                state.db,
                uid=state.uid,
                session_id=state.session_id,
                tool_results=stored_tool_results,
            )
        if audit_round.confirmation_payload is not None:
            confirmation_content = json.dumps(audit_round.confirmation_payload, ensure_ascii=False)
            await update_confirmation_message_status(state.db, audit_record_id=audit_round.audit_record_id)
            state.final_ai_content = confirmation_content
            response = LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ASSISTANT, content=confirmation_content),
                        finish_reason=True,
                        created_at=time.time(),
                    )
                ],
                history=dump_output_history(
                    state.turn_messages,
                    show_tool_calls=state.show_tool_calls,
                ),
                files=state.files_to_user or None,
            ).model_dump()
            if state.latest_llm_request_metadata is not None:
                response["llm_request_metadata"] = state.latest_llm_request_metadata
            return response
        await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)
        return None

    audit_claim_token = None
    audit_execution_ids: dict[str, int] = {}
    audit_execution_checkpoint_state: dict[str, Any] | None = None
    audit_all_succeeded = True
    if audit_round is not None:
        claimed_record = None
        try:
            claimed_record, audit_claim_token = await audit_crud.claim_passed_for_execution(
                state.db,
                audit_record_id=audit_round.audit_record_id,
            )
            if claimed_record is not None and audit_claim_token is not None:
                audit_details = await audit_crud.list_tool_details(state.db, audit_round.audit_record_id)
                detail_by_call_id = {detail.original_tool_call_id: detail for detail in audit_details}
                for tool_call in ai_msg.tool_calls:
                    detail = detail_by_call_id.get(tool_call.id)
                    if detail is None:
                        audit_claim_token = None
                        break
                    execution = await audit_crud.create_execution_attempt(
                        state.db,
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
                        state.db,
                        execution_record_id=execution_id,
                        status=AuditExecutionStatus.CANCELLED,
                        error=t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
                    )
                if claimed_record is not None and claimed_record.execution_claim_token:
                    await audit_crud.finish_execution_round(
                        state.db,
                        audit_record_id=audit_round.audit_record_id,
                        claim_token=claimed_record.execution_claim_token,
                        status=AuditRecordStatus.FAILED,
                        error_reason=t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
                    )
                    await update_confirmation_message_status(state.db, audit_record_id=audit_round.audit_record_id)
                stored_tool_results: list[InternalMessage] = []
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
                    stored_tool_results.append(
                        await save_tool_response(
                            state.db,
                            state.session_id,
                            state.uid,
                            state.profile.id,
                            tool_result,
                            state.messages,
                            state.turn_messages,
                        )
                    )
                    if state.stream_event_callback is not None and state.show_tool_calls:
                        await state.stream_event_callback(
                            {
                                "type": "tool_end",
                                "name": tool_call.name,
                                "result": tool_result.content,
                                "tool_call_id": tool_call.id,
                                "response_id": response_id,
                            }
                        )
                await persist_session_todo_snapshot_on_tool_results(
                    state.db,
                    uid=state.uid,
                    session_id=state.session_id,
                    tool_results=stored_tool_results,
                )
                await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)
                return None

            if state.checkpoint_state.callback is not None:
                audit_execution_checkpoint_state = {
                    "audit_record_id": audit_round.audit_record_id,
                    "claim_token": audit_claim_token,
                }
                await _save_execution_checkpoint(
                    state.checkpoint_state,
                    state.messages,
                    state.current_turn,
                    active_audit_execution=audit_execution_checkpoint_state,
                    update_active_audit_execution=True,
                )
        except asyncio.CancelledError:
            if audit_claim_token is not None:
                await _mark_claimed_audit_execution_unknown(state.db, audit_round.audit_record_id, audit_claim_token)
                raise AuditExecutionStatePersistenceError(cause=t(ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN))
            raise
        except Exception as exc:
            if audit_claim_token is not None:
                await _mark_claimed_audit_execution_unknown(state.db, audit_round.audit_record_id, audit_claim_token)
                raise AuditExecutionStatePersistenceError(cause=str(exc)) from exc
            raise

    # 在任何工具开始前持久化完整调用意图。恢复时未落库的工具结果会被视为
    # 执行状态未知并交给模型核实，而不会重新执行原工具。
    await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)

    parallel_tool_context = _ParallelToolExecutionContext(
        semaphore=asyncio.Semaphore(state.cfg.tool.executor_max_workers),
        active_tasks=state.active_tasks,
        profile=state.profile,
        cfg=state.cfg,
        messages=state.messages,
        username=state.username,
        session_id=state.session_id,
        current_turn=state.current_turn,
        uid=state.uid,
        allowed_knowledge_base_ids=state.allowed_knowledge_base_ids,
        context_window_k=state.chat_params["context_window_k"],
        tool_call_count=len(ai_msg.tool_calls),
        context_summary_boundary_message_id=state.checkpoint_state.upper_message_id,
        source_message_id=state.checkpoint_state.memory_recall_boundary_message_id,
    )
    tasks = [asyncio.create_task(_execute_isolated_tool_call(parallel_tool_context, tc)) for tc in ai_msg.tool_calls]
    stored_tool_results: list[InternalMessage] = []
    completed_tool_count = 0
    try:
        for completed_task in asyncio.as_completed(tasks):
            tool_res = await completed_task
            tool_call = _find_tool_call_by_id(ai_msg.tool_calls, tool_res.tool_call_id)
            if audit_claim_token is not None:
                execution_id = audit_execution_ids[tool_res.tool_call_id]
                queued_task_id = get_queued_background_task_id(tool_res.content)
                terminal_session_id = get_handed_off_terminal_session_id(tool_res.content) if tool_call is not None and tool_call.name == "execute_shell" else None
                if queued_task_id is None and terminal_session_id is None:
                    execution_succeeded = _tool_result_succeeded(tool_res.content)
                    audit_all_succeeded = audit_all_succeeded and execution_succeeded
                    result_summary = serialize_execution_summary(
                        tool_res.content,
                        max_chars=1000,
                    )
                    await audit_crud.finish_execution_attempt(
                        state.db,
                        execution_record_id=execution_id,
                        status=AuditExecutionStatus.SUCCEEDED if execution_succeeded else AuditExecutionStatus.FAILED,
                        result_summary=result_summary,
                        error=None if execution_succeeded else result_summary,
                    )
                elif audit_execution_checkpoint_state is not None:
                    audit_execution_checkpoint_state["handoff_state"] = "persisted"
                    if queued_task_id is not None:
                        task_ids = audit_execution_checkpoint_state.setdefault("background_task_ids", [])
                        if queued_task_id not in task_ids:
                            task_ids.append(queued_task_id)
                    if terminal_session_id is not None:
                        terminal_session_ids = audit_execution_checkpoint_state.setdefault("terminal_session_ids", [])
                        if terminal_session_id not in terminal_session_ids:
                            terminal_session_ids.append(terminal_session_id)
            state.files_to_user.extend(extract_files_to_user([tool_res]))
            stored_tool_results.append(
                await save_tool_response(
                    state.db,
                    state.session_id,
                    state.uid,
                    state.profile.id,
                    tool_res,
                    state.messages,
                    state.turn_messages,
                )
            )
            completed_tool_count += 1
            if completed_tool_count == len(tasks):
                await persist_session_todo_snapshot_on_tool_results(
                    state.db,
                    uid=state.uid,
                    session_id=state.session_id,
                    tool_results=stored_tool_results,
                )
            if audit_execution_checkpoint_state is not None and (queued_task_id is not None or terminal_session_id is not None):
                await _save_execution_checkpoint(
                    state.checkpoint_state,
                    state.messages,
                    state.current_turn,
                    active_audit_execution=audit_execution_checkpoint_state,
                    update_active_audit_execution=True,
                )
            else:
                await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)
            if state.stream_event_callback is not None and state.show_tool_calls:
                await state.stream_event_callback(
                    {
                        "type": "tool_end",
                        "name": tool_call.name if tool_call else "unknown",
                        "result": tool_res.content,
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
        if hasattr(state.db, "execute"):
            execution_round_status = await audit_crud.finish_execution_round_if_complete(
                state.db,
                audit_record_id=audit_round.audit_record_id,
                claim_token=audit_claim_token,
            )
        else:
            legacy_round_finished = await audit_crud.finish_execution_round(
                state.db,
                audit_record_id=audit_round.audit_record_id,
                claim_token=audit_claim_token,
                status=AuditRecordStatus.SUCCEEDED if audit_all_succeeded else AuditRecordStatus.FAILED,
                error_reason=None if audit_all_succeeded else t(ERR_AUDIT_EXECUTION_CLAIM_FAILED),
            )
            if not legacy_round_finished:
                raise AuditExecutionStatePersistenceError(cause=t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
            execution_round_status = AuditRecordStatus.SUCCEEDED if audit_all_succeeded else AuditRecordStatus.FAILED
        if execution_round_status is not None and state.checkpoint_state.callback is not None:
            await _save_execution_checkpoint(
                state.checkpoint_state,
                state.messages,
                state.current_turn,
                active_audit_execution=None,
                update_active_audit_execution=True,
            )
        if execution_round_status is not None:
            await update_confirmation_message_status(state.db, audit_record_id=audit_round.audit_record_id)

    await _save_execution_checkpoint(state.checkpoint_state, state.messages, state.current_turn)
    return None
