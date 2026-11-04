import json
import time
from typing import Any

from app.core.constants import (
    END_SESSION_SCOPE_FOREGROUND,
    ERR_BACKGROUND_TASK_NOT_FOUND,
    ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE,
    ERR_LLM_UNEXPECTED_ERROR,
)
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.message import message_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.prompts import BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT
from app.core.session_reply_queue.executor_agent_work import execute_agent_work
from app.core.session_reply_queue.executor_audit import _persist_work_audit_execution_binding
from app.core.session_reply_queue.executor_common import _response_content, _result_message_dedupe_key
from app.core.session_reply_queue.executor_interactive import _dispatch_interactive_work
from app.core.session_reply_queue.executor_metadata import _generate_auxiliary_reply_with_request_metadata
from app.core.session_reply_queue.executor_resume import restore_interactive_tool_replies
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.assistant_files import parse_assistant_files_content
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.frozen_context import capture_frozen_context_messages, load_frozen_context_messages, serialize_frozen_context_messages
from app.core.utils.dispatcher.helpers import dump_background_proactive_history, dump_output_history
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.message import InternalMessage, InternalToolCall, MessageRole, MessageType, decode_message_content
from app.models.provider_usage import ProviderUsageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkType

__all__ = []


async def _execute_foreground(db, work: SessionReplyWorkItem, worker_id: str) -> dict[str, Any]:
    content, attachments, message_ids = await session_reply_queue_manager.freeze_foreground_input(db, work=work, worker_id=worker_id)
    await db.refresh(work)
    execution_state = work.execution_state or {}
    initial_message = InternalMessage.from_user_input(
        id=message_ids[-1],
        content=content,
        attachments=attachments or None,
        guidance_prompt=execution_state.get("guidance_prompt"),
    )
    if bool((work.execution_state or {}).get("audit_decision_response")):
        profile = await profile_crud.get_with_relations(db, work.profile_id)
        if profile is None or profile.uid != work.uid:
            raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
        ai_msg, turn_messages, files, llm_request_metadata = await _generate_auxiliary_reply_with_request_metadata(
            db,
            work=work,
            request_purpose=ProviderUsageType.AUDIT_AUXILIARY,
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
    elif resume_state.get("reload_history") is not True:
        resume_state = await restore_interactive_tool_replies(
            db,
            work=work,
            worker_id=worker_id,
            checkpoint=resume_state,
        )
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


async def _execute_external_resume(db, work: SessionReplyWorkItem, worker_id: str) -> dict[str, Any]:
    if work.work_type == SessionReplyWorkType.AGENT_WORK:
        return await execute_agent_work(db, work, worker_id)
    if work.work_type == SessionReplyWorkType.BACKGROUND_TASK_RESUME:
        return await _execute_background_task_resume(db, work, worker_id)

    execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
    checkpoint = execution_state.get("dispatcher_checkpoint")
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("messages"), list):
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))

    message_ids = work.input_message_ids
    if not message_ids:
        message_ids = [execution_state.get("decision_message_id")]
    if not isinstance(message_ids, list) or not message_ids or any(not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0 for message_id in message_ids) or len(message_ids) != len(set(message_ids)):
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))

    message = await message_crud.get(db, message_ids[-1])
    if message is None or message.uid != work.uid or message.session_id != work.session_id or message.profile_id != work.profile_id or message.role != MessageRole.USER or message.type not in (MessageType.TEXT, MessageType.AUDIT_DECISION):
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))

    initial_message = InternalMessage.from_user_input(
        id=message.id,
        content=decode_message_content(message.content, message.content_format),
        attachments=message.attachments,
        environment_prompt=message.environment_prompt,
        guidance_prompt=message.guidance_prompt,
    )
    execution_scope = execution_state.get("execution_scope")
    frozen_context_messages = load_frozen_context_messages(execution_state.get("frozen_context_messages"))
    return await _dispatch_interactive_work(
        db,
        work=work,
        worker_id=worker_id,
        message=initial_message.content or "",
        initial_message=initial_message,
        history_before_id=message_ids[0],
        frozen_user_message_ids=message_ids,
        attachments=initial_message.attachments,
        allow_additional_user_messages=(execution_scope == END_SESSION_SCOPE_FOREGROUND and frozen_context_messages is None),
        execution_resume_state=checkpoint,
        execution_scope=execution_scope,
        frozen_context_messages=frozen_context_messages,
    )


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


def _load_background_request_context(task) -> list[InternalMessage]:
    extra = task.extra if isinstance(task.extra, dict) else {}
    raw_context = extra.get("request_context")
    if not isinstance(raw_context, list):
        return []
    return [InternalMessage.model_validate(message) for message in raw_context if isinstance(message, dict)]


def _background_context_boundary(task, request_context: list[InternalMessage]) -> int | None:
    extra = task.extra if isinstance(task.extra, dict) else {}
    boundary = extra.get("context_summary_user_boundary_message_id")
    if isinstance(boundary, int) and not isinstance(boundary, bool) and boundary > 0:
        return boundary
    return max((message.id for message in request_context if message.role == MessageRole.USER and message.id is not None), default=None)


def _build_background_summary_messages(task) -> list[InternalMessage]:
    task_result = task.result or {"status": task.status, "tool_name": task.tool_name, "error": task.error}
    return [
        InternalMessage(
            role=MessageRole.ASSISTANT,
            tool_calls=[
                InternalToolCall(
                    id=task.tool_call_id,
                    name=task.tool_name,
                    arguments=dict(task.arguments) if isinstance(task.arguments, dict) else {},
                )
            ],
        ),
        InternalMessage(role=MessageRole.TOOL, tool_call_id=task.tool_call_id, content=json.dumps(task_result, ensure_ascii=False)),
        InternalMessage(role=MessageRole.USER, content=BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT),
    ]


