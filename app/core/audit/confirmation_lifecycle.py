from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    ERR_AUDIT_CONFIRMATION_EXPIRED,
    MSG_AUDIT_CONFIRMATION_SUPERSEDED,
)
from app.core.crud.audit.audit import audit_crud
from app.core.i18n import t
from app.models.audit import AuditRecordStatus

from .confirmation_projection import update_confirmation_message_status
from .confirmation_results import (
    _update_confirmation_tool_results,
    cancel_persisted_pending_confirmation_bundle,
)

__all__ = [
    "sync_expired_confirmation_messages",
    "expire_confirmation_by_session",
    "cancel_confirmation_by_session",
]


async def sync_expired_confirmation_messages(
    db: AsyncSession,
    *,
    audit_record_id: int,
    locale: str | None,
) -> None:
    await _update_confirmation_tool_results(
        db,
        audit_record_id=audit_record_id,
        before_message_id=None,
        status=AuditRecordStatus.EXPIRED,
        confirmation_status=AuditRecordStatus.EXPIRED.value,
        feedback=t(ERR_AUDIT_CONFIRMATION_EXPIRED, locale=locale),
    )
    await db.commit()
    await update_confirmation_message_status(db, audit_record_id=audit_record_id)
    await db.commit()


async def expire_confirmation_by_session(db: AsyncSession, *, uid: str, session_id: str) -> int:
    record = await audit_crud.get_confirmation_claim(db, uid=uid, session_id=session_id)
    expired_count = await audit_crud.expire_confirmation_by_session(db, uid=uid, session_id=session_id)
    if expired_count and record is not None and record.id is not None:
        await sync_expired_confirmation_messages(
            db,
            audit_record_id=record.id,
            locale=record.language,
        )
    return expired_count


async def cancel_confirmation_by_session(db: AsyncSession, *, uid: str, session_id: str, locale: str | None = None) -> int:
    record = await audit_crud.get_confirmation_claim(db, uid=uid, session_id=session_id)
    if record is None or record.id is None:
        return 0
    await cancel_persisted_pending_confirmation_bundle(
        db,
        audit_record_id=record.id,
        uid=uid,
        session_id=session_id,
        feedback=t(MSG_AUDIT_CONFIRMATION_SUPERSEDED, locale=locale),
        confirmation_status="superseded",
    )
    return 1
