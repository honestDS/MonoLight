import hashlib
import json
import time
from collections import Counter, deque
from functools import partial
from typing import Any

from app.core.constants import (
    ERR_BACKGROUND_TASK_NOT_FOUND,
    ERR_BACKGROUND_TASK_PROFILE_UNAVAILABLE,
    ERR_LLM_UNEXPECTED_ERROR,
    ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND,
)
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.prompts import BACKGROUND_TASK_RESULT_INSTRUCTION_PROMPT, PROMPT_TOOL_INTERRUPTED
from app.core.session_reply_queue.executor_audit import _persist_work_audit_execution_binding
from app.core.session_reply_queue.executor_common import _result_message_dedupe_key
from app.core.session_reply_queue.executor_interactive import (
    _dispatch_interactive_work,
    _save_interactive_work_execution_checkpoint,
)
from app.core.session_reply_queue.executor_metadata import _generate_auxiliary_reply_with_request_metadata
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.assistant_files import parse_assistant_files_content
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.helpers import dump_background_proactive_history, dump_output_history
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTask
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.session_reply_provider_usage import SessionReplyProviderRequestPurpose
from app.models.session_reply_work_item import SessionReplyWorkItem

__all__ = []


async def _restore_foreground_tool_replies(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    checkpoint: dict[str, Any],
) -> dict[str, Any]:
    messages = [InternalMessage.model_validate(message) for message in checkpoint["messages"]]

    def assistant_identity(message: InternalMessage, from_end: int) -> tuple[str, int]:
        if message.id is not None:
            return ("message_id", message.id)
        return ("assistant_from_end", from_end)

    assistant_indexes = [index for index, message in enumerate(messages) if message.role == MessageRole.ASSISTANT]
    assistant_from_end = {index: ordinal for ordinal, index in enumerate(reversed(assistant_indexes), start=1)}
    groups: list[dict[str, Any]] = []
    checkpoint_result_row_ids: set[int] = set()
    current_group = None
    for index, message in enumerate(messages):
        if message.role == MessageRole.ASSISTANT:
            current_group = {
                "identity": assistant_identity(message, assistant_from_end[index]),
                "assistant": message,
                "existing_counts": Counter(),
                "existing_without_id_counts": Counter(),
                "missing": [],
            }
            groups.append(current_group)
        elif current_group is not None and message.role == MessageRole.TOOL:
            if message.id is not None:
                checkpoint_result_row_ids.add(message.id)
            if message.tool_call_id is not None:
                current_group["existing_counts"][message.tool_call_id] += 1
                if message.id is None:
                    current_group["existing_without_id_counts"][message.tool_call_id] += 1

    for group in groups:
        remaining_existing = Counter(group["existing_counts"])
        for call_index, tool_call in enumerate(group["assistant"].tool_calls or []):
            if remaining_existing[tool_call.id] > 0:
                remaining_existing[tool_call.id] -= 1
                continue
            group["missing"].append((call_index, tool_call))

    if not any(group["missing"] for group in groups):
        return checkpoint

    await ensure_session_reply_work_claim(db)
    restored_results: dict[tuple[tuple[str, int], int], InternalMessage] = {}
    for group in groups:
        assistant = group["assistant"]
        if not group["missing"] or assistant.id is None:
            continue

        pending_by_id: dict[str, deque[int]] = {}
        for call_index, tool_call in group["missing"]:
            pending_by_id.setdefault(tool_call.id, deque()).append(call_index)
        no_id_results_to_skip = Counter(group["existing_without_id_counts"])
        cursor = None
        while any(pending_by_id.values()):
            page = await message_crud.get_history_forward_by_id(
                db,
                session_id=work.session_id,
                uid=work.uid,
                after_id=assistant.id,
                page_after_id=cursor,
                limit=200,
            )
            if not page:
                break

            tool_result_rows = []
            reached_next_assistant = False
            # 只在当前assistant区间内恢复，遇到下一条assistant即停止。
            for row in page:
                if row.role == MessageRole.ASSISTANT:
                    reached_next_assistant = True
                    break
                if row.role == MessageRole.TOOL and row.type == MessageType.TOOL_RESULT:
                    tool_result_rows.append(row)

            rows_by_id = {row.id: row for row in tool_result_rows}
            for result in parse_db_messages_to_internal(tool_result_rows):
                row = rows_by_id.get(result.id)
                if row is None or row.id in checkpoint_result_row_ids:
                    continue
                call_id = result.tool_call_id
                if no_id_results_to_skip[call_id] > 0:
                    no_id_results_to_skip[call_id] -= 1
                    continue
                pending = pending_by_id.get(call_id)
                if not pending:
                    continue
                call_index = pending.popleft()
                result.created_at = row.created_at.timestamp()
                restored_results[(group["identity"], call_index)] = result

            if reached_next_assistant:
                break
            cursor = page[-1].id

    # 全部区间扫描完成后再生成未知结果，避免后续区间误认新写入行。
    for group in groups:
        for call_index, tool_call in group["missing"]:
            result_key = (group["identity"], call_index)
            if result_key in restored_results:
                continue
            tool_result = InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=tool_call.id,
                tool_execution_status="execution_unknown",
                content=json.dumps(
                    {"status": "execution_unknown", "error": PROMPT_TOOL_INTERRUPTED},
                    ensure_ascii=False,
                ),
            )
            dedupe_payload = [
                _result_message_dedupe_key(work),
                group["identity"],
                call_index,
                tool_call.id,
            ]
            saved = await save_message(
                db,
                work.session_id,
                work.uid,
                MessageRole.TOOL,
                MessageType.TOOL_RESULT,
                tool_result,
                work.profile_id,
                is_processed=True,
                commit=False,
                dedupe_key=hashlib.sha256(json.dumps(dedupe_payload, ensure_ascii=False).encode()).hexdigest(),
            )
            tool_result.id = saved.id
            tool_result.created_at = saved.created_at
            restored_results[result_key] = tool_result

    def insert_results(raw_messages: list[Any]) -> list[Any]:
        parsed_messages = [InternalMessage.model_validate(message) for message in raw_messages]
        assistant_indexes = [index for index, message in enumerate(parsed_messages) if message.role == MessageRole.ASSISTANT]
        assistant_from_end = {index: ordinal for ordinal, index in enumerate(reversed(assistant_indexes), start=1)}
        output = []
        index = 0
        while index < len(parsed_messages):
            message = parsed_messages[index]
            output.append(raw_messages[index])
            index += 1
            if message.role != MessageRole.ASSISTANT:
                continue
            identity = assistant_identity(message, assistant_from_end[index - 1])
            while index < len(parsed_messages) and parsed_messages[index].role == MessageRole.TOOL:
                output.append(raw_messages[index])
                index += 1
            for call_index, _ in enumerate(message.tool_calls or []):
                result = restored_results.get((identity, call_index))
                if result is not None:
                    output.append(result.model_dump(mode="json"))
        return output

    new_checkpoint = {
        **checkpoint,
        "messages": insert_results(checkpoint["messages"]),
    }
    turn_messages = checkpoint.get("turn_messages")
    if isinstance(turn_messages, list):
        new_checkpoint["turn_messages"] = insert_results(turn_messages)
    await _save_interactive_work_execution_checkpoint(
        db,
        new_checkpoint.copy(),
        work=work,
        worker_id=worker_id,
    )
    return new_checkpoint


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
            request_purpose=SessionReplyProviderRequestPurpose.AUDIT_AUXILIARY,
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
    else:
        resume_state = await _restore_foreground_tool_replies(
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
    ai_msg, turn_messages, files, llm_request_metadata = await _generate_auxiliary_reply_with_request_metadata(
        db,
        work=work,
        request_purpose=SessionReplyProviderRequestPurpose.BACKGROUND_SUMMARY,
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

    ai_msg, turn_messages, files, llm_request_metadata = await _generate_auxiliary_reply_with_request_metadata(
        db,
        work=work,
        request_purpose=SessionReplyProviderRequestPurpose.SCHEDULED_SUMMARY,
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
