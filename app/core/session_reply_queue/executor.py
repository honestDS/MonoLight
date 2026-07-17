import json
import socket
import time
import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import update

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.audit.integrity import create_file_integrity_snapshot, verify_persisted_tool_round
from app.core.audit.service import audit_tool_round
from app.core.constants import (
    ERR_AUDIT_EXECUTION_CLAIM_FAILED,
    ERR_AUDIT_SOURCE_MESSAGE_VERIFICATION_FAILED,
    ERR_BACKGROUND_TASK_NOT_FOUND,
    ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE,
    ERR_LLM_UNEXPECTED_ERROR,
    ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND,
    ERR_SESSION_REPLY_FINAL_MESSAGE_NOT_PERSISTED,
    ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT,
    ERR_TOOL_ROUND_PRECHECK_FAILED,
)
from app.core.crud.audit import audit_crud
from app.core.crud.background_task import background_task_crud
from app.core.crud.message import message_crud
from app.core.crud.profile import profile_crud
from app.core.crud.session import session_crud
from app.core.crud.session_reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session_reply_work_item import session_reply_work_item_crud
from app.core.dispatcher import ChatDispatcher
from app.core.i18n import get_current_locale, t
from app.core.message_platforms.notifier import send_session_event
from app.core.prompts import AUDIT_SOURCE_MESSAGE_INVALID_PROMPT, BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT
from app.core.session_reply_queue.manager import build_session_reply_work_event_id, build_session_reply_work_identity, session_reply_queue_manager
from app.core.tools import get_tools_for_profile
from app.core.utils.assistant_files import parse_assistant_files_content
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.helpers import dump_background_proactive_history
from app.core.utils.dispatcher.process_single_tool import prevalidate_tool_round, process_single_tool
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus
from app.models.message import InternalMessage, InternalToolCall, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus, SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX = "session-reply-work"


def _work_identity(work: SessionReplyWorkItem) -> str:
    return build_session_reply_work_identity(work)


def _result_message_dedupe_key(work: SessionReplyWorkItem) -> str:
    return f"{SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX}:{_work_identity(work)}:result"


def _error_message_dedupe_key(work: SessionReplyWorkItem) -> str:
    return f"{SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX}:{_work_identity(work)}:error"


def _legacy_result_message_dedupe_key(work_id: int) -> str:
    return f"{SESSION_REPLY_WORK_MESSAGE_KEY_PREFIX}:{work_id}:result"


def _message_belongs_to_work(message: Message, work: SessionReplyWorkItem) -> bool:
    if message.uid != work.uid or message.session_id != work.session_id or message.profile_id != work.profile_id:
        return False
    if not isinstance(message.created_at, datetime) or not isinstance(work.created_at, datetime):
        return False
    return message.created_at.replace(tzinfo=None) >= work.created_at.replace(tzinfo=None)


async def _get_persisted_result(db, work: SessionReplyWorkItem) -> Message | None:
    persisted_result = await db.get(Message, work.result_message_id) if work.result_message_id else None
    if persisted_result is not None and _message_belongs_to_work(persisted_result, work):
        return persisted_result

    persisted_result = await message_crud.get_by_dedupe_key(db, _result_message_dedupe_key(work))
    if persisted_result is not None and _message_belongs_to_work(persisted_result, work):
        return persisted_result

    if work.id is None:
        return None
    legacy_result = await message_crud.get_by_dedupe_key(db, _legacy_result_message_dedupe_key(work.id))
    if legacy_result is not None and _message_belongs_to_work(legacy_result, work):
        return legacy_result
    return None


def _response_from_persisted_message(work: SessionReplyWorkItem, message: Message) -> dict[str, Any]:
    content, files = parse_assistant_files_content(message.content)
    if work.work_type == SessionReplyWorkType.FOREGROUND_REPLY:
        return {
            "choices": [
                {
                    "message": {
                        "role": MessageRole.ASSISTANT,
                        "content": content,
                    },
                    "finish_reason": True,
                    "created_at": time.time(),
                }
            ],
            "history": [],
            "files": files or None,
        }
    return {
        "content": content,
        "history": [],
        "files": files,
    }


def _response_content(response: dict[str, Any]) -> str:
    if isinstance(response.get("content"), str):
        return response["content"]
    choices = response.get("choices")
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") if isinstance(choices[0], dict) else None
        if isinstance(message, dict) and isinstance(message.get("content"), str):
            return message["content"]
    return ""


