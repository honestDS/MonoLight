from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

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
):
    await save_message(
        db,
        session_id,
        uid,
        MessageRole.ASSISTANT,
        MessageType.TOOL_CALL if ai_msg.tool_calls else MessageType.TEXT,
        ai_msg,
        profile_id,
        is_processed=True,
    )
