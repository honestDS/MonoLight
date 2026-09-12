import hashlib
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.message import message_crud
from app.core.message_platforms.notifier import send_session_event
from app.models.audit import AuditRecordStatus
from app.models.message import InternalMessage, MessageRole, MessageType

from .confirmation_common import (
    ConfirmationStatusUpdate,
    PendingConfirmationCancellation,
    logger,
)
from .confirmation_queries import (
    _get_confirmation_message,
    _get_structured_tool_result_messages,
)

__all__ = [
    "build_confirmation_update_events",
    "notify_confirmation_tool_results",
    "broadcast_pending_confirmation_cancellation",
]


async def _get_confirmation_tool_result_events(db: AsyncSession, record) -> list[dict]:
    if record.source_assistant_message_id is None or record.id is None:
        return []

    source_message = await message_crud.get(db, record.source_assistant_message_id)
    if source_message is None:
        return []
    try:
        source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
    except ValueError:
        return []

    tool_call_ids = [tool_call.id for tool_call in source_internal.tool_calls or []]
    if not tool_call_ids:
        return []
    structured_messages = await _get_structured_tool_result_messages(
        db,
        uid=record.uid,
        session_id=record.session_id,
        audit_record_id=record.id,
    )
    structured = bool(structured_messages)
    if structured:
        messages = structured_messages
    else:
        messages = await message_crud.get_history_forward_by_id(
            db,
            session_id=record.session_id,
            uid=record.uid,
            after_id=record.source_assistant_message_id,
            before_id=record.decision_message_id,
            limit=500,
        )
    expected_tool_call_ids = set(tool_call_ids)
    results_by_tool_call_id: dict[str, dict] = {}
    for message in messages:
        if message.type != MessageType.TOOL_RESULT or message.id is None:
            continue
        try:
            tool_result = InternalMessage.model_validate_json(message.content or "{}")
        except ValueError:
            continue
        tool_call_id = message.audit_tool_call_id if structured else tool_result.tool_call_id
        if structured and tool_result.tool_call_id != tool_call_id:
            continue
        if tool_result.role != MessageRole.TOOL or tool_call_id not in expected_tool_call_ids or tool_call_id in results_by_tool_call_id:
            continue
        created_at = message.created_at.timestamp() if message.created_at is not None else None
        results_by_tool_call_id[tool_call_id] = {
            "id": message.id,
            "db_id": message.id,
            "role": message.role.value if isinstance(message.role, MessageRole) else str(message.role),
            "type": message.type.value if isinstance(message.type, MessageType) else str(message.type),
            "content": message.content,
            "tool_call_id": tool_call_id,
            "created_at": created_at,
        }
    return [results_by_tool_call_id[tool_call_id] for tool_call_id in tool_call_ids if tool_call_id in results_by_tool_call_id]


def _confirmation_status_value(record) -> str:
    return record.status.value if isinstance(record.status, AuditRecordStatus) else str(record.status)


def _build_confirmation_status_event(
    record,
    *,
    message_id: int | None,
    status: str,
    content: str | None,
) -> dict[str, Any]:
    return {
        "type": "audit_confirmation_status",
        "source": "audit_confirmation",
        "event_id": f"audit-confirmation:{record.id}:{status}",
        "session_id": record.session_id,
        "audit_record_id": record.id,
        "message_id": message_id,
        "status": status,
        "content": content,
    }


def _build_confirmation_tool_results_event(record, tool_results: list[dict]) -> dict[str, Any] | None:
    if not tool_results:
        return None

    result_hash = hashlib.sha256(json.dumps(tool_results, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()[:16]
    return {
        "type": "audit_tool_results_update",
        "source": "audit_confirmation",
        "event_id": f"audit-tool-results:{record.id}:{result_hash}",
        "session_id": record.session_id,
        "audit_record_id": record.id,
        "messages": tool_results,
    }


async def build_confirmation_update_events(
    db: AsyncSession,
    *,
    audit_record_id: int,
    include_tool_results: bool = True,
) -> list[dict[str, Any]]:
    record = await audit_crud.get_record(db, audit_record_id)
    if record is None or record.id is None:
        return []
    confirmation_message, _confirmation_payload = await _get_confirmation_message(db, record)
    if confirmation_message is None or confirmation_message.id is None:
        return []

    events = [
        _build_confirmation_status_event(
            record,
            message_id=confirmation_message.id,
            status=_confirmation_status_value(record),
            content=confirmation_message.content,
        )
    ]
    if include_tool_results:
        tool_results = await _get_confirmation_tool_result_events(db, record)
        tool_results_event = _build_confirmation_tool_results_event(record, tool_results)
        if tool_results_event is not None:
            events.append(tool_results_event)
    return events


async def _send_confirmation_status_event(
    record,
    *,
    message_id: int | None,
    status: str,
    content: str | None,
) -> None:
    event = _build_confirmation_status_event(
        record,
        message_id=message_id,
        status=status,
        content=content,
    )
    try:
        await send_session_event(record.uid, record.session_id, event)
    except Exception:
        logger.bind(uid=record.uid, session_id=record.session_id, audit_record_id=record.id, status=status).warning(
            "Failed to broadcast audit confirmation status",
            exc_info=True,
        )


async def _send_confirmation_tool_results_event(record, tool_results: list[dict]) -> None:
    event = _build_confirmation_tool_results_event(record, tool_results)
    if event is None:
        return

    try:
        await send_session_event(record.uid, record.session_id, event)
    except Exception:
        logger.bind(uid=record.uid, session_id=record.session_id, audit_record_id=record.id).warning(
            "Failed to broadcast audit tool result update",
            exc_info=True,
        )


async def notify_confirmation_tool_results(db: AsyncSession, *, audit_record_id: int) -> bool:
    """在工具结果已提交后，广播数据库中的最终工具结果。"""
    record = await audit_crud.get_record(db, audit_record_id)
    if record is None or record.id is None:
        return False
    tool_results = await _get_confirmation_tool_result_events(db, record)
    if not tool_results:
        return False
    await _send_confirmation_tool_results_event(record, tool_results)
    return True


async def _broadcast_confirmation_status_update(
    db: AsyncSession,
    *,
    status_update: ConfirmationStatusUpdate,
) -> None:
    await _send_confirmation_status_event(
        status_update.record,
        message_id=status_update.message_id,
        status=status_update.status,
        content=status_update.content,
    )
    tool_results = await _get_confirmation_tool_result_events(db, status_update.record)
    await _send_confirmation_tool_results_event(status_update.record, tool_results)


async def broadcast_pending_confirmation_cancellation(
    db: AsyncSession,
    *,
    cancellation: PendingConfirmationCancellation,
) -> None:
    """广播已提交的取消投影。"""
    if cancellation.status_update is not None:
        await _broadcast_confirmation_status_update(db, status_update=cancellation.status_update)
