import asyncio
import json
import os
import time
from typing import (
    Any,
)

from sqlalchemy.ext.asyncio import (
    AsyncSession,
)

from app.core.log import (
    LogManager,
    get_logger,
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
    allowed_knowledge_base_ids: list[int] | None = None,
    active_tasks: set[asyncio.Task] | None = None,
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
            # 传递配置给 Executor
            if hasattr(instance, "set_config"):
                instance.set_config(cfg)

            # 传递运行时上下文给 Executor
            if hasattr(instance, "set_runtime_context"):
                instance.set_runtime_context(
                    db=db,
                    profile=profile,
                    session_id=session_id,
                    allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                )

            current_coro = instance.execute(**args)
            task = asyncio.create_task(current_coro)
            if active_tasks is not None:
                active_tasks.add(task)

            start_time = time.perf_counter()
            try:
                cmd_result = await task
            except asyncio.CancelledError:
                duration = time.perf_counter() - start_time

                get_logger("dispatcher").bind(
                    uid=uid,
                    session_id=session_id,
                    tool_name=tool_name,
                    duration=f"{duration:.3f}s",
                ).warning(f"工具 {tool_name} 在执行 {duration:.3f}s 后被中止")

                if not task.done():
                    task.cancel()
                raise
            finally:
                if active_tasks is not None:
                    active_tasks.discard(task)
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
