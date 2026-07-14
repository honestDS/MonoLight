import json
from dataclasses import dataclass
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.message import message_crud
from app.models.message import Message, MessageRole, MessageType


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


def _parse_tool_call_ids(message: Message) -> set[str]:
    if message.type != MessageType.TOOL_CALL:
        return set()
    try:
        payload = json.loads(message.content or "")
    except (TypeError, json.JSONDecodeError):
        return set()
    if not isinstance(payload, dict) or not isinstance(payload.get("tool_calls"), list):
        return set()
    return {str(tool_call["id"]) for tool_call in payload["tool_calls"] if isinstance(tool_call, dict) and tool_call.get("id")}


def _parse_tool_result_id(message: Message) -> str | None:
    if message.type != MessageType.TOOL_RESULT:
        return None
    try:
        payload = json.loads(message.content or "")
    except (TypeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload.get("tool_call_id"):
        return None
    return str(payload["tool_call_id"])


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
        raise ValueError("fixed_upper_message_id must be positive")
    if expected_summary_message_id is not None and fixed_upper_message_id <= expected_summary_message_id:
        raise ValueError("fixed upper message must be newer than the persisted summary boundary")

    fixed_upper = await _get_fixed_upper_message(
        db,
        session_id=session_id,
        uid=uid,
        message_id=fixed_upper_message_id,
    )
    if fixed_upper is None:
        raise ValueError("fixed upper message does not exist in the requested session")

    if trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE:
        if fixed_upper.role != MessageRole.USER:
            raise ValueError("user-message trigger requires a user message as the fixed upper boundary")
        scan_before_id = fixed_upper_message_id
    elif trigger_mode == ContextSummaryTriggerMode.TOOL_RESULT:
        if fixed_upper.role != MessageRole.TOOL or fixed_upper.type != MessageType.TOOL_RESULT:
            raise ValueError("tool-result trigger requires a completed tool result as the fixed upper boundary")
        scan_before_id = fixed_upper_message_id + 1
    else:
        raise ValueError(f"unsupported context summary trigger mode: {trigger_mode}")

    page_after_id = expected_summary_message_id
    pending_tool_call_ids: set[str] = set()
    target_message_id: int | None = None
    last_scanned_message_id: int | None = None
    covered_user_message: Message | None = None

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
            if _is_real_user_message(message):
                covered_user_message = message

            pending_tool_call_ids.update(_parse_tool_call_ids(message))
            tool_result_id = _parse_tool_result_id(message)
            if tool_result_id is not None:
                pending_tool_call_ids.discard(tool_result_id)

            if not pending_tool_call_ids:
                target_message_id = message.id

        if len(page) < page_size:
            break
        last_id = page[-1].id
        if last_id is None:
            break
        page_after_id = last_id

    expected_target_id = last_scanned_message_id if trigger_mode == ContextSummaryTriggerMode.USER_MESSAGE else fixed_upper_message_id
    if target_message_id != expected_target_id:
        target_message_id = None

    return ContextSummaryBoundary(
        trigger_mode=trigger_mode,
        fixed_upper_message_id=fixed_upper_message_id,
        target_message_id=target_message_id,
        covered_user_message_id=covered_user_message.id if covered_user_message is not None else None,
        covered_user_message_content=covered_user_message.content if covered_user_message is not None else None,
    )
