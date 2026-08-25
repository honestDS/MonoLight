from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.message import InternalMessage

MemoryRecallStatus = Literal["completed", "failed"]
StreamEventCallback = Callable[[dict[str, Any]], Awaitable[None]]
ContextSummaryCallback = Callable[[dict[str, Any]], Awaitable[None]]


@dataclass(slots=True)
class MemoryRecallContext:
    db: AsyncSession
    uid: str
    session_id: str
    profile: Any
    cfg: Any
    username: str
    messages: list[InternalMessage]
    turn_messages: list[InternalMessage]
    current_user_boundary_message_id: int | None
    upper_message_id: int | None
    chat_channel: Any
    chat_cursor_key: str
    chat_channel_obj: Any | None = None
    model_entry: dict[str, Any] | None = None
    channel_rule: Any | None = None
    chat_params: dict[str, Any] = field(default_factory=dict)
    dispatcher_mode: Literal["non_stream", "stream"] = "non_stream"
    stream_event_callback: StreamEventCallback | None = None
    show_tool_calls: bool = True
    expose_tool_call_content: bool = True
    context_summary_callback: ContextSummaryCallback | None = None
    context_summary_checker: Callable[[], Awaitable[bool]] | None = None
    latest_llm_request_metadata: dict[str, Any] | None = None
    total_output_tokens: int = 0
    session_total_output_tokens: int | None = None
    session_total_input_tokens: int = 0
    session_total_cached_tokens: int = 0
    allowed_knowledge_base_ids: list[int] = field(default_factory=list)


@dataclass(slots=True)
class MemoryRecallPrecheckResult:
    status: MemoryRecallStatus
    messages: list[InternalMessage]
    turn_messages: list[InternalMessage]
    chat_channel: Any
    chat_cursor_key: str
    chat_channel_obj: Any | None
    model_entry: dict[str, Any] | None
    channel_rule: Any | None
    chat_params: dict[str, Any]
    latest_llm_request_metadata: dict[str, Any] | None
    total_output_tokens: int
    session_total_output_tokens: int | None
    session_total_input_tokens: int
    session_total_cached_tokens: int
    error_type: str | None = None


def get_profile_id(context: MemoryRecallContext) -> int | None:
    profile_id = getattr(context.profile, "id", None)
    if isinstance(profile_id, int) and not isinstance(profile_id, bool) and profile_id > 0:
        return profile_id
    return None


def build_result(
    context: MemoryRecallContext,
    status: MemoryRecallStatus,
    error_type: str | None = None,
) -> MemoryRecallPrecheckResult:
    return MemoryRecallPrecheckResult(
        status=status,
        messages=context.messages,
        turn_messages=context.turn_messages,
        chat_channel=context.chat_channel,
        chat_cursor_key=context.chat_cursor_key,
        chat_channel_obj=context.chat_channel_obj,
        model_entry=context.model_entry,
        channel_rule=context.channel_rule,
        chat_params=context.chat_params,
        latest_llm_request_metadata=context.latest_llm_request_metadata,
        total_output_tokens=context.total_output_tokens,
        session_total_output_tokens=context.session_total_output_tokens,
        session_total_input_tokens=context.session_total_input_tokens,
        session_total_cached_tokens=context.session_total_cached_tokens,
        error_type=error_type,
    )


__all__ = [
    "ContextSummaryCallback",
    "MemoryRecallContext",
    "MemoryRecallPrecheckResult",
    "MemoryRecallStatus",
    "StreamEventCallback",
    "build_result",
    "get_profile_id",
]
