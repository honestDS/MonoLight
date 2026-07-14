from collections.abc import Awaitable, Callable

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.context_messages import is_context_summary_message
from app.core.utils.context_summary import ContextSummaryTriggerMode, ensure_context_summary
from app.core.utils.context_summary.common import ContextSummaryWorkValidityChecker
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
) -> list[InternalMessage]:
    system_messages = [message.model_copy(deep=True) for message in messages if message.role == MessageRole.SYSTEM]
    uncovered_messages = [
        message.model_copy(deep=True)
        for message in messages
        if message.role != MessageRole.SYSTEM
        and not is_context_summary_message(message)
        and _is_after_fixed_upper(
            message,
            trigger_mode=trigger_mode,
            fixed_upper_message_id=fixed_upper_message_id,
        )
    ]
    fixed_request_messages = [*system_messages, *uncovered_messages]

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
        reserved_tokens=0,
        tools=tools,
        trigger_mode=trigger_mode,
        fixed_upper_message_id=fixed_upper_message_id,
        fixed_request_messages=fixed_request_messages,
        work_validity_checker=work_validity_checker,
        lifecycle_event_callback=lifecycle_event_callback,
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
