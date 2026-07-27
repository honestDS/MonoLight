import asyncio
import json
import socket
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Any

from sqlalchemy import update

from app.core.audit.confirmation import (
    get_pending_tool_results,
    notify_confirmation_tool_results,
    replace_pending_tool_result,
    update_confirmation_message_status,
)
from app.core.audit.integrity import create_file_integrity_snapshot, verify_file_integrity_snapshot, verify_persisted_tool_round
from app.core.audit.service import audit_tool_round, is_audit_configured
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
    SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY,
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
from app.core.session_reply_queue.manager import (
    build_session_reply_work_event_id,
    build_session_reply_work_identity,
    get_work_request_ids,
    session_reply_queue_manager,
)
from app.core.tools import get_tools_for_profile
from app.core.utils.assistant_files import parse_assistant_files_content
from app.core.utils.background_task_result import sanitize_execution_summary
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.helpers import dump_background_proactive_history, dump_output_history
from app.core.utils.dispatcher.process_single_tool import get_queued_background_task_id, prevalidate_tool_round, process_single_tool
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.core.utils.dispatcher.validate_profile_and_cfg import validate_profile_and_cfg
from app.core.utils.request_token_baseline import extract_session_total_output_tokens
from app.models.audit import AuditExecutionStatus, AuditRecordStatus
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
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
        "request_ids": get_work_request_ids(work),
    }
    if response.get("llm_request_metadata") is not None:
        event["llm_request_metadata"] = response["llm_request_metadata"]
    if work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
        event["task_id"] = int(work.source_id)
        event["background_task_id"] = int(work.source_id)
    elif work.work_type == SessionReplyWorkType.SCHEDULED_TASK_SUMMARY:
        event["trigger_message_id"] = int(work.source_id)
    return event


def _metadata_with_work_order(
    work: SessionReplyWorkItem,
    metadata: Any,
    event_sequence_no: int | None = None,
) -> Any:
    if not isinstance(metadata, dict):
        return metadata
    ordered_metadata = {
        **metadata,
        "work_id": work.id,
        "work_sequence_no": work.sequence_no,
    }
    if event_sequence_no is not None:
        ordered_metadata["event_sequence_no"] = event_sequence_no
    return ordered_metadata


@dataclass
class _ForegroundStreamEventState:
    work: SessionReplyWorkItem
    next_sequence: int
    dequeued_request_ids: set[str]


async def _persist_foreground_stream_event(
    stream_state: _ForegroundStreamEventState,
    event: dict[str, Any],
) -> None:
    work = stream_state.work
    persisted_event = {
        **event,
        "session_id": work.session_id,
        "work_id": work.id,
        "event_sequence_no": stream_state.next_sequence,
    }
    async with AsyncSessionLocal() as event_db:
        if persisted_event["type"] == "llm_request_metadata":
            persisted_event = _metadata_with_work_order(
                work,
                persisted_event,
                event_sequence_no=stream_state.next_sequence,
            )
            await session_crud.update_llm_request_metadata(
                event_db,
                session_id=work.session_id,
                uid=work.uid,
                metadata=persisted_event,
                commit=False,
            )
        await session_reply_stream_event_crud.publish(
            event_db,
            work_id=work.id,
            sequence_no=stream_state.next_sequence,
            event=persisted_event,
            commit=False,
        )
        await event_db.commit()
    stream_state.next_sequence += 1


async def _publish_foreground_stream_event(
    db,
    stream_state: _ForegroundStreamEventState,
    event: dict[str, Any],
) -> None:
    work = stream_state.work
    if event.get("type") == "agent_loop_start":
        await db.refresh(work)
        request_ids = [request_id for request_id in get_work_request_ids(work) if request_id not in stream_state.dequeued_request_ids]
        if request_ids:
            await _persist_foreground_stream_event(
                stream_state,
                {
                    "type": "input_dequeued",
                    "session_id": work.session_id,
                    "work_id": work.id,
                    "request_ids": request_ids,
                },
            )
            stream_state.dequeued_request_ids.update(request_ids)
    await _persist_foreground_stream_event(stream_state, event)


