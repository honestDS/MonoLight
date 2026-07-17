import json
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import (
    MSG_AUDIT_CONFIRMATION_STATUS_IM,
    MSG_AUDIT_STATUS_CANCELLED,
    MSG_AUDIT_STATUS_EXECUTING,
    MSG_AUDIT_STATUS_EXECUTION_UNKNOWN,
    MSG_AUDIT_STATUS_EXPIRED,
    MSG_AUDIT_STATUS_FAILED,
    MSG_AUDIT_STATUS_REJECTED,
    MSG_AUDIT_STATUS_SUCCEEDED,
)
from app.core.crud.audit import audit_crud
from app.core.crud.message import message_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.message_platforms.notifier import send_session_event
from app.models.audit import AuditRecordStatus
from app.models.message import MessageType


class ConfirmationDecision(StrEnum):
    APPROVE = "approve"
    REJECT = "reject"


_APPROVE_WORDS = {"同意", "继续", "approve", "continue"}
_REJECT_WORDS = {"拒绝", "reject"}
_STATUS_TEXT_KEYS = {
    "executing": MSG_AUDIT_STATUS_EXECUTING,
    "rejected": MSG_AUDIT_STATUS_REJECTED,
    "expired": MSG_AUDIT_STATUS_EXPIRED,
    "cancelled": MSG_AUDIT_STATUS_CANCELLED,
    "succeeded": MSG_AUDIT_STATUS_SUCCEEDED,
    "failed": MSG_AUDIT_STATUS_FAILED,
    "execution_unknown": MSG_AUDIT_STATUS_EXECUTION_UNKNOWN,
}

logger = get_logger(__name__)


def parse_confirmation_decision(
    message: object,
    *,
    attachments: list[str] | None = None,
    has_quote: bool = False,
) -> ConfirmationDecision | None:
    if not isinstance(message, str) or attachments or has_quote:
        return None
    normalized = message.strip()
    if not normalized:
        return None
    lowered = normalized.lower()
    if lowered in _APPROVE_WORDS:
        return ConfirmationDecision.APPROVE
    if lowered in _REJECT_WORDS:
        return ConfirmationDecision.REJECT
    return None


def message_has_quote(raw_message: object) -> bool:
    if not isinstance(raw_message, dict):
        return False
    quote_keys = {
        "quote",
        "quoted_message",
        "reference",
        "refer_message",
        "ref_message",
        "reply_to",
    }
    if any(raw_message.get(key) for key in quote_keys):
        return True
    item_list = raw_message.get("item_list")
    if not isinstance(item_list, list):
        return False
    return any(isinstance(item, dict) and (str(item.get("type") or "").lower() in {"quote", "reference", "reply"} or any(item.get(key) for key in quote_keys)) for item in item_list)


async def update_confirmation_message_status(
    db: AsyncSession,
    *,
    audit_record_id: int,
) -> bool:
    for _ in range(3):
        record = await audit_crud.get_record(db, audit_record_id)
        if record is None or record.id is None:
            return False
        status = record.status.value if isinstance(record.status, AuditRecordStatus) else str(record.status)
        if status == AuditRecordStatus.PREPARING.value:
            return False
        message = None
        payload = None
        for candidate in await message_crud.list_by_type(
            db,
            uid=record.uid,
            session_id=record.session_id,
            message_type=MessageType.AUDIT_CONFIRMATION,
        ):
            try:
                candidate_payload = json.loads(candidate.content or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(candidate_payload, dict) and str(candidate_payload.get("audit_record_id")) == str(record.id):
                message = candidate
                payload = candidate_payload
                break
        if message is None or message.id is None or payload is None:
            return False
        if str(payload.get("status") or "") == status:
            return False

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
        )
        if not updated:
            await db.rollback()
            continue

        event = {
            "type": "audit_confirmation_status",
            "source": "audit_confirmation",
            "event_id": f"audit-confirmation:{record.id}:{status}",
            "session_id": record.session_id,
            "audit_record_id": record.id,
            "message_id": message.id,
            "status": status,
            "content": serialized_payload,
        }
        try:
            await send_session_event(record.uid, record.session_id, event)
        except Exception:
            logger.bind(uid=record.uid, session_id=record.session_id, audit_record_id=record.id, status=status).warning(
                "Failed to broadcast audit confirmation status",
                exc_info=True,
            )
        return True
    return False


async def expire_confirmation_by_session(db: AsyncSession, *, uid: str, session_id: str) -> int:
    record = await audit_crud.get_confirmation_claim(db, uid=uid, session_id=session_id)
    expired_count = await audit_crud.expire_confirmation_by_session(db, uid=uid, session_id=session_id)
    if expired_count:
        if record is not None and record.id is not None:
            await update_confirmation_message_status(db, audit_record_id=record.id)
    return expired_count
