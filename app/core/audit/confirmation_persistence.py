import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_AUDIT_EXECUTION_CLAIM_FAILED,
    MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.audit.tool_result_version import audit_tool_result_version_crud
from app.core.i18n import t
from app.core.utils.dispatcher.save_message import save_message
from app.models.audit import AuditRecordStatus
from app.models.message import InternalMessage, MessageRole, MessageType

__all__ = [
    "persist_pending_confirmation_bundle",
    "persist_cancelled_pending_audit_results",
]


async def persist_pending_confirmation_bundle(
    db: AsyncSession,
    *,
    audit_record_id: int,
    uid: str,
    session_id: str,
    profile_id: int,
    tool_results: list[InternalMessage] | tuple[InternalMessage, ...],
    confirmation_payload: dict[str, Any],
    dedupe_key: str | None,
) -> tuple[list[InternalMessage], InternalMessage]:
    try:
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.source_assistant_message_id is None:
            raise LookupError(audit_record_id)
        stored_tool_results: list[InternalMessage] = []
        for tool_result in tool_results:
            stored_tool_result = tool_result.model_copy()
            saved_message = await save_message(
                db,
                session_id,
                uid,
                MessageRole.TOOL,
                MessageType.TOOL_RESULT,
                stored_tool_result,
                profile_id,
                is_processed=True,
                audit_record_id=audit_record_id,
                audit_tool_call_id=stored_tool_result.tool_call_id,
                commit=False,
            )
            if saved_message.id is None or not isinstance(stored_tool_result.tool_call_id, str):
                raise LookupError(audit_record_id)
            await audit_tool_result_version_crud.append_version(
                db,
                uid=uid,
                session_id=session_id,
                audit_record_id=audit_record_id,
                source_assistant_message_id=record.source_assistant_message_id,
                original_tool_call_id=stored_tool_result.tool_call_id,
                message_id=saved_message.id,
                content=saved_message.content or "",
                commit=False,
            )
            stored_tool_result.id = saved_message.id
            stored_tool_result.created_at = saved_message.created_at
            stored_tool_results.append(stored_tool_result)

        confirmation_message = await save_message(
            db,
            session_id,
            uid,
            MessageRole.ASSISTANT,
            MessageType.AUDIT_CONFIRMATION,
            confirmation_payload,
            profile_id,
            is_processed=True,
            dedupe_key=dedupe_key,
            commit=False,
        )
        activated = await audit_crud.activate_confirmation_claim(
            db,
            audit_record_id=audit_record_id,
            uid=uid,
            session_id=session_id,
            commit=False,
        )
        if not activated:
            raise RuntimeError(t(ERR_AUDIT_EXECUTION_CLAIM_FAILED))
        await db.commit()
        return stored_tool_results, confirmation_message
    except Exception as exc:
        await db.rollback()
        try:
            await audit_crud.mark_pending_persistence_failed(
                db,
                audit_record_id=audit_record_id,
                error_reason=str(exc),
            )
        except Exception:
            pass
        raise


async def persist_cancelled_pending_audit_results(
    db: AsyncSession,
    *,
    audit_record_id: int,
    uid: str,
    session_id: str,
    profile_id: int,
    tool_results: list[InternalMessage] | tuple[InternalMessage, ...],
) -> list[InternalMessage]:
    try:
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.status != AuditRecordStatus.PENDING or record.uid != uid or record.session_id != session_id or record.source_assistant_message_id is None:
            raise LookupError(audit_record_id)

        cancellation_reason = t(MSG_AUDIT_CONFIRMATION_CANCELLED_BY_USER_MESSAGE, locale=record.language)
        stored_tool_results: list[InternalMessage] = []
        for tool_result in tool_results:
            stored_tool_result = tool_result.model_copy(deep=True)
            if stored_tool_result.role != MessageRole.TOOL or not isinstance(stored_tool_result.tool_call_id, str):
                raise LookupError(audit_record_id)
            try:
                result_payload = json.loads(stored_tool_result.content or "{}")
            except (TypeError, ValueError) as exc:
                raise ValueError from exc
            if not isinstance(result_payload, dict) or result_payload.get("status") != AuditRecordStatus.PENDING.value:
                raise ValueError
            result_payload.update(
                status=AuditRecordStatus.CANCELLED.value,
                confirmation_status="superseded",
                error=cancellation_reason,
            )
            stored_tool_result.content = json.dumps(result_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            saved_message = await save_message(
                db,
                session_id,
                uid,
                MessageRole.TOOL,
                MessageType.TOOL_RESULT,
                stored_tool_result,
                profile_id,
                is_processed=True,
                audit_record_id=audit_record_id,
                audit_tool_call_id=stored_tool_result.tool_call_id,
                commit=False,
            )
            if saved_message.id is None:
                raise LookupError(audit_record_id)
            await audit_tool_result_version_crud.append_version(
                db,
                uid=uid,
                session_id=session_id,
                audit_record_id=audit_record_id,
                source_assistant_message_id=record.source_assistant_message_id,
                original_tool_call_id=stored_tool_result.tool_call_id,
                message_id=saved_message.id,
                content=saved_message.content or "",
                commit=False,
            )
            stored_tool_result.id = saved_message.id
            stored_tool_result.created_at = saved_message.created_at
            stored_tool_results.append(stored_tool_result)

        closed = await audit_crud.close_pending(
            db,
            audit_record_id=audit_record_id,
            uid=uid,
            session_id=session_id,
            status=AuditRecordStatus.CANCELLED,
            error_reason=cancellation_reason,
            commit=False,
        )
        if not closed:
            raise LookupError(audit_record_id)
        await db.commit()
        return stored_tool_results
    except Exception as exc:
        await db.rollback()
        try:
            await audit_crud.mark_pending_persistence_failed(
                db,
                audit_record_id=audit_record_id,
                error_reason=str(exc),
            )
        except Exception:
            pass
        raise
