import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER,
    ERR_AUDIT_EXECUTION_CLAIM_FAILED,
    MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.audit.tool_result_version import audit_tool_result_version_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.session import session_crud
from app.core.i18n import t
from app.models.audit import AuditRecordStatus
from app.models.message import InternalMessage, Message, MessageRole, MessageType

from .confirmation_common import (
    CONFIRMATION_DECISION_FIELD,
    REJECTION_SOURCE_FIELD,
    ConfirmationDecision,
    PendingConfirmationCancellation,
)
from .confirmation_events import broadcast_pending_confirmation_cancellation
from .confirmation_projection import _sync_confirmation_message_status_projection
from .confirmation_queries import _get_structured_tool_result_messages

__all__ = [
    "get_pending_tool_results",
    "replace_pending_tool_result",
    "cancel_persisted_pending_confirmation_bundle",
    "supersede_persisted_pending_confirmation_bundle",
    "update_confirmation_tool_results_for_decision",
]


async def get_pending_tool_results(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    source_assistant_message_id: int,
    before_message_id: int | None,
    tool_call_ids: list[str],
    audit_record_id: int | None = None,
) -> dict[str, Message] | None:
    if (audit_record_id is None and before_message_id is None) or not tool_call_ids or len(set(tool_call_ids)) != len(tool_call_ids):
        return None

    structured = audit_record_id is not None
    if structured:
        messages = await _get_structured_tool_result_messages(
            db,
            uid=uid,
            session_id=session_id,
            audit_record_id=audit_record_id,
        )
    else:
        messages = await message_crud.get_history_forward_by_id(
            db,
            session_id=session_id,
            uid=uid,
            after_id=source_assistant_message_id,
            before_id=before_message_id,
            limit=500,
        )
    tool_result_messages = [message for message in messages if message.type == MessageType.TOOL_RESULT]
    if len(tool_result_messages) != len(tool_call_ids):
        return None

    expected_tool_call_ids = set(tool_call_ids)
    pending_results: dict[str, Message] = {}
    for message in tool_result_messages:
        try:
            tool_result = InternalMessage.model_validate_json(message.content or "{}")
            result_payload = json.loads(tool_result.content or "{}")
        except (TypeError, ValueError):
            return None
        tool_call_id = message.audit_tool_call_id if structured else tool_result.tool_call_id
        if structured and tool_result.tool_call_id != tool_call_id:
            return None
        result_status = result_payload.get("status") if isinstance(result_payload, dict) else None
        is_pending_result = result_status == AuditRecordStatus.PENDING.value
        is_executing_confirmation_result = (
            result_status == AuditRecordStatus.EXECUTING.value and result_payload.get("confirmation_status") in {ConfirmationDecision.APPROVE.value, ConfirmationDecision.IGNORE.value} and isinstance(result_payload.get(CONFIRMATION_DECISION_FIELD), str) and bool(result_payload[CONFIRMATION_DECISION_FIELD].strip())
        )
        if tool_result.role != MessageRole.TOOL or not isinstance(tool_call_id, str) or tool_call_id not in expected_tool_call_ids or tool_call_id in pending_results or not isinstance(result_payload, dict) or not (is_pending_result or is_executing_confirmation_result):
            return None
        pending_results[tool_call_id] = message

    if set(pending_results) != expected_tool_call_ids:
        return None
    return pending_results


async def replace_pending_tool_result(
    db: AsyncSession,
    *,
    pending_message: Message,
    original_tool_call_id: str,
    content: str | None,
    audit_record_id: int | None = None,
) -> str | None:
    replacement_content = content
    confirmation_decision = None
    try:
        pending_tool_result = InternalMessage.model_validate_json(pending_message.content or "{}")
        pending_payload = json.loads(pending_tool_result.content or "{}")
        if isinstance(pending_payload, dict):
            decision_value = pending_payload.get(CONFIRMATION_DECISION_FIELD)
            if isinstance(decision_value, str):
                confirmation_decision = decision_value
    except (TypeError, ValueError):
        pass

    if confirmation_decision is not None:
        try:
            result_payload = json.loads(replacement_content or "{}")
        except (TypeError, ValueError):
            result_payload = None
        if isinstance(result_payload, dict):
            result_payload[CONFIRMATION_DECISION_FIELD] = confirmation_decision
            replacement_content = json.dumps(result_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    stored_tool_result = InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=original_tool_call_id,
        content=replacement_content,
    )
    serialized_content = stored_tool_result.model_dump_json(exclude_none=True)
    if audit_record_id is not None:
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.source_assistant_message_id is None or pending_message.id is None:
            raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
        await audit_tool_result_version_crud.append_version(
            db,
            uid=pending_message.uid,
            session_id=pending_message.session_id,
            audit_record_id=audit_record_id,
            source_assistant_message_id=record.source_assistant_message_id,
            original_tool_call_id=original_tool_call_id,
            message_id=pending_message.id,
            content=serialized_content,
            commit=False,
        )
    else:
        updated = await message_crud.update_content_if_matches(
            db,
            message_id=pending_message.id,
            expected_content=pending_message.content,
            content=serialized_content,
            message_type=MessageType.TOOL_RESULT,
            commit=False,
        )
        if not updated:
            raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
        await session_crud.bump_context_content_revision(
            db,
            session_id=pending_message.session_id,
            uid=pending_message.uid,
            commit=False,
        )
    await db.refresh(pending_message)
    return replacement_content