async def _fetch_additional_foreground_user_messages(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> UserInputBatch | None:
    return await session_reply_queue_manager.absorb_contiguous_foreground_messages(
        db,
        work_id=work.id,
        worker_id=worker_id,
    )


async def _check_foreground_work_validity(
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> bool:
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


async def _save_foreground_execution_checkpoint(
    db,
    checkpoint: dict[str, Any],
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> None:
    active_audit_execution_present = SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint
    active_audit_execution = checkpoint.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
    state = {
        **(work.execution_state or {}),
        "dispatcher_checkpoint": checkpoint,
    }
    if active_audit_execution_present:
        if active_audit_execution is None:
            state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
        else:
            state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = active_audit_execution
    updated = await session_reply_work_item_crud.update_claimed(
        db,
        work_id=work.id,
        worker_id=worker_id,
        values={"execution_state": state},
    )
    if not updated:
        raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT))
    work.execution_state = state


async def _persist_work_audit_execution_binding(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    binding: dict[str, Any] | None,
) -> None:
    """持久化后台回复工作当前的审计执行绑定。"""
    state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
    if binding is None:
        state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
    else:
        state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = dict(binding)
    updated = await session_reply_work_item_crud.update_claimed(
        db,
        work_id=work.id,
        worker_id=worker_id,
        values={"execution_state": state},
    )
    if not updated:
        raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT))
    work.execution_state = state


async def _mark_audit_execution_unknown_reliably(
    db,
    *,
    audit_record_id: int,
    claim_token: str,
    error_reason: str,
) -> bool:
    marked = await audit_crud.mark_execution_unknown(
        db,
        audit_record_id=audit_record_id,
        claim_token=claim_token,
        error_reason=error_reason,
    )
    if not marked:
        marked = await audit_crud.finish_execution_round(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
            status=AuditRecordStatus.EXECUTION_UNKNOWN,
            error_reason=error_reason,
        )
    if marked:
        await update_confirmation_message_status(db, audit_record_id=audit_record_id)
    return marked


async def _mark_new_confirmed_execution_unknown_without_masking(
    db,
    *,
    audit_record_id: int,
    claim_token: str,
) -> None:
    try:
        await _mark_audit_execution_unknown_reliably(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
            error_reason=t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT),
        )
    except BaseException:
        pass


async def _persist_confirmed_work_audit_execution_binding(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    audit_record_id: int,
    claim_token: str,
) -> None:
    state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
    state["audit_claim_token"] = claim_token
    values = {
        "source_id": str(audit_record_id),
        "execution_state": state,
    }
    if worker_id:
        try:
            updated = await session_reply_work_item_crud.update_claimed(
                db,
                work_id=work.id,
                worker_id=worker_id,
                values=values,
            )
        except asyncio.CancelledError:
            await _mark_new_confirmed_execution_unknown_without_masking(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
            )
            raise
        except Exception:
            await _mark_new_confirmed_execution_unknown_without_masking(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
            )
            raise
        if not updated:
            await _mark_new_confirmed_execution_unknown_without_masking(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
            )
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT))
    work.source_id = str(audit_record_id)
    work.execution_state = state


def get_bound_audit_execution(work: SessionReplyWorkItem) -> tuple[int, str] | None:
    """读取回复工作中可恢复的审计整轮绑定。"""
    state_value = getattr(work, "execution_state", None)
    state = state_value if isinstance(state_value, dict) else {}
    work_type = getattr(work, "work_type", None)
    if work_type in {
        SessionReplyWorkType.FOREGROUND_REPLY,
        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
        SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
    }:
        binding = state.get(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY)
        if not isinstance(binding, dict):
            return None
        audit_record_id = binding.get("audit_record_id")
        claim_token = binding.get("claim_token")
    elif work_type == SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
        audit_record_id = getattr(work, "source_id", None)
        claim_token = state.get("audit_claim_token")
    else:
        return None

    try:
        audit_record_id = int(audit_record_id)
    except (TypeError, ValueError):
        return None
    if audit_record_id <= 0 or not isinstance(claim_token, str) or not claim_token:
        return None
    return audit_record_id, claim_token


def work_has_active_audit_execution(work: SessionReplyWorkItem) -> bool:
    """判断回复工作是否持有需要禁止自动重试的活动审计绑定。"""
    if getattr(work, "work_type", None) not in {
        SessionReplyWorkType.FOREGROUND_REPLY,
        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
        SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
    }:
        return False
    state_value = getattr(work, "execution_state", None)
    state = state_value if isinstance(state_value, dict) else {}
    return SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in state


