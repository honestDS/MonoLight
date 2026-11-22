import json

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.session.message import message_crud
from app.models.message import Message, MessageType

__all__ = []


async def _get_structured_tool_result_messages(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    audit_record_id: int,
) -> list[Message]:
    result = await db.execute(
        select(Message)
        .where(
            Message.uid == uid,
            Message.session_id == session_id,
            Message.type == MessageType.TOOL_RESULT,
            Message.audit_record_id == audit_record_id,
            Message.audit_tool_call_id.is_not(None),
        )
        .order_by(Message.id.asc())
        .execution_options(populate_existing=True)
    )
    return list(result.scalars().all())


async def _get_confirmation_message(db: AsyncSession, record) -> tuple[Message | None, dict | None]:
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
            return candidate, candidate_payload
    return None, None
