import json

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.prompts import (
    ERR_PARALLEL_LIMIT_EXCEEDED,
)
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.core.utils.dispatcher.session_todo_snapshot import persist_session_todo_snapshot_on_tool_results
from app.models.message import (
    InternalMessage,
    MessageRole,
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
    stored_tool_results: list[InternalMessage] = []
    for tool_call in ai_msg.tool_calls:
        tool_res = InternalMessage(role=MessageRole.TOOL, tool_call_id=tool_call.id, content=error_msg)
        stored_tool_res = await save_tool_response(
            db,
            session_id,
            uid,
            profile.id,
            tool_res,
            messages,
            turn_messages,
        )
        stored_tool_results.append(stored_tool_res)
        last_message_id = stored_tool_res.id
    await persist_session_todo_snapshot_on_tool_results(
        db,
        uid=uid,
        session_id=session_id,
        tool_results=stored_tool_results,
    )
    return last_message_id