async def mark_work_audit_execution_unknown(work_id: int, worker_id: str, error: str) -> None:
    """将中断回复工作绑定的审计整轮标记为结果未知。"""
    async with AsyncSessionLocal() as db:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None:
            return
        binding = get_bound_audit_execution(work)
        if binding is None:
            return
        audit_record_id, claim_token = binding
        background_tasks = await background_task_crud.list_by_audit_record(db, audit_record_id)
        active_background_execution_ids = {task.audit_execution_record_id for task in background_tasks if task.status in {BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING} and task.audit_execution_record_id is not None}
        if active_background_execution_ids:
            await audit_crud.mark_running_executions_unknown_except(
                db,
                audit_record_id=audit_record_id,
                claim_token=claim_token,
                excluded_execution_record_ids=active_background_execution_ids,
                error_reason=error,
            )
            return
        await _mark_audit_execution_unknown_reliably(
            db,
            audit_record_id=audit_record_id,
            claim_token=claim_token,
            error_reason=error,
        )


def _confirmed_file_snapshots_changed(details: list[Any], *, working_directory: str) -> bool:
    """检查已确认工具引用的文件快照是否发生变化。"""
    try:
        for detail in details:
            for file_snapshot in detail.file_snapshots:
                path = file_snapshot.get("absolute_path")
                if not isinstance(path, str):
                    return True
                current = create_file_integrity_snapshot(path, working_directory=working_directory)
                if not verify_file_integrity_snapshot(file_snapshot, current):
                    return True
    except Exception:
        return True
    return False


async def _generate_reply_with_request_metadata(
    db,
    *,
    work: SessionReplyWorkItem,
    **kwargs: Any,
) -> tuple[InternalMessage, list[InternalMessage], list[dict[str, Any]], dict[str, Any] | None]:
    latest_request_metadata = None
    session_total_output_tokens = 0
    work_output_tokens = 0
    if "additional_system_prompt" not in kwargs:
        execution_state = getattr(work, "execution_state", None)
        additional_system_prompt = execution_state.get("additional_system_prompt") if isinstance(execution_state, dict) else None
        if isinstance(additional_system_prompt, str) and additional_system_prompt.strip():
            kwargs["additional_system_prompt"] = additional_system_prompt.strip()
    if "guidance_prompt" not in kwargs:
        execution_state = getattr(work, "execution_state", None)
        guidance_prompt = execution_state.get("guidance_prompt") if isinstance(execution_state, dict) else None
        if isinstance(guidance_prompt, str) and guidance_prompt.strip():
            kwargs["guidance_prompt"] = guidance_prompt
    if hasattr(db, "execute"):
        session = await session_crud.get_by_session_id(db, work.session_id)
        if session is not None:
            session_total_output_tokens = extract_session_total_output_tokens(session.llm_request_metadata)

    async def persist_request_metadata(metadata: dict[str, Any]) -> None:
        nonlocal latest_request_metadata, session_total_output_tokens, work_output_tokens
        output_tokens = metadata.get("output_tokens")
        if isinstance(output_tokens, int) and not isinstance(output_tokens, bool) and output_tokens >= 0:
            session_total_output_tokens += output_tokens
            work_output_tokens += output_tokens
        ordered_metadata = _metadata_with_work_order(
            work,
            {
                **metadata,
                "output_tokens": work_output_tokens,
                "total_output_tokens": session_total_output_tokens,
            },
        )
        await session_crud.update_llm_request_metadata(
            db,
            session_id=work.session_id,
            uid=work.uid,
            metadata=ordered_metadata,
            commit=False,
        )
        latest_request_metadata = ordered_metadata

    ai_msg, turn_messages, files = await ChatDispatcher._generate_reply_from_history(
        db,
        **kwargs,
        request_metadata_callback=persist_request_metadata,
    )
    return ai_msg, turn_messages, files, latest_request_metadata