def _event_for_work(work: SessionReplyWorkItem, response: dict[str, Any], *, error: bool = False) -> dict[str, Any]:
    source = {
        SessionReplyWorkType.FOREGROUND_REPLY: "foreground",
        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION: "confirmed_tool_execution",
        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY: "background_task",
        SessionReplyWorkType.SCHEDULED_TASK_SUMMARY: "scheduled_task",
    }[work.work_type]
    event = {
        "event_id": build_session_reply_work_event_id(work, error=error),
        "type": "proactive_reply_error" if error else "proactive_reply",
        "source": source,
        "session_id": work.session_id,
        "work_id": work.id,
        "content": _response_content(response),
        "history": response.get("history", []),
        "files": response.get("files", []),
    }
    if work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
        event["task_id"] = int(work.source_id)
        event["background_task_id"] = int(work.source_id)
    elif work.work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY:
        event["trigger_message_id"] = int(work.source_id)
    return event


async def mark_confirmed_execution_unknown(work_id: int, worker_id: str, error: str) -> None:
    async with AsyncSessionLocal() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.work_type != SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
            return
        claim_token = str((work.execution_state or {}).get("audit_claim_token") or "")
        if not claim_token:
            return
        await audit_crud.mark_execution_unknown(
            db,
            audit_record_id=int(work.source_id),
            claim_token=claim_token,
            error_reason=error,
        )
        await update_confirmation_message_status(db, audit_record_id=int(work.source_id))


async def _execute_foreground(db, work: SessionReplyWorkItem, worker_id: str) -> dict[str, Any]:
    content, attachments, message_ids = await session_reply_queue_manager.freeze_foreground_input(db, work=work, worker_id=worker_id)
    initial_message = InternalMessage(
        id=message_ids[-1],
        role=MessageRole.USER,
        content=content,
        attachments=attachments or None,
    )
    await db.refresh(work)
    stream_requested = bool((work.execution_state or {}).get("stream_requested"))
    context_summary_events_requested = bool((work.execution_state or {}).get("context_summary_events_requested"))
    async with AsyncSessionLocal() as event_db:
        next_stream_sequence = await session_reply_stream_event_crud.get_latest_sequence(event_db, work_id=work.id) + 1

    async def publish_stream_event(event: dict[str, Any]) -> None:
        nonlocal next_stream_sequence
        persisted_event = {
            **event,
            "session_id": work.session_id,
            "work_id": work.id,
        }
        async with AsyncSessionLocal() as event_db:
            await session_reply_stream_event_crud.publish(
                event_db,
                work_id=work.id,
                sequence_no=next_stream_sequence,
                event=persisted_event,
            )
        next_stream_sequence += 1

    async def fetch_additional_user_messages() -> list[InternalMessage]:
        return await session_reply_queue_manager.absorb_contiguous_foreground_messages(
            db,
            work_id=work.id,
            worker_id=worker_id,
        )

    async def check_work_validity() -> bool:
        async with AsyncSessionLocal() as validity_db:
            active_claims = await session_reply_work_item_crud.get_active_claims(
                validity_db,
                {work.id: worker_id},
            )
            session = await session_crud.get_by_session_id(
                validity_db,
                work.session_id,
            )
        return (work.id, worker_id) in active_claims and session is not None and session.uid == work.uid and session.profile_id == work.profile_id

    async def save_execution_checkpoint(checkpoint: dict[str, Any]) -> None:
        state = {
            **(work.execution_state or {}),
            "dispatcher_checkpoint": checkpoint,
        }
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={"execution_state": state},
        )
        if not updated:
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT))
        work.execution_state = state

    execution_state = work.execution_state or {}
    expose_tool_call_content = bool(execution_state.get("expose_tool_call_content", True))
    resume_state = execution_state.get("dispatcher_checkpoint")
    if not isinstance(resume_state, dict) or not isinstance(resume_state.get("messages"), list):
        resume_state = None

    dispatch_kwargs = {
        "db": db,
        "message": content,
        "uid": work.uid,
        "session_id": work.session_id,
        "attachments": attachments or None,
        "session_source": str(execution_state.get("message_source") or "queue"),
        "persisted_initial_message": initial_message,
        "history_before_id": message_ids[0],
        "frozen_user_message_ids": message_ids,
        "final_message_dedupe_key": _result_message_dedupe_key(work),
        "persisted_profile_id": work.profile_id,
        "additional_user_messages_fetcher": fetch_additional_user_messages,
        "execution_resume_state": resume_state,
        "execution_checkpoint_callback": save_execution_checkpoint,
        "context_summary_work_validity_checker": check_work_validity,
        "expose_tool_call_content": expose_tool_call_content,
    }
    if not stream_requested:
        return await ChatDispatcher.dispatch(
            **dispatch_kwargs,
            context_summary_lifecycle_callback=publish_stream_event if context_summary_events_requested else None,
        )

    response = None
    async for event in ChatDispatcher.dispatch_stream(
        **dispatch_kwargs,
        context_summary_events_requested=context_summary_events_requested,
    ):
        event_type = event.get("type")
        if event_type == "done":
            response = event.get("response")
        elif event_type == "error":
            error_message = str(event.get("message") or t(ERR_LLM_UNEXPECTED_ERROR))
            raise RuntimeError(error_message)
        else:
            await publish_stream_event(event)
    if not isinstance(response, dict):
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
    return response


