from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.session import session_crud
from app.core.utils.context_messages import is_context_summary_message
from app.core.utils.context_summary import ContextSummaryTriggerMode, ensure_context_summary
from app.core.utils.context_summary.common import ContextSummaryWorkValidityChecker
from app.core.utils.request_token_baseline import extract_confirmed_provider_input_tokens
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


def _find_completed_tool_result_boundary(
    messages: list[InternalMessage],
    *,
    fixed_upper_message_id: int,
    protected_tool_call_ids: set[str] | None,
) -> int | None:
    pending_tool_call_ids: set[str] = set()
    latest_completed_result_id: int | None = None
    protected_ids = protected_tool_call_ids or set()

    for message in messages:
        if isinstance(message.id, int) and not isinstance(message.id, bool) and message.id < fixed_upper_message_id:
            continue
        if message.role == MessageRole.SYSTEM or is_context_summary_message(message):
            continue
        if message.role == MessageRole.USER:
            if message.attachments or isinstance(message.content, list):
                break
            continue
        if message.role == MessageRole.ASSISTANT and message.tool_calls:
            tool_call_ids = {tool_call.id for tool_call in message.tool_calls if tool_call.id}
            if not tool_call_ids:
                continue
            if pending_tool_call_ids:
                break
            if tool_call_ids & protected_ids:
                break
            pending_tool_call_ids = tool_call_ids
            continue
        if message.role != MessageRole.TOOL or not pending_tool_call_ids:
            continue
        if message.tool_call_id not in pending_tool_call_ids:
            continue
        if not isinstance(message.id, int) or isinstance(message.id, bool) or message.id <= 0:
            break
        pending_tool_call_ids.remove(message.tool_call_id)
        if not pending_tool_call_ids:
            latest_completed_result_id = message.id

    return latest_completed_result_id


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
    channel_id: int | None = None,
    model_id: str | None = None,
    protocol: str | None = None,
    previous_llm_request_metadata: dict | None = None,
    reserved_tokens: int = 0,
    force: bool = False,
    protected_tool_call_ids: set[str] | None = None,
) -> list[InternalMessage]:
    effective_trigger_mode = trigger_mode
    effective_fixed_upper_message_id = fixed_upper_message_id
    if trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE:
        completed_tool_result_id = _find_completed_tool_result_boundary(
            messages,
            fixed_upper_message_id=fixed_upper_message_id,
            protected_tool_call_ids=protected_tool_call_ids,
        )
        if completed_tool_result_id is not None:
            effective_trigger_mode = ContextSummaryTriggerMode.TOOL_RESULT
            effective_fixed_upper_message_id = completed_tool_result_id

    system_messages = [message.model_copy(deep=True) for message in messages if message.role == MessageRole.SYSTEM]
    uncovered_messages = [
        message.model_copy(deep=True)
        for message in messages
        if message.role != MessageRole.SYSTEM
        and not is_context_summary_message(message)
        and _is_after_fixed_upper(
            message,
            trigger_mode=effective_trigger_mode,
            fixed_upper_message_id=effective_fixed_upper_message_id,
        )
    ]
    fixed_request_messages = [*system_messages, *uncovered_messages]

    confirmed_input_tokens = None
    if isinstance(channel_id, int) and not isinstance(channel_id, bool) and channel_id > 0 and isinstance(model_id, str) and model_id.strip() and isinstance(protocol, str) and protocol.strip():
        session = await session_crud.get_by_session_id(db, session_id)
        if session is not None and hasattr(db, "refresh"):
            await db.refresh(session)
        context_summary_revision = session.context_summary_revision if session is not None else 0
        context_content_revision = session.context_content_revision if session is not None else 0
        confirmed_input_tokens = extract_confirmed_provider_input_tokens(
            previous_llm_request_metadata,
            channel_id=channel_id,
            model_id=model_id,
            protocol=protocol,
            context_summary_revision=context_summary_revision,
            context_content_revision=context_content_revision,
        )
        if confirmed_input_tokens is None and session is not None:
            confirmed_input_tokens = extract_confirmed_provider_input_tokens(
                session.llm_request_metadata,
                channel_id=channel_id,
                model_id=model_id,
                protocol=protocol,
                context_summary_revision=context_summary_revision,
                context_content_revision=context_content_revision,
            )

    state = await ensure_context_summary(
        db,
        session_id=session_id,
        uid=uid,
        profile=profile,
        cfg=cfg,
        before_id=(effective_fixed_upper_message_id if effective_trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE else effective_fixed_upper_message_id + 1),
        current_message="",
        summary_source_messages=messages,
        context_window_k=context_window_k,
        max_tokens=max_tokens,
        reserved_tokens=max(reserved_tokens, 0),
        tools=tools,
        trigger_mode=effective_trigger_mode,
        fixed_upper_message_id=effective_fixed_upper_message_id,
        fixed_request_messages=fixed_request_messages,
        confirmed_input_tokens=confirmed_input_tokens,
        channel_id=channel_id,
        model_id=model_id,
        protocol=protocol,
        work_validity_checker=work_validity_checker,
        lifecycle_event_callback=lifecycle_event_callback,
        force=force,
    )

    summary_message = state.as_message()
    if summary_message is None:
        return messages

    retained_messages = [message for message in messages if message.role != MessageRole.SYSTEM and not is_context_summary_message(message) and (message.id is None or message.id > (state.message_id or 0))]
    return [
        *system_messages,
        summary_message,
        *retained_messages,
    ]
