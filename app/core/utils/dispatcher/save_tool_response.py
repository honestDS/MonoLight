from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.utils.dispatcher.save_message import save_message
from app.models.message import (
    InternalMessage,
    MessageRole,
    MessageType,
)


async def save_tool_response(
    db: AsyncSession,
    session_id: str,
    uid: str,
    profile_id: int,
    tool_res: InternalMessage,
    messages: list[InternalMessage],
    turn_messages: list[InternalMessage],
):
    messages.append(tool_res)
    turn_messages.append(tool_res)
    await save_message(
        db, session_id, uid, MessageRole.TOOL, MessageType.TOOL_RESULT, tool_res, profile_id, is_processed=True
    )
