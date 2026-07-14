import json

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.prompts import (
    ERR_PARALLEL_LIMIT_EXCEEDED,
)
from app.core.utils.dispatcher.save_message import save_message
from app.models.message import (
    InternalMessage,
    MessageRole,
    MessageType,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)


async def handle_parallel_tool_limit(
    db: AsyncSession,
    session_id: str,
    uid: str,
    profile: Profile,
    cfg: ProfileConfig,
    ai_msg: InternalMessage,
    messages: list[InternalMessage],
    turn_messages: list[InternalMessage],
) -> int | None:
    error_msg = json.dumps(
        {
            "error": "parallel_limit_exceeded",
            "message": ERR_PARALLEL_LIMIT_EXCEEDED.format(requested=len(ai_msg.tool_calls), limit=cfg.tool.max_parallel_tools),
        },
        ensure_ascii=False,
    )
    last_message_id: int | None = None
    for tool_call in ai_msg.tool_calls:
        tool_res = InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content=error_msg)
        saved_msg = await save_message(db, session_id, uid, MessageRole.TOOL, MessageType.TOOL_RESULT, tool_res, profile.id)
        tool_res.id = saved_msg.id
        tool_res.created_at = saved_msg.created_at
        messages.append(tool_res)
        turn_messages.append(tool_res)
        last_message_id = saved_msg.id
    return last_message_id
