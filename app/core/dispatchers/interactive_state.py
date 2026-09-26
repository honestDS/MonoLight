import asyncio
from collections.abc import Awaitable, Callable, MutableSet
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_MEMORY_RECALL_BOUNDARY_INVALID,
    ERR_MEMORY_RECALL_STATUS_BOUNDARY_REQUIRED,
    ERR_MEMORY_RECALL_STATUS_INVALID,
)
from app.core.i18n import t
from app.core.utils.context_summary import ContextSummaryTriggerMode
from app.core.utils.context_summary.common import ContextSummaryWorkValidityChecker
from app.core.utils.request_token_baseline import merge_session_cache_token_totals
from app.models.message import InternalMessage

from .interactive_helpers import (
    _AdditionalUserMessagesContext,
    _ExecutionCheckpointState,
)

__all__ = [
    "InteractiveDispatchState",
    "build_interactive_dispatch_state",
]


@dataclass
class InteractiveDispatchState:
    db: AsyncSession
    uid: str
    session_id: str
    active_tasks: MutableSet[asyncio.Task] | None
    session_source: str
    final_message_dedupe_key: str | None
    context_summary_lifecycle_callback: Callable[[dict[str, object]], Awaitable[None]] | None
    context_summary_work_validity_checker: ContextSummaryWorkValidityChecker | None
    expose_tool_call_content: bool
    show_tool_calls: bool
    dispatcher_mode: Literal["non_stream", "stream"]
    request_metadata_callback: Callable[[dict[str, Any]], Awaitable[None]] | None
    stream_event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None
    dispatch_logger: Any
    username: str
    profile: Any
    initial_msg: InternalMessage
    queue_managed: bool
    additional_user_messages_context: _AdditionalUserMessagesContext
    execution_resume_state: dict[str, Any] | None
    checkpoint_state: _ExecutionCheckpointState
    turn_messages: list[InternalMessage] = field(default_factory=list)
    files_to_user: list[str] = field(default_factory=list)
    is_first_iter: bool = True
    latest_llm_request_metadata: dict[str, Any] | None = None
    messages: list[InternalMessage] = field(default_factory=list)
    current_turn: int = 0
    cfg: Any = None
    memory_enabled: bool = False
    memory_precheck_enabled: bool = False
    chat_channel: Any = None
    chat_cursor_key: str = ""
    chat_channel_obj: Any | None = None
    model_entry: dict[str, Any] | None = None
    channel_rule: Any | None = None
    chat_params: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    allowed_knowledge_base_ids: list[int] = field(default_factory=list)
    img_understanding: bool = False
    audio_understanding: bool = False
    video_understanding: bool = False
    final_ai_content: Any = None
    final_reasoning_content: str | None = None
    final_finish_reason: str | None = None
    final_finish_details: dict[str, Any] | None = None
    final_provider_metadata: dict[str, Any] | None = None
    final_refusal: str | None = None
    final_message_provider_metadata: dict[str, Any] | None = None


