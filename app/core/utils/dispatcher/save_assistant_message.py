import asyncio
from datetime import datetime

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.constants import (
    ERR_SESSION_REPLY_LEASE_LOST,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.session import session_crud
from app.core.i18n import t
from app.core.utils.dispatcher.process_markdown_response import process_markdown_response
from app.core.utils.dispatcher.save_message import save_message
from app.models.message import (
    InternalMessage,
    MessageRole,
    MessageType,
)


async def save_assistant_message(
    db: AsyncSession,
    session_id: str,
    uid: str,
    profile_id: int,
    ai_msg: InternalMessage,
    dedupe_key: str | None = None,
    created_at: datetime | None = None,
):
    claim_info = db.info.get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
    if claim_info:
        work_id, worker_id = claim_info
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={},
            commit=False,
        )
        if not updated:
            await db.rollback()
            raise asyncio.CancelledError(t(ERR_SESSION_REPLY_LEASE_LOST))

    session = await session_crud.get_by_session_id(db, session_id)
    enable_markdown = session.enable_markdown if session else False

    # 清洗 Markdown 标记
    ai_msg = process_markdown_response(ai_msg, enable_markdown)

    saved_msg = await save_message(
        db,
        session_id,
        uid,
        MessageRole.ASSISTANT,
        MessageType.TOOL_CALL if ai_msg.tool_calls else MessageType.TEXT,
        ai_msg,
        profile_id,
        is_processed=True,
        dedupe_key=dedupe_key,
        created_at=created_at,
    )
    ai_msg.id = saved_msg.id
    ai_msg.created_at = saved_msg.created_at
    return saved_msg
