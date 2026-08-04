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
    audit_record_id: int | None = None,
    dedupe_key: str | None = None,
) -> InternalMessage:
    stored_tool_res = tool_res.model_copy()

    saved_msg = await save_message(
        db,
        session_id,
        uid,
        MessageRole.TOOL,
        MessageType.TOOL_RESULT,
        stored_tool_res,
        profile_id,
        is_processed=True,
        audit_record_id=audit_record_id,
        audit_tool_call_id=tool_res.tool_call_id if audit_record_id is not None else None,
        dedupe_key=dedupe_key,
    )
    stored_tool_res.id = saved_msg.id
    stored_tool_res.created_at = saved_msg.created_at
    messages.append(stored_tool_res)
    turn_messages.append(stored_tool_res)
    return stored_tool_res