def build_interactive_dispatch_state(
    *,
    db: AsyncSession,
    uid: str,
    session_id: str = "default",
    active_tasks: MutableSet[asyncio.Task] | None = None,
    session_source: str = "http",
    frozen_user_message_ids: list[int] | None = None,
    final_message_dedupe_key: str | None = None,
    context_summary_lifecycle_callback: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    context_summary_work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
    expose_tool_call_content: bool = True,
    show_tool_calls: bool = True,
    dispatcher_mode: Literal["non_stream", "stream"] = "non_stream",
    request_metadata_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    stream_event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
    dispatch_logger: Any,
    username: str,
    profile: Any,
    initial_msg: InternalMessage,
    queue_managed: bool,
    additional_user_messages_context: _AdditionalUserMessagesContext,
    execution_resume_state: dict[str, Any] | None = None,
    execution_checkpoint_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> InteractiveDispatchState:
    turn_messages = [InternalMessage.model_validate(item) for item in execution_resume_state.get("turn_messages", [])] if execution_resume_state else []
    files_to_user = list(execution_resume_state.get("files_to_user", [])) if execution_resume_state else []
    is_first_iter = execution_resume_state is None
    resumed_total_output_tokens = execution_resume_state.get("total_output_tokens", 0) if execution_resume_state else 0
    resumed_session_total_output_tokens = execution_resume_state.get("session_total_output_tokens") if execution_resume_state else None
    resumed_session_total_input_tokens, resumed_session_total_cached_tokens = merge_session_cache_token_totals(
        None,
        total_input_tokens=(execution_resume_state.get("session_total_input_tokens", 0) if execution_resume_state is not None else 0),
        total_cached_tokens=(execution_resume_state.get("session_total_cached_tokens", 0) if execution_resume_state is not None else 0),
    )
    initial_memory_recall_boundary = max(frozen_user_message_ids) if frozen_user_message_ids else initial_msg.id
    resumed_memory_recall_boundary = execution_resume_state.get("memory_recall_boundary_message_id") if execution_resume_state else None
    resumed_memory_recall_status = execution_resume_state.get("memory_recall_status") if execution_resume_state else None
    if resumed_memory_recall_boundary is not None and (not isinstance(resumed_memory_recall_boundary, int) or isinstance(resumed_memory_recall_boundary, bool) or resumed_memory_recall_boundary <= 0):
        raise ValueError(t(ERR_MEMORY_RECALL_BOUNDARY_INVALID))
    if resumed_memory_recall_status is not None and (not isinstance(resumed_memory_recall_status, str) or resumed_memory_recall_status not in {"pending", "completed", "failed"}):
        raise ValueError(t(ERR_MEMORY_RECALL_STATUS_INVALID))
    if resumed_memory_recall_status is not None and resumed_memory_recall_boundary is None:
        raise ValueError(t(ERR_MEMORY_RECALL_STATUS_BOUNDARY_REQUIRED))
    checkpoint_state = _ExecutionCheckpointState(
        callback=execution_checkpoint_callback,
        turn_messages=turn_messages,
        files_to_user=files_to_user,
        upper_message_id=min(frozen_user_message_ids) if frozen_user_message_ids else initial_msg.id,
        memory_recall_boundary_message_id=initial_memory_recall_boundary,
        memory_recall_status=(resumed_memory_recall_status if resumed_memory_recall_boundary == initial_memory_recall_boundary else None),
        total_output_tokens=(resumed_total_output_tokens if isinstance(resumed_total_output_tokens, int) and not isinstance(resumed_total_output_tokens, bool) and resumed_total_output_tokens >= 0 else 0),
        session_total_input_tokens=resumed_session_total_input_tokens,
        session_total_cached_tokens=resumed_session_total_cached_tokens,
        session_total_output_tokens=(resumed_session_total_output_tokens if isinstance(resumed_session_total_output_tokens, int) and not isinstance(resumed_session_total_output_tokens, bool) and resumed_session_total_output_tokens >= 0 else None),
    )
    if execution_resume_state is not None:
        saved_checkpoint_mode = execution_resume_state.get("context_summary_trigger_mode")
        saved_checkpoint_upper_id = execution_resume_state.get("context_summary_fixed_upper_message_id")
        if saved_checkpoint_mode == ContextSummaryTriggerMode.USER_MESSAGE.value and isinstance(saved_checkpoint_upper_id, int) and saved_checkpoint_upper_id > 0:
            checkpoint_state.upper_message_id = saved_checkpoint_upper_id

    return InteractiveDispatchState(
        db=db,
        uid=uid,
        session_id=session_id,
        active_tasks=active_tasks,
        session_source=session_source,
        final_message_dedupe_key=final_message_dedupe_key,
        context_summary_lifecycle_callback=context_summary_lifecycle_callback,
        context_summary_work_validity_checker=context_summary_work_validity_checker,
        expose_tool_call_content=expose_tool_call_content,
        show_tool_calls=show_tool_calls,
        dispatcher_mode=dispatcher_mode,
        request_metadata_callback=request_metadata_callback,
        stream_event_callback=stream_event_callback,
        dispatch_logger=dispatch_logger,
        username=username,
        profile=profile,
        initial_msg=initial_msg,
        queue_managed=queue_managed,
        additional_user_messages_context=additional_user_messages_context,
        execution_resume_state=execution_resume_state,
        checkpoint_state=checkpoint_state,
        turn_messages=turn_messages,
        files_to_user=files_to_user,
        is_first_iter=is_first_iter,
    )
