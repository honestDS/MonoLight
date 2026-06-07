import json
import os
from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.log import (
    LogManager,
)
from app.core.tools import (
    TOOL_EXECUTOR_MAP,
)
from app.core.utils.dispatcher.audit_tool_call import audit_tool_call
from app.models.message import (
    InternalMessage,
    MessageRole,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)


async def process_single_tool(
    tool_call: Any,
    db: AsyncSession,
    profile: Profile,
    cfg: ProfileConfig,
    messages: list[InternalMessage],
    username: str,
    session_id: str,
    turn: int,
    uid: str,
) -> InternalMessage:
    tool_name = tool_call.name
    args = tool_call.arguments

    LogManager.log_tool_call(turn, tool_name, json.dumps(args, ensure_ascii=False), session_id, uid)

    cmd_result = await audit_tool_call(
        db,
        profile,
        cfg,
        tool_name,
        args,
        messages,
        session_id=session_id,
        uid=uid,
    )

    if cmd_result is None:
        executor_cls = TOOL_EXECUTOR_MAP.get(tool_name)
        if executor_cls:
            instance = executor_cls(
                project_root=os.getcwd(),
                uid=uid,
            )
            cmd_result = await instance.execute(**args)
        else:
            cmd_result = json.dumps(
                {"error": f"Tool {tool_name} not registered"},
                ensure_ascii=False,
            )

    LogManager.log_tool_result(turn, cmd_result, session_id, uid)

    return InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=tool_call.id,
        content=cmd_result,
    )
