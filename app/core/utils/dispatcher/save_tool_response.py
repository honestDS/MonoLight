from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.tools.send_file_to_user import sanitize_files_to_user_result
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
) -> InternalMessage:
    stored_tool_res = tool_res.model_copy()
    stored_tool_res.content = sanitize_files_to_user_result(stored_tool_res.content)

    saved_msg = await save_message(
        db,
        session_id,
        uid,
        MessageRole.TOOL,
        MessageType.TOOL_RESULT,
        stored_tool_res,
        profile_id,
        is_processed=True,
    )
    stored_tool_res.id = saved_msg.id
    stored_tool_res.created_at = saved_msg.created_at
    messages.append(stored_tool_res)
    turn_messages.append(stored_tool_res)
    return stored_tool_res