async def _execute_confirmed_tools(db, work: SessionReplyWorkItem) -> dict[str, Any]:
    audit_record_id = int(work.source_id)
    claim_token = str((work.execution_state or {}).get("audit_claim_token") or "")
    record = await audit_crud.get_record(db, audit_record_id)
    details = await audit_crud.list_tool_details(db, audit_record_id)
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
        ai_msg, turn_messages, files = await ChatDispatcher._generate_reply_from_history(
            db,
            uid=work.uid,
            session_id=work.session_id,
            profile=profile,
            call_context="confirmed_tool_source_invalid",
            allow_tools=False,
            extra_messages=[InternalMessage(role=MessageRole.USER, content=AUDIT_SOURCE_MESSAGE_INVALID_PROMPT)],
            reply_source="confirmed_tool_execution",
            final_message_dedupe_key=_result_message_dedupe_key(work),
        )
        content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
        return {"content": content, "history": dump_background_proactive_history(turn_messages), "files": files}

    cfg = await validate_profile_and_cfg(db, profile)
    files_changed = False
    try:
        for detail in details:
            for file_snapshot in detail.file_snapshots:
                expected_hash = file_snapshot.get("sha256")
                path = file_snapshot.get("resolved_path") or file_snapshot.get("absolute_path")
                if not path or not expected_hash:
                    continue
                current = create_file_integrity_snapshot(path, working_directory=record.working_directory)
                if current.sha256 != expected_hash or current.size != file_snapshot.get("size"):
                    files_changed = True
                    break
            if files_changed:
                break
    except Exception:
        files_changed = True

    if files_changed:
        await audit_crud.cancel_execution_for_file_reaudit(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
            error_reason="命令直接引用的文件已变化，原确认失效",
        )
        await update_confirmation_message_status(db, audit_record_id=audit_record_id)
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
        if not reaudit_round.may_execute:
            messages = [source_internal]
            turn_messages = [source_internal]
            for tool_result in reaudit_round.tool_results:
                await save_tool_response(
                    db,
                    work.session_id,
                    work.uid,
                    profile.id,
                    tool_result,
                    messages,
                    turn_messages,
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
                await update_confirmation_message_status(db, audit_record_id=reaudit_round.audit_record_id)
                turn_messages.append(InternalMessage(role=MessageRole.ASSISTANT, content=confirmation_content))
                return {
                    "content": confirmation_content,
                    "history": dump_background_proactive_history(turn_messages),
                    "files": [],
                }
            ai_msg, final_messages, files = await ChatDispatcher._generate_reply_from_history(
                db,
                uid=work.uid,
                session_id=work.session_id,
                profile=profile,
                call_context="confirmed_tool_file_reaudit_blocked",
                allow_tools=False,
                reply_source="confirmed_tool_execution",
                final_message_dedupe_key=_result_message_dedupe_key(work),
            )
            content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
            return {
                "content": content,
                "history": dump_background_proactive_history([*turn_messages, *final_messages]),
                "files": files,
            }
        record, claim_token = await audit_crud.claim_passed_for_execution(
            db,
            audit_record_id=reaudit_round.audit_record_id,
        )
        if record is None or claim_token is None:
            raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
        audit_record_id = reaudit_round.audit_record_id
        details = await audit_crud.list_tool_details(db, audit_record_id)

    _tools, allowed_knowledge_base_ids = await get_tools_for_profile(db, profile)
    detail_by_original_id = {detail.original_tool_call_id: detail for detail in details}
    confirmed_calls = [InternalToolCall(id=f"call_{uuid.uuid4().hex}", name=item.name, arguments=dict(item.arguments or {})) for item in source_tool_calls]
    confirmed_message = InternalMessage(role=MessageRole.ASSISTANT, tool_calls=confirmed_calls)
    await save_assistant_message(db, work.session_id, work.uid, profile.id, confirmed_message)
    messages = [confirmed_message]
    turn_messages = [confirmed_message]
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
    if not all_succeeded:
        cancellation_error = t(ERR_TOOL_ROUND_PRECHECK_FAILED) if precheck_errors else t(ERR_AUDIT_EXECUTION_CLAIM_FAILED)
        for execution in executions_by_original_call_id.values():
            await audit_crud.finish_execution_attempt(
                db,
                execution_record_id=execution.id,
                status=AuditExecutionStatus.CANCELLED,
                error=cancellation_error,
            )
        for confirmed_call in confirmed_calls:
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
            await save_tool_response(
                db,
                work.session_id,
                work.uid,
                profile.id,
                InternalMessage(
                    role=MessageRole.TOOL,
                    tool_call_id=confirmed_call.id,
                    content=error_content,
                ),
                messages,
                turn_messages,
            )
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
                audit_preapproved=True,
            )
            await save_tool_response(db, work.session_id, work.uid, profile.id, tool_result, messages, turn_messages)
            try:
                result_payload = json.loads(tool_result.content or "{}")
            except (TypeError, ValueError):
                result_payload = {}
            succeeded = not (isinstance(result_payload, dict) and (result_payload.get("error") or result_payload.get("status") == "failed" or (isinstance(result_payload.get("exit_code"), int) and result_payload["exit_code"] != 0)))
            all_succeeded = all_succeeded and succeeded
            await audit_crud.finish_execution_attempt(
                db,
                execution_record_id=execution.id,
                status=AuditExecutionStatus.SUCCEEDED if succeeded else AuditExecutionStatus.FAILED,
                result_summary=(tool_result.content or "")[:1000],
                error=None if succeeded else (tool_result.content or "")[:1000],
            )

    await audit_crud.finish_execution_round(
        db,
        audit_record_id=audit_record_id,
        claim_token=claim_token,
        status=AuditRecordStatus.SUCCEEDED if all_succeeded else AuditRecordStatus.FAILED,
        error_reason=None if all_succeeded else "一个或多个工具执行失败",
    )
    await update_confirmation_message_status(db, audit_record_id=audit_record_id)
    ai_msg, final_messages, files = await ChatDispatcher._generate_reply_from_history(
        db,
        uid=work.uid,
        session_id=work.session_id,
        profile=profile,
        call_context="confirmed_tool_final_reply",
        allow_tools=False,
        reply_source="confirmed_tool_execution",
        final_message_dedupe_key=_result_message_dedupe_key(work),
    )
    content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
    return {"content": content, "history": dump_background_proactive_history([*turn_messages, *final_messages]), "files": files}


