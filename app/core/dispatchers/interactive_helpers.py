import asyncio
import json
from collections.abc import Awaitable, Callable, MutableSet
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.constants import (
    ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN,
    ERR_VALUE_MUST_BE_POSITIVE,
    SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY,
)
from app.core.crud.audit import audit_crud
from app.core.i18n import t
from app.core.tools.read_multimodal_file import parse_multimodal_file_read_result
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.dispatcher.fetch_and_merge_new_user_messages import fetch_and_merge_new_user_messages
from app.core.utils.dispatcher.helpers import process_single_tool_with_isolated_db
from app.core.utils.dispatcher.markdown_instruction import (
    append_environment_prompt_instruction,
    build_max_output_tokens_instruction,
)
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.core.utils.message_assembler import MessageAssembler
from app.models.message import InternalMessage, MessageRole


def _tool_result_succeeded(content: str | None) -> bool:
    try:
        payload = json.loads(content or "{}")
    except (TypeError, ValueError):
        return True
    if not isinstance(payload, dict):
        return True
    return not (payload.get("error") or payload.get("status") == "failed" or (isinstance(payload.get("exit_code"), int) and payload["exit_code"] != 0))


def collect_pending_multimodal_file_inputs(messages: list[InternalMessage]) -> list[dict[str, str]]:
    assistant_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].role == MessageRole.ASSISTANT),
        None,
    )
    if assistant_index is None or not messages[assistant_index].tool_calls:
        return []

    inputs: list[dict[str, str]] = []
    for tool_call in messages[assistant_index].tool_calls:
        if getattr(tool_call, "name", None) != "read_multimodal_file":
            continue
        for tool_response in messages[assistant_index + 1 :]:
            if tool_response.role != MessageRole.TOOL or tool_response.tool_call_id != tool_call.id:
                continue
            result = parse_multimodal_file_read_result(tool_response.content)
            if result is not None and result["modality"] == "image":
                inputs.append(
                    {
                        "path": result["path"],
                        "modality": result["modality"],
                        "message": result["message"],
                        "tool_call_id": tool_call.id,
                    }
                )
            break
    return inputs


def build_pending_multimodal_input_message(
    pending_inputs: list[dict[str, str]],
    *,
    image_understanding: bool,
    audio_understanding: bool,
    video_understanding: bool,
) -> InternalMessage | None:
    if not pending_inputs:
        return None
    paths = list(dict.fromkeys(item["path"] for item in pending_inputs))
    messages = list(dict.fromkeys(item["message"] for item in pending_inputs))
    return MessageAssembler.assemble(
        InternalMessage(
            role=MessageRole.USER,
            content="\n\n".join(messages),
            attachments=paths,
        ),
        image_understanding=image_understanding,
        audio_understanding=audio_understanding,
        video_understanding=video_understanding,
        is_history=False,
    )


@dataclass
class _AdditionalUserMessagesContext:
    db: AsyncSession
    session_id: str
    uid: str
    queue_managed: bool
    fetcher: Callable[[], Awaitable[UserInputBatch | list[InternalMessage] | None]] | None


def _normalize_additional_user_messages(
    user_messages: UserInputBatch | list[InternalMessage] | None,
) -> UserInputBatch | None:
    if isinstance(user_messages, UserInputBatch):
        return user_messages
    if not user_messages:
        return None

    source_message_ids: list[int] = []
    seen_message_ids: set[int] = set()
    for message in user_messages:
        message_id = message.id
        if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0:
            raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="message_id"))
        if message_id not in seen_message_ids:
            seen_message_ids.add(message_id)
            source_message_ids.append(message_id)
    return UserInputBatch(
        messages=tuple(user_messages),
        source_message_ids=tuple(source_message_ids),
    )


async def _fetch_additional_user_messages(
    context: _AdditionalUserMessagesContext,
    max_tokens: int,
) -> UserInputBatch | None:
    if context.fetcher is not None:
        new_user_batch = await context.fetcher()
    elif context.queue_managed:
        return None
    else:
        new_user_batch = await fetch_and_merge_new_user_messages(
            context.db,
            context.session_id,
            context.uid,
        )
    new_user_batch = _normalize_additional_user_messages(new_user_batch)
    if new_user_batch is None:
        return None
    max_tokens_instruction = build_max_output_tokens_instruction(max_tokens)
    for new_message in new_user_batch.messages:
        append_environment_prompt_instruction(new_message, max_tokens_instruction)
    return new_user_batch


@dataclass
class _ExecutionCheckpointState:
    callback: Callable[[dict[str, Any]], Awaitable[None]] | None
    turn_messages: list[InternalMessage]
    files_to_user: list[str]
    upper_message_id: int | None
    memory_recall_boundary_message_id: int | None = None
    memory_recall_status: str | None = None
    total_output_tokens: int = 0
    session_total_input_tokens: int = 0
    session_total_cached_tokens: int = 0
    session_total_output_tokens: int | None = None