async def _update_confirmation_tool_results(
    db: AsyncSession,
    *,
    audit_record_id: int,
    before_message_id: int | None,
    status: AuditRecordStatus,
    confirmation_status: str,
    feedback: str | None,
    confirmation_decision: str | None = None,
) -> int:
    record = await audit_crud.get_record(db, audit_record_id)
    if record is None or record.source_assistant_message_id is None:
        return 0

    source_message = await message_crud.get(db, record.source_assistant_message_id)
    if source_message is None or source_message.uid != record.uid or source_message.session_id != record.session_id or source_message.role != MessageRole.ASSISTANT or source_message.type != MessageType.TOOL_CALL:
        return 0
    try:
        source_internal = InternalMessage.model_validate_json(source_message.content or "{}")
    except ValueError:
        return 0
    tool_call_ids = {tool_call.id for tool_call in source_internal.tool_calls or []}
    if not tool_call_ids:
        return 0

    structured_messages = await _get_structured_tool_result_messages(
        db,
        uid=record.uid,
        session_id=record.session_id,
        audit_record_id=audit_record_id,
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
            before_id=before_message_id,
            limit=500,
        )
    updated_count = 0
    for message in messages:
        if message.type != MessageType.TOOL_RESULT:
            continue
        try:
            tool_result = InternalMessage.model_validate_json(message.content or "{}")
            result_payload = json.loads(tool_result.content or "{}")
        except (TypeError, ValueError):
            continue
        tool_call_id = message.audit_tool_call_id if structured else tool_result.tool_call_id
        if structured and tool_result.tool_call_id != tool_call_id:
            continue
        if tool_call_id not in tool_call_ids or not isinstance(result_payload, dict):
            continue
        if result_payload.get("status") != AuditRecordStatus.PENDING.value:
            continue
        result_payload.update(status=status.value, confirmation_status=confirmation_status)
        if feedback is not None:
            result_payload["error"] = feedback
        if status == AuditRecordStatus.REJECTED:
            result_payload["error"] = t(ERR_AUDIT_CONFIRMATION_REJECTED_BY_USER, locale=record.language)
            result_payload[REJECTION_SOURCE_FIELD] = "user"
        if confirmation_decision is not None:
            result_payload[CONFIRMATION_DECISION_FIELD] = confirmation_decision
        tool_result.content = json.dumps(result_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        serialized_content = tool_result.model_dump_json(exclude_none=True)
        if structured:
            if message.id is None:
                continue
            await audit_tool_result_version_crud.append_version(
                db,
                uid=record.uid,
                session_id=record.session_id,
                audit_record_id=audit_record_id,
                source_assistant_message_id=record.source_assistant_message_id,
                original_tool_call_id=tool_call_id,
                message_id=message.id,
                content=serialized_content,
                commit=False,
            )
            await db.refresh(message)
            updated_count += 1
        elif await message_crud.update_content(
            db,
            message_id=message.id,
            content=serialized_content,
            commit=False,
        ):
            await db.refresh(message)
            updated_count += 1
    if not structured and updated_count:
        await session_crud.bump_context_content_revision(
            db,
            session_id=record.session_id,
            uid=record.uid,
            commit=False,
        )
    return updated_count


async def _get_cancelled_structured_tool_results(
    db: AsyncSession,
    *,
    record,
) -> list[InternalMessage]:
    if record.id is None:
        raise LookupError(record.id)
    stored_messages = await _get_structured_tool_result_messages(
        db,
        uid=record.uid,
        session_id=record.session_id,
        audit_record_id=record.id,
    )
    if not stored_messages:
        return []
    if len(stored_messages) != record.tool_count:
        raise LookupError(record.id)

    tool_results: list[InternalMessage] = []
    tool_call_ids: set[str] = set()
    for stored_message in stored_messages:
        tool_result = InternalMessage.model_validate_json(stored_message.content or "{}")
        if tool_result.role != MessageRole.TOOL:
            raise ValueError(record.id)
        if not isinstance(tool_result.tool_call_id, str) or not tool_result.tool_call_id or tool_result.tool_call_id != stored_message.audit_tool_call_id or tool_result.tool_call_id in tool_call_ids:
            raise ValueError(record.id)
        if not isinstance(tool_result.content, str):
            raise ValueError(record.id)
        try:
            result_payload = json.loads(tool_result.content)
        except (TypeError, ValueError) as exc:
            raise ValueError(record.id) from exc
        if not isinstance(result_payload, dict) or result_payload.get("status") != AuditRecordStatus.CANCELLED.value:
            raise ValueError(record.id)

        tool_call_ids.add(tool_result.tool_call_id)
        tool_result.id = stored_message.id
        tool_result.created_at = stored_message.created_at
        tool_results.append(tool_result)
    return tool_results


async def cancel_persisted_pending_confirmation_bundle(
    db: AsyncSession,
    *,
    audit_record_id: int,
    uid: str,
    session_id: str,
    feedback: str,
    confirmation_status: str,
    commit: bool = True,
) -> PendingConfirmationCancellation:
    """原子取消已持久化的待确认审计，并同步工具结果和确认卡片。

    ``commit=False`` 时由调用方提交整个外层事务，并在成功后调用
    ``broadcast_pending_confirmation_cancellation``。
    """
    try:
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.uid != uid or record.session_id != session_id:
            raise LookupError(audit_record_id)

        closed = False
        if record.status == AuditRecordStatus.PENDING:
            closed = await audit_crud.close_pending(
                db,
                audit_record_id=audit_record_id,
                uid=uid,
                session_id=session_id,
                status=AuditRecordStatus.CANCELLED,
                error_reason=feedback,
                commit=False,
            )
            if closed:
                await _update_confirmation_tool_results(
                    db,
                    audit_record_id=audit_record_id,
                    before_message_id=None,
                    status=AuditRecordStatus.CANCELLED,
                    confirmation_status=confirmation_status,
                    feedback=feedback,
                )
            record = await audit_crud.get_record(db, audit_record_id)

        if record is None or record.uid != uid or record.session_id != session_id or record.status != AuditRecordStatus.CANCELLED:
            raise LookupError(audit_record_id)
        tool_results = await _get_cancelled_structured_tool_results(db, record=record)
        projection = await _sync_confirmation_message_status_projection(
            db,
            audit_record_id=audit_record_id,
            commit=False,
        )
        if projection is None:
            raise LookupError(audit_record_id)
        cancellation = PendingConfirmationCancellation(
            tool_results=tool_results,
            status_update=projection.status_update,
        )
        if commit:
            await db.commit()
    except Exception:
        if commit:
            await db.rollback()
        raise
    if commit:
        await broadcast_pending_confirmation_cancellation(db, cancellation=cancellation)
    return cancellation


async def supersede_persisted_pending_confirmation_bundle(
    db: AsyncSession,
    *,
    audit_record_id: int,
    uid: str,
    session_id: str,
) -> list[InternalMessage]:
    record = await audit_crud.get_record(db, audit_record_id)
    if record is None or record.uid != uid or record.session_id != session_id:
        raise LookupError(audit_record_id)
    cancellation = await cancel_persisted_pending_confirmation_bundle(
        db,
        audit_record_id=audit_record_id,
        uid=uid,
        session_id=session_id,
        feedback=t(MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE, locale=record.language),
        confirmation_status="superseded",
    )
    return cancellation.tool_results


async def update_confirmation_tool_results_for_decision(
    db: AsyncSession,
    *,
    audit_record_id: int,
    before_message_id: int,
    decision: ConfirmationDecision,
    raw_message: str,
) -> int:
    status = AuditRecordStatus.EXECUTING if decision in {ConfirmationDecision.APPROVE, ConfirmationDecision.IGNORE} else AuditRecordStatus.REJECTED
    return await _update_confirmation_tool_results(
        db,
        audit_record_id=audit_record_id,
        before_message_id=before_message_id,
        status=status,
        confirmation_status=decision.value,
        feedback=None,
        confirmation_decision=raw_message,
    )