def _load_background_submission_context(task: BackgroundTask) -> list[InternalMessage] | None:
    extra = task.extra if isinstance(task.extra, dict) else {}
    if "submission_context" not in extra:
        return None
    raw_context = extra.get("submission_context")
    if not isinstance(raw_context, list):
        return None
    return [InternalMessage.model_validate(message) for message in raw_context if isinstance(message, dict)]


def _last_frozen_user_message_id(messages: list[InternalMessage] | None) -> int | None:
    if not messages:
        return None
    return max(
        (message.id for message in messages if message.role == MessageRole.USER and message.id is not None),
        default=None,
    )


def _build_background_result_messages(task: BackgroundTask) -> list[InternalMessage]:
    task_result = task.result or {
        "status": task.status,
        "tool_name": task.tool_name,
        "error": task.error,
    }
    return [
        InternalMessage(
            role=MessageRole.TOOL,
            tool_call_id=task.tool_call_id,
            content=json.dumps(task_result, ensure_ascii=False),
        ),
        InternalMessage(
            role=MessageRole.USER,
            content=BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT,
        ),
    ]


async def _execute_background(db, work: SessionReplyWorkItem) -> dict[str, Any]:
    task = await background_task_crud.get(db, int(work.source_id))
    if task is None:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_NOT_FOUND))
    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE))
    submission_context = _load_background_submission_context(task)
    ai_msg, turn_messages, files = await ChatDispatcher._generate_reply_from_history(
        db,
        uid=work.uid,
        session_id=work.session_id,
        profile=profile,
        call_context="session_reply_background_summary",
        allow_tools=True,
        extra_messages=_build_background_result_messages(task) if submission_context is not None else None,
        submission_context=submission_context,
        initial_trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        initial_fixed_upper_message_id=_last_frozen_user_message_id(submission_context),
        reply_source="background_task",
        final_message_dedupe_key=_result_message_dedupe_key(work),
    )
    content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
    return {
        "content": content,
        "history": dump_background_proactive_history(turn_messages),
        "files": files,
    }


