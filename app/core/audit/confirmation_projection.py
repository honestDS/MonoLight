import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    MSG_AUDIT_CONFIRMATION_STATUS_IM,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.models.audit import AuditRecordStatus

from .confirmation_common import (
    _STATUS_TEXT_KEYS,
    ConfirmationMessageProjection,
    ConfirmationStatusUpdate,
)
from .confirmation_events import _broadcast_confirmation_status_update
from .confirmation_queries import _get_confirmation_message

__all__ = [
    "update_confirmation_message_status",
]


async def _sync_confirmation_message_status_projection(
    db: AsyncSession,
    *,
    audit_record_id: int,
    commit: bool,
) -> ConfirmationMessageProjection | None:
    for _ in range(3):
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.id is None:
            return None
        status = record.status.value if isinstance(record.status, AuditRecordStatus) else str(record.status)
        if status == AuditRecordStatus.PREPARING.value:
            return None
        message, payload = await _get_confirmation_message(db, record)
        if message is None or message.id is None or payload is None:
            return None
        if str(payload.get("status") or "") == status:
            return ConfirmationMessageProjection(record=record, status_update=None)

        payload["status"] = status
        status_key = _STATUS_TEXT_KEYS.get(status)
        if status_key:
            payload["plain_text"] = t(
                MSG_AUDIT_CONFIRMATION_STATUS_IM,
                locale=record.language,
                summary=str(payload.get("summary") or ""),
                status=t(status_key, locale=record.language),
            )
        serialized_payload = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        updated = await message_crud.update_content_if_matches(
            db,
            message_id=message.id,
            expected_content=message.content,
            content=serialized_payload,
            commit=False,
        )
        if not updated:
            continue

        if commit:
            await db.commit()
        else:
            await db.flush()
        return ConfirmationMessageProjection(
            record=record,
            status_update=ConfirmationStatusUpdate(
                record=record,
                message_id=message.id,
                status=status,
                content=serialized_payload,
            ),
        )
    return None


async def update_confirmation_message_status(
    db: AsyncSession,
    *,
    audit_record_id: int,
    commit: bool = True,
) -> bool:
    projection = await _sync_confirmation_message_status_projection(
        db,
        audit_record_id=audit_record_id,
        commit=commit,
    )
    if projection is None or projection.status_update is None:
        return False
    if commit:
        await _broadcast_confirmation_status_update(db, status_update=projection.status_update)
    return True
