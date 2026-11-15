from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_CONTEXT_SUMMARY_TOOL_TRIGGER_BOUNDARY_INVALID,
    ERR_CONTEXT_SUMMARY_TRIGGER_MODE_UNSUPPORTED,
    ERR_CONTEXT_SUMMARY_UPPER_NOT_FOUND,
    ERR_CONTEXT_SUMMARY_UPPER_NOT_NEWER,
    ERR_CONTEXT_SUMMARY_USER_TRIGGER_BOUNDARY_INVALID,
    ERR_VALUE_MUST_BE_POSITIVE,
)
from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.core.utils.message_parser import StoredToolCallState
from app.models.message import InternalMessage, Message, MessageRole, MessageType


class ContextSummaryTriggerMode(StrEnum):
    USER_MESSAGE = "user_message"
    TOOL_RESULT = "tool_result"


@dataclass(frozen=True)
class ContextSummaryBoundary:
    trigger_mode: ContextSummaryTriggerMode
    fixed_upper_message_id: int
    target_message_id: int | None
    covered_user_message_id: int | None
    covered_user_message_content: str | None
    late_cancelled_tool_results: tuple[tuple[int, InternalMessage], ...] = ()


def _is_real_user_message(message: Message) -> bool:
    return message.role == MessageRole.USER and message.type == MessageType.TEXT


async def _get_fixed_upper_message(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    message_id: int,
) -> Message | None:
    page = await message_crud.get_history_forward_by_id(
        db,
        session_id=session_id,
        uid=uid,
        after_id=message_id - 1,
        before_id=message_id + 1,
        limit=1,
    )
    if not page or page[0].id != message_id:
        return None
    return page[0]


async def resolve_context_summary_boundary(
    db: AsyncSession,
    *,
    session_id: str,
    uid: str,
    expected_summary_message_id: int | None,
    trigger_mode: ContextSummaryTriggerMode,
    fixed_upper_message_id: int,
    page_size: int = 200,
) -> ContextSummaryBoundary:
    if fixed_upper_message_id <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="fixed_upper_message_id"))
    if expected_summary_message_id is not None and fixed_upper_message_id <= expected_summary_message_id:
        raise ValueError(t(ERR_CONTEXT_SUMMARY_UPPER_NOT_NEWER))

    fixed_upper = await _get_fixed_upper_message(
        db,
        session_id=session_id,
        uid=uid,
        message_id=fixed_upper_message_id,
    )
    if fixed_upper is None:
        raise ValueError(t(ERR_CONTEXT_SUMMARY_UPPER_NOT_FOUND))

    if trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE:
        if fixed_upper.role != MessageRole.USER:
            raise ValueError(t(ERR_CONTEXT_SUMMARY_USER_TRIGGER_BOUNDARY_INVALID))
        scan_before_id = fixed_upper_message_id
    elif trigger_mode == ContextSummaryTriggerMode.TOOL_RESULT:
        if fixed_upper.role != MessageRole.TOOL or fixed_upper.type != MessageType.TOOL_RESULT:
            raise ValueError(t(ERR_CONTEXT_SUMMARY_TOOL_TRIGGER_BOUNDARY_INVALID))
        scan_before_id = fixed_upper_message_id + 1
    else:
        raise ValueError(t(ERR_CONTEXT_SUMMARY_TRIGGER_MODE_UNSUPPORTED, trigger_mode=trigger_mode))

    page_after_id = expected_summary_message_id
    state = StoredToolCallState(session_id, uid)
    last_scanned_message_id: int | None = None
    covered_user_message: Message | None = None
    late_cancelled_tool_results: list[tuple[int, InternalMessage]] = []

    while True:
        page = await message_crud.get_history_forward_by_id(
            db,
            session_id=session_id,
            uid=uid,
            after_id=expected_summary_message_id,
            before_id=scan_before_id,
            page_after_id=page_after_id,
            limit=page_size,
        )
        if not page:
            break

        for message in page:
            if message.id is None:
                continue
            last_scanned_message_id = message.id
            state.observe(message)
            if _is_real_user_message(message):
                if trigger_mode == ContextSummaryTriggerMode.TOOL_RESULT and not message.is_processed:
                    return ContextSummaryBoundary(
                        trigger_mode=trigger_mode,
                        fixed_upper_message_id=fixed_upper_message_id,
                        target_message_id=None,
                        covered_user_message_id=None,
                        covered_user_message_content=None,
                    )
                covered_user_message = message

        if len(page) < page_size:
            break
        last_id = page[-1].id
        if last_id is None:
            break
        page_after_id = last_id

    if state.pending and last_scanned_message_id is not None:
        late_rows = await message_crud.list_late_cancelled_tool_results(
            db,
            uid=uid,
            session_id=session_id,
            source_message_ids=sorted({source_message_id for source_message_id, _tool_call_id in state.pending}),
            after_id=last_scanned_message_id,
        )
        for late_row in late_rows:
            accepted = state.accept_late_tool_result(late_row)
            if accepted is not None:
                late_cancelled_tool_results.append(accepted)

    target_message_id = None
    if not state.pending:
        target_message_id = last_scanned_message_id if trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE else fixed_upper_message_id

    return ContextSummaryBoundary(
        trigger_mode=trigger_mode,
        fixed_upper_message_id=fixed_upper_message_id,
        target_message_id=target_message_id,
        covered_user_message_id=covered_user_message.id if covered_user_message is not None else None,
        covered_user_message_content=covered_user_message.content if covered_user_message is not None else None,
        late_cancelled_tool_results=tuple(late_cancelled_tool_results) if target_message_id is not None else (),
    )