async def _execute_background(db, work: SessionReplyWorkItem, worker_id: str = "") -> dict[str, Any]:
    """Summarize one completed background tool result without exposing tools."""
    task = await background_task_crud.get(db, int(work.source_id))
    if task is None:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_NOT_FOUND))
    if task.uid != work.uid or task.session_id != work.session_id or task.profile_id != work.profile_id:
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE))

    request_context = _load_background_request_context(task)
    ai_msg, turn_messages, files, llm_request_metadata = await _generate_auxiliary_reply_with_request_metadata(
        db,
        work=work,
        request_purpose=ProviderUsageType.BACKGROUND_SUMMARY,
        uid=work.uid,
        session_id=work.session_id,
        profile=profile,
        call_context="session_reply_background_summary",
        allow_tools=False,
        extra_messages=_build_background_summary_messages(task),
        submission_context=request_context,
        initial_trigger_mode=ContextSummaryTriggerMode.USER_MESSAGE,
        initial_fixed_upper_message_id=_background_context_boundary(task, request_context),
        reply_source="background_task",
        final_message_dedupe_key=_result_message_dedupe_key(work),
    )
    response = {
        "content": parse_assistant_files_content(ai_msg.content),
        "history": dump_background_proactive_history(turn_messages),
        "files": files,
    }
    if llm_request_metadata is not None:
        response["llm_request_metadata"] = llm_request_metadata
    return response


async def _execute_background_task_resume(db, work: SessionReplyWorkItem, worker_id: str = "") -> dict[str, Any]:
    """使用主对话循环处理后台任务结果并绑定可恢复的执行状态。"""
    task = await background_task_crud.get(db, int(work.source_id))
    if task is None:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_NOT_FOUND))
    if task.uid != work.uid or task.session_id != work.session_id or task.profile_id != work.profile_id:
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
    profile = await profile_crud.get_with_relations(db, work.profile_id)
    if profile is None or profile.uid != work.uid:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE))

    request_context = _load_background_request_context(task)
    task_extra = task.extra if isinstance(task.extra, dict) else {}
    frozen_context_messages = load_frozen_context_messages(task_extra.get("frozen_context_messages"))
    if frozen_context_messages is None:
        boundary = _background_context_boundary(task, request_context)
        if boundary is not None:
            frozen_context_messages = load_frozen_context_messages(
                await capture_frozen_context_messages(
                    db,
                    session_id=work.session_id,
                    uid=work.uid,
                    before_id=boundary + 1,
                )
            )
        else:
            frozen_context_messages = [message.model_copy(deep=True) for message in request_context]

    execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
    if "frozen_context_messages" not in execution_state:
        execution_state = {
            **execution_state,
            "frozen_context_messages": serialize_frozen_context_messages(frozen_context_messages),
        }
        work.execution_state = execution_state
    resume_state = execution_state.get("dispatcher_checkpoint")
    if not isinstance(resume_state, dict) or not isinstance(resume_state.get("messages"), list):
        resume_state = None

    history_before_id: int | None = None
    frozen_user_message_ids: list[int] = []
    if resume_state is not None and resume_state.get("deferred_external_results") is True:
        external_resume_context = execution_state.get("external_resume_context")
        if not isinstance(external_resume_context, list) or not external_resume_context or not isinstance(external_resume_context[0], dict):
            raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
        payload = external_resume_context[0]
        initial_message = InternalMessage(
            role=MessageRole.USER,
            content=json.dumps(payload, ensure_ascii=False),
        )
    else:
        result_event = await message_crud.get_by_dedupe_key(db, f"background-task:{int(work.source_id)}:result")
        if result_event is None or result_event.id is None or result_event.uid != work.uid or result_event.session_id != work.session_id or result_event.profile_id != work.profile_id or result_event.type != MessageType.BACKGROUND_TASK_RESULT:
            raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))

        parsed_events = parse_db_messages_to_internal([result_event])
        initial_message = next((message for message in parsed_events if message.role == MessageRole.USER), None)
        if initial_message is None:
            raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
        history_before_id = result_event.id
        frozen_user_message_ids = [result_event.id]

    if resume_state is None:
        resume_state = {
            "messages": [],
            "turn_messages": [],
            "reload_history": True,
            "context_summary_trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE.value,
            "context_summary_fixed_upper_message_id": history_before_id,
        }
    elif resume_state.get("reload_history") is not True:
        resume_state = await restore_interactive_tool_replies(
            db,
            work=work,
            worker_id=worker_id,
            checkpoint=resume_state,
        )

    interactive_response = await _dispatch_interactive_work(
        db,
        work=work,
        worker_id=worker_id,
        message=initial_message.content or "",
        initial_message=initial_message,
        history_before_id=history_before_id,
        frozen_user_message_ids=frozen_user_message_ids,
        attachments=initial_message.attachments,
        allow_additional_user_messages=False,
        execution_resume_state=resume_state,
        execution_scope=END_SESSION_SCOPE_FOREGROUND,
        frozen_context_messages=frozen_context_messages,
    )

    if interactive_response.get("status") == "waiting_external_work" or interactive_response.get("skip_reply") is True:
        return interactive_response

    history = interactive_response.get("history")
    history_messages = [InternalMessage.model_validate(message) for message in history] if isinstance(history, list) else []
    response = {
        "content": parse_assistant_files_content(_response_content(interactive_response)),
        "history": dump_background_proactive_history(history_messages),
        "files": interactive_response.get("files") or [],
    }
    if interactive_response.get("llm_request_metadata") is not None:
        response["llm_request_metadata"] = interactive_response["llm_request_metadata"]
    return response