def _is_valid_memory_recall_boundary(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def update_memory_recall_boundary(
    state: _ExecutionCheckpointState,
    boundary_message_id: int | None,
) -> None:
    if not _is_valid_memory_recall_boundary(boundary_message_id):
        return
    if state.memory_recall_boundary_message_id != boundary_message_id:
        state.memory_recall_boundary_message_id = boundary_message_id
        state.memory_recall_status = "pending"


def memory_recall_needs_precheck(state: _ExecutionCheckpointState) -> bool:
    return _is_valid_memory_recall_boundary(state.memory_recall_boundary_message_id) and state.memory_recall_status not in {"completed", "failed"}


async def _save_execution_checkpoint(
    state: _ExecutionCheckpointState,
    messages: list[InternalMessage],
    current_turn: int,
    *,
    active_audit_execution: dict[str, Any] | None = None,
    update_active_audit_execution: bool = False,
) -> None:
    if state.callback is None:
        return
    checkpoint = {
        "messages": [item.model_dump(mode="json") for item in messages],
        "turn_messages": [item.model_dump(mode="json") for item in state.turn_messages],
        "files_to_user": state.files_to_user,
        "current_turn": current_turn,
        "context_summary_trigger_mode": ContextSummaryTriggerMode.USER_MESSAGE.value,
        "context_summary_fixed_upper_message_id": state.upper_message_id,
        "total_output_tokens": state.total_output_tokens,
        "session_total_input_tokens": state.session_total_input_tokens,
        "session_total_cached_tokens": state.session_total_cached_tokens,
    }
    if state.session_total_output_tokens is not None:
        checkpoint["session_total_output_tokens"] = state.session_total_output_tokens
    if _is_valid_memory_recall_boundary(state.memory_recall_boundary_message_id):
        checkpoint["memory_recall_boundary_message_id"] = state.memory_recall_boundary_message_id
        checkpoint["memory_recall_status"] = state.memory_recall_status
    if update_active_audit_execution:
        checkpoint[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = active_audit_execution
    await state.callback(checkpoint)


async def _mark_claimed_audit_execution_unknown(
    db: AsyncSession,
    audit_record_id: int,
    claim_token: str,
) -> None:
    await audit_crud.mark_execution_unknown(
        db,
        audit_record_id=audit_record_id,
        claim_token=claim_token,
        error_reason=t(ERR_SESSION_REPLY_AUDIT_EXECUTION_UNKNOWN),
    )
    await update_confirmation_message_status(db, audit_record_id=audit_record_id)


@dataclass
class _AgentLoopStreamState:
    callback: Callable[[dict[str, Any]], Awaitable[None]] | None
    current_turn: int
    response_id: str
    expose_tool_call_content: bool
    show_tool_calls: bool
    emitted_agent_loop_output: bool = False
    emitted_stream_content: bool = False
    buffered_content_chunks: list[str] = field(default_factory=list)


async def _emit_agent_loop_output(state: _AgentLoopStreamState) -> None:
    if state.callback is None or state.emitted_agent_loop_output:
        return
    await state.callback(
        {
            "type": "agent_loop_output",
            "turn": state.current_turn,
            "response_id": state.response_id,
        }
    )
    state.emitted_agent_loop_output = True


async def _handle_stream_content(state: _AgentLoopStreamState, content: str) -> None:
    if not state.expose_tool_call_content or not state.show_tool_calls:
        state.buffered_content_chunks.append(content)
        return
    await _emit_agent_loop_output(state)
    if state.callback is None:
        return
    await state.callback(
        {
            "type": "content",
            "content": content,
            "turn": state.current_turn,
            "response_id": state.response_id,
        }
    )
    state.emitted_stream_content = True


@dataclass
class _ParallelToolExecutionContext:
    semaphore: asyncio.Semaphore
    active_tasks: MutableSet[asyncio.Task] | None
    profile: Any
    cfg: Any
    messages: list[InternalMessage]
    username: str
    session_id: str
    current_turn: int
    uid: str
    allowed_knowledge_base_ids: list[int]
    context_window_k: int
    context_summary_boundary_message_id: int | None
    source_message_id: int | None = None


async def _execute_isolated_tool_call(
    context: _ParallelToolExecutionContext,
    tool_call: Any,
) -> InternalMessage:
    async with context.semaphore:
        task = asyncio.create_task(
            process_single_tool_with_isolated_db(
                tool_call,
                context.profile,
                context.cfg,
                context.messages,
                context.username,
                context.session_id,
                context.current_turn,
                context.uid,
                allowed_knowledge_base_ids=context.allowed_knowledge_base_ids,
                context_window_k=context.context_window_k,
                context_summary_boundary_message_id=context.context_summary_boundary_message_id,
                source_message_id=context.source_message_id,
            )
        )
        if context.active_tasks is not None:
            context.active_tasks.add(task)
        try:
            return await task
        finally:
            if context.active_tasks is not None:
                context.active_tasks.discard(task)


def _find_tool_call_by_id(tool_calls: list[Any], tool_call_id: str | None) -> Any | None:
    for tool_call in tool_calls:
        if tool_call.id == tool_call_id:
            return tool_call
    return None