async def _execute_scheduled(db, work: SessionReplyWorkItem) -> dict[str, Any]:
    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        raise RuntimeError(t(ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND))
    ai_msg, turn_messages, files = await ChatDispatcher._generate_reply_from_history(
        db,
        uid=work.uid,
        session_id=work.session_id,
        profile=profile,
        call_context="session_reply_scheduled_summary",
        allow_tools=True,
        initial_trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        initial_fixed_upper_message_id=int(work.source_id),
        restrict_tools_to_background_allowlist=False,
        reply_source="scheduled_task",
        final_message_dedupe_key=_result_message_dedupe_key(work),
    )
    content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
    return {
        "content": content,
        "history": [message.model_dump(mode="json") for message in turn_messages],
        "files": files,
    }


async def execute_session_reply_work(work_id: int, worker_id: str) -> None:
    async with AsyncSessionLocal() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id:
            return

        persisted_result = await _get_persisted_result(db, work)
        if persisted_result is not None:
            response = (work.execution_state or {}).get("response") or _response_from_persisted_message(work, persisted_result)
        elif work.work_type == SessionReplyWorkType.FOREGROUND_REPLY:
            response = await _execute_foreground(db, work, worker_id)
        elif work.work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
            response = await _execute_confirmed_tools(db, work)
        elif work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
            response = await _execute_background(db, work)
        else:
            response = await _execute_scheduled(db, work)

        result_message = persisted_result or await message_crud.get_by_dedupe_key(db, _result_message_dedupe_key(work))
        if result_message is None:
            raise RuntimeError(t(ERR_SESSION_REPLY_FINAL_MESSAGE_NOT_PERSISTED))

        state = {**(work.execution_state or {}), "response": response}
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"result_message_id": result_message.id, "execution_state": state},
        )
        if not updated:
            return

    await send_session_event(work.uid, work.session_id, _event_for_work(work, response))

    async with AsyncSessionLocal() as db:
        if work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
            await db.execute(
                update(BackgroundTask)
                .where(BackgroundTask.id == int(work.source_id))
                .values(
                    reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
                    reply_locked_by=None,
                    reply_lock_until=None,
                )
            )
        await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=work_id,
            worker_id=worker_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            result_message_id=result_message.id,
            event_sent=True,
            commit=False,
        )
        await db.commit()


async def fail_session_reply_work(
    work_id: int,
    worker_id: str,
    error: str,
    *,
    user_error: str | None = None,
) -> None:
    async with AsyncSessionLocal() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id:
            return
        error_content = user_error or t(ERR_LLM_UNEXPECTED_ERROR)
        error_message = await save_message(
            db,
            work.session_id,
            work.uid,
            MessageRole.ERR,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ERR, content=error_content),
            work.profile_id,
            is_processed=True,
            dedupe_key=_error_message_dedupe_key(work),
        )

    await send_session_event(work.uid, work.session_id, _event_for_work(work, {"content": error_content}, error=True))

    async with AsyncSessionLocal() as db:
        if work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
            await db.execute(
                update(BackgroundTask)
                .where(BackgroundTask.id == int(work.source_id))
                .values(
                    reply_status=BackgroundTaskReplyStatus.FAILED,
                    reply_locked_by=None,
                    reply_lock_until=None,
                    error=error,
                )
            )
        await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=work_id,
            worker_id=worker_id,
            status=SessionReplyWorkStatus.FAILED,
            result_message_id=error_message.id,
            error=error,
            event_sent=True,
            commit=False,
        )
        await db.commit()


def retry_delay_seconds(attempt_count: int) -> int:
    return min(300, 2 ** max(0, attempt_count - 1))


def lease_deadline() -> int:
    return int(time.time())