async def _execute_foreground(db, work: SessionReplyWorkItem, worker_id: str) -> dict[str, Any]:
    content, attachments, message_ids = await session_reply_queue_manager.freeze_foreground_input(db, work=work, worker_id=worker_id)
    await db.refresh(work)
    execution_state = work.execution_state or {}
    initial_message = InternalMessage(
        id=message_ids[-1],
        role=MessageRole.USER,
        content=content,
        attachments=attachments or None,
        guidance_prompt=execution_state.get("guidance_prompt"),
    )
    if bool((work.execution_state or {}).get("audit_decision_response")):
        profile = await profile_crud.get_with_relations(db, work.profile_id)
        if profile is None or profile.uid != work.uid:
            raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
        ai_msg, turn_messages, files, llm_request_metadata = await _generate_reply_with_request_metadata(
            db,
            work=work,
            uid=work.uid,
            session_id=work.session_id,
            profile=profile,
            call_context="session_reply_audit_decision",
            allow_tools=False,
            reply_source="audit_decision",
            final_message_dedupe_key=_result_message_dedupe_key(work),
        )
        content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
        response = {
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
            "history": dump_output_history(turn_messages),
            "files": files or None,
        }
        if llm_request_metadata is not None:
            response["llm_request_metadata"] = llm_request_metadata
        return response
    stream_requested = bool((work.execution_state or {}).get("stream_requested"))
    context_summary_events_requested = bool((work.execution_state or {}).get("context_summary_events_requested"))
    async with AsyncSessionLocal() as event_db:
        next_stream_sequence = await session_reply_stream_event_crud.get_latest_sequence(event_db, work_id=work.id) + 1
    stream_state = _ForegroundStreamEventState(
        work=work,
        next_sequence=next_stream_sequence,
        dequeued_request_ids=set(),
    )

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
        "additional_user_messages_fetcher": partial(
            _fetch_additional_foreground_user_messages,
            db,
            work=work,
            worker_id=worker_id,
        ),
        "execution_resume_state": resume_state,
        "execution_checkpoint_callback": partial(
            _save_foreground_execution_checkpoint,
            db,
            work=work,
            worker_id=worker_id,
        ),
        "context_summary_work_validity_checker": partial(
            _check_foreground_work_validity,
            work=work,
            worker_id=worker_id,
        ),
        "expose_tool_call_content": expose_tool_call_content,
    }
    additional_system_prompt = execution_state.get("additional_system_prompt")
    if isinstance(additional_system_prompt, str) and additional_system_prompt.strip():
        dispatch_kwargs["additional_system_prompt"] = additional_system_prompt.strip()
    if not stream_requested:
        response = await ChatDispatcher.dispatch(
            **dispatch_kwargs,
            context_summary_lifecycle_callback=partial(
                _publish_foreground_stream_event,
                db,
                stream_state,
            )
            if context_summary_events_requested
            else None,
        )
        if isinstance(response.get("llm_request_metadata"), dict):
            response["llm_request_metadata"] = _metadata_with_work_order(work, response["llm_request_metadata"])
            await session_crud.update_llm_request_metadata(
                db,
                session_id=work.session_id,
                uid=work.uid,
                metadata=response["llm_request_metadata"],
                commit=False,
            )
        return response

    response = None
    async for event in ChatDispatcher.dispatch_stream(
        **dispatch_kwargs,
        context_summary_events_requested=context_summary_events_requested,
        raise_errors=True,
    ):
        event_type = event.get("type")
        if event_type == "done":
            response = event.get("response")
        elif event_type == "error":
            error_message = str(event.get("message") or t(ERR_LLM_UNEXPECTED_ERROR))
            raise RuntimeError(error_message)
        else:
            await _publish_foreground_stream_event(db, stream_state, event)
    if not isinstance(response, dict):
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
    return response


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
    content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
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
        return await _source_invalid_confirmed_tool_response(
            db,
            work=work,
            audit_record_id=audit_record_id,
            record=record,
            claim_token=claim_token,
            profile=profile,
        )

    cfg = await validate_profile_and_cfg(db, profile)
    files_changed = _confirmed_file_snapshots_changed(details, working_directory=record.working_directory)
    decision_message_id = (work.execution_state or {}).get("decision_message_id")
    pending_tool_results = await get_pending_tool_results(
        db,
        uid=work.uid,
        session_id=work.session_id,
        source_assistant_message_id=source_message.id,
        before_message_id=decision_message_id if isinstance(decision_message_id, int) else None,
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
                messages.append(stored_tool_result)
                turn_messages.append(stored_tool_result)
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
                    await notify_confirmation_tool_results(db, audit_record_id=audit_record_id)
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
            content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
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
            )
            await _append_confirmed_tool_result(replacement_state, original_call.id, tool_result)
            try:
                result_payload = json.loads(tool_result.content or "{}")
            except (TypeError, ValueError):
                result_payload = {}
            if get_queued_background_task_id(tool_result.content) is None:
                succeeded = not (isinstance(result_payload, dict) and (result_payload.get("error") or result_payload.get("status") == "failed" or (isinstance(result_payload.get("exit_code"), int) and result_payload["exit_code"] != 0)))
                all_succeeded = all_succeeded and succeeded
                await audit_crud.finish_execution_attempt(
                    db,
                    execution_record_id=execution.id,
                    status=AuditExecutionStatus.SUCCEEDED if succeeded else AuditExecutionStatus.FAILED,
                    result_summary=sanitize_execution_summary(tool_result.content, redact_text=True),
                    error=None if succeeded else sanitize_execution_summary(tool_result.content, redact_text=True),
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
            await notify_confirmation_tool_results(db, audit_record_id=audit_record_id)
    ai_msg, final_messages, files, llm_request_metadata = await _generate_reply_with_request_metadata(
        db,
        work=work,
        uid=work.uid,
        session_id=work.session_id,
        profile=profile,
        call_context="confirmed_tool_final_reply",
        allow_tools=False,
        reply_source="confirmed_tool_execution",
        final_message_dedupe_key=_result_message_dedupe_key(work),
    )
    content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
    response = {"content": content, "history": dump_background_proactive_history([*turn_messages, *final_messages]), "files": files}
    if llm_request_metadata is not None:
        response["llm_request_metadata"] = llm_request_metadata
    return response


def _load_background_submission_context(task: BackgroundTask) -> list[InternalMessage] | None:
    extra = task.extra if isinstance(task.extra, dict) else {}
    if "submission_context" not in extra:
        return None
    raw_context = extra.get("submission_context")
    if not isinstance(raw_context, list):
        return None
    return [InternalMessage.model_validate(message) for message in raw_context if isinstance(message, dict)]


def _fallback_last_frozen_user_message_id(messages: list[InternalMessage] | None) -> int | None:
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


async def _persist_work_audit_execution_binding_callback(
    binding: dict[str, Any] | None,
    *,
    db,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> None:
    await _persist_work_audit_execution_binding(
        db,
        work=work,
        worker_id=worker_id,
        binding=binding,
    )


async def _execute_background(db, work: SessionReplyWorkItem, worker_id: str = "") -> dict[str, Any]:
    """生成后台任务总结并绑定可恢复的审计执行状态。"""
    task = await background_task_crud.get(db, int(work.source_id))
    if task is None:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_NOT_FOUND))
    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE))

    submission_context = _load_background_submission_context(task)
    extra = task.extra if isinstance(task.extra, dict) else {}
    stored_boundary_message_id = extra.get("context_summary_user_boundary_message_id")
    initial_fixed_upper_message_id = stored_boundary_message_id if (isinstance(stored_boundary_message_id, int) and not isinstance(stored_boundary_message_id, bool) and stored_boundary_message_id > 0) else _fallback_last_frozen_user_message_id(submission_context)
    ai_msg, turn_messages, files, llm_request_metadata = await _generate_reply_with_request_metadata(
        db,
        work=work,
        uid=work.uid,
        session_id=work.session_id,
        profile=profile,
        call_context="session_reply_background_summary",
        allow_tools=True,
        extra_messages=_build_background_result_messages(task) if submission_context is not None else None,
        submission_context=submission_context,
        initial_trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        initial_fixed_upper_message_id=initial_fixed_upper_message_id,
        reply_source="background_task",
        final_message_dedupe_key=_result_message_dedupe_key(work),
        audit_execution_binding_callback=partial(
            _persist_work_audit_execution_binding_callback,
            db=db,
            work=work,
            worker_id=worker_id,
        ),
    )
    content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
    response = {
        "content": content,
        "history": dump_background_proactive_history(turn_messages),
        "files": files,
    }
    if llm_request_metadata is not None:
        response["llm_request_metadata"] = llm_request_metadata
    return response


async def _execute_scheduled(db, work: SessionReplyWorkItem, worker_id: str = "") -> dict[str, Any]:
    """生成定时任务总结并绑定可恢复的审计执行状态。"""
    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        raise RuntimeError(t(ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND))

    ai_msg, turn_messages, files, llm_request_metadata = await _generate_reply_with_request_metadata(
        db,
        work=work,
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
        audit_execution_binding_callback=partial(
            _persist_work_audit_execution_binding_callback,
            db=db,
            work=work,
            worker_id=worker_id,
        ),
    )
    content, _untrusted_files = parse_assistant_files_content(ai_msg.content)
    response = {
        "content": content,
        "history": [message.model_dump(mode="json") for message in turn_messages],
        "files": files,
    }
    if llm_request_metadata is not None:
        response["llm_request_metadata"] = llm_request_metadata
    return response


async def execute_session_reply_work(work_id: int, worker_id: str) -> None:
    """执行已领取的会话回复工作并完成结果投递。"""
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
            response = await _execute_confirmed_tools(db, work, worker_id)
        elif work.work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY:
            response = await _execute_background(db, work, worker_id)
        else:
            response = await _execute_scheduled(db, work, worker_id)

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
