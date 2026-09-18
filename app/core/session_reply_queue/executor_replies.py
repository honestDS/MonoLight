import json
import time
from functools import partial
from typing import Any

from app.core.constants import (
    ERR_BACKGROUND_TASK_NOT_FOUND,
    ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE,
    ERR_LLM_UNEXPECTED_ERROR,
    ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND,
)
from app.core.crud.profile.profile import profile_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.prompts import BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT
from app.core.session_reply_queue.executor_audit import _persist_work_audit_execution_binding
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.session_reply_queue.executor_interactive import _dispatch_interactive_work
from app.core.session_reply_queue.executor_metadata import _generate_reply_with_request_metadata
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.assistant_files import parse_assistant_files_content
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.helpers import dump_background_proactive_history, dump_output_history
from app.models.background_task import BackgroundTask
from app.models.message import InternalMessage, MessageRole
from app.models.session_reply_work_item import SessionReplyWorkItem

__all__ = []


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
        content = parse_assistant_files_content(ai_msg.content)
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
            "history": dump_output_history(
                turn_messages,
                show_tool_calls=bool(execution_state.get("show_tool_calls", True)),
            ),
            "files": files or None,
        }
        if llm_request_metadata is not None:
            response["llm_request_metadata"] = llm_request_metadata
        return response

    resume_state = execution_state.get("dispatcher_checkpoint")
    if not isinstance(resume_state, dict) or not isinstance(resume_state.get("messages"), list):
        resume_state = None
    return await _dispatch_interactive_work(
        db,
        work=work,
        worker_id=worker_id,
        message=content,
        initial_message=initial_message,
        history_before_id=message_ids[0],
        frozen_user_message_ids=message_ids,
        attachments=attachments or None,
        allow_additional_user_messages=True,
        execution_resume_state=resume_state,
    )


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
    content = parse_assistant_files_content(ai_msg.content)
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
    content = parse_assistant_files_content(ai_msg.content)
    response = {
        "content": content,
        "history": dump_background_proactive_history(turn_messages),
        "files": files,
    }
    if llm_request_metadata is not None:
        response["llm_request_metadata"] = llm_request_metadata
    return response
