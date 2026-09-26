from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.session import session_crud
from app.core.utils.context_messages import is_context_summary_message
from app.core.utils.context_summary import ContextSummaryTriggerMode, ensure_context_summary
from app.core.utils.context_summary.common import ContextSummaryWorkValidityChecker
from app.core.utils.dispatcher.session_todo_snapshot import (
    append_session_todo_snapshot,
    load_current_session_todo_snapshot,
    measure_session_todo_snapshot_tokens,
    strip_session_todo_snapshots,
)
from app.core.utils.request_token_baseline import estimate_incremental_input_tokens
from app.models.message import InternalMessage, MessageRole
from app.models.profile import Profile, ProfileConfig


def _is_after_fixed_upper(
    message: InternalMessage,
    *,
    trigger_mode: ContextSummaryTriggerMode,
    fixed_upper_message_id: int,
) -> bool:
    if message.id is None:
        return True
    if trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE:
        return message.id >= fixed_upper_message_id
    return message.id > fixed_upper_message_id


async def apply_context_summary_checkpoint(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    messages: list[InternalMessage],
    trigger_mode: ContextSummaryTriggerMode,
    fixed_upper_message_id: int,
    context_window_k: int,
    max_tokens: int,
    tools: list[dict] | None,
    work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
    lifecycle_event_callback: Callable[[dict[str, object]], Awaitable[None]] | None = None,
    model_id: str | None = None,
    protocol: str | None = None,
    previous_llm_request_metadata: dict | None = None,
    reserved_tokens: int = 0,
    allow_incremental_input_estimate: bool = True,
    force: bool = False,
) -> list[InternalMessage]:
    todo_snapshot = None
    if isinstance(db, AsyncSession):
        todo_snapshot = await load_current_session_todo_snapshot(
            db,
            uid=uid,
            session_id=session_id,
        )
    summary_input_messages = strip_session_todo_snapshots(messages)
    todo_snapshot_tokens = measure_session_todo_snapshot_tokens(todo_snapshot)

    system_messages = [message.model_copy(deep=True) for message in summary_input_messages if message.role == MessageRole.SYSTEM]
    uncovered_messages = [
        message.model_copy(deep=True)
        for message in summary_input_messages
        if message.role != MessageRole.SYSTEM
        and not is_context_summary_message(message)
        and _is_after_fixed_upper(
            message,
            trigger_mode=trigger_mode,
            fixed_upper_message_id=fixed_upper_message_id,
        )
    ]
    fixed_request_messages = [*system_messages, *uncovered_messages]

    required_input_tokens_override = None
    # Todo 快照作为新工具结果的一部分进入请求；同 ID 内容替换会提升 context_content_revision，
    # 因此 provider 基线仍可由统一增量校验判断是否可复用，无需为 Todo 单独退回本地估算。
    if allow_incremental_input_estimate and isinstance(model_id, str) and model_id.strip() and isinstance(protocol, str) and protocol.strip():
        session = await session_crud.get_by_session_id(db, session_id)
        if session is not None and hasattr(db, "refresh"):
            await db.refresh(session)
        metadata = previous_llm_request_metadata if isinstance(previous_llm_request_metadata, dict) and previous_llm_request_metadata.get("input_tokens_source") == "provider" else None
        if metadata is None and session is not None:
            metadata = session.llm_request_metadata
        required_input_tokens_override = estimate_incremental_input_tokens(
            messages,
            tools,
            metadata,
            model_id=model_id,
            protocol=protocol,
            context_summary_revision=session.context_summary_revision if session is not None else 0,
            context_content_revision=session.context_content_revision if session is not None else 0,
        )

    state = await ensure_context_summary(
        db,
        session_id=session_id,
        uid=uid,
        profile=profile,
        cfg=cfg,
        before_id=(fixed_upper_message_id if trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE else fixed_upper_message_id + 1),
        current_message="",
        context_window_k=context_window_k,
        max_tokens=max_tokens,
        reserved_tokens=max(reserved_tokens, 0) + todo_snapshot_tokens,
        tools=tools,
        trigger_mode=trigger_mode,
        fixed_upper_message_id=fixed_upper_message_id,
        fixed_request_messages=fixed_request_messages,
        required_input_tokens_override=required_input_tokens_override,
        work_validity_checker=work_validity_checker,
        lifecycle_event_callback=lifecycle_event_callback,
        force=force,
    )

    summary_message = state.as_message()
    if summary_message is None:
        return messages

    retained_messages = [message for message in summary_input_messages if message.role != MessageRole.SYSTEM and not is_context_summary_message(message) and (message.id is None or message.id > (state.message_id or 0))]
    compacted_messages = [
        *system_messages,
        summary_message,
        *retained_messages,
    ]
    return append_session_todo_snapshot(compacted_messages, todo_snapshot)
