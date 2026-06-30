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

from app.core.dispatch_context import build_dispatch_context
from app.core.i18n import t
from app.core.log import (
    LogManager,
    get_logger,
)
from app.core.prompts import BACKGROUND_TASK_QUEUED_PROMPT, BACKGROUND_TASK_UNSUPPORTED_PROMPT
from app.core.tools import (
    TOOL_EXECUTOR_MAP,
    tool_schema_has_parameter,
)
from app.core.utils.dispatcher.audit_tool_call import audit_tool_call
from app.core.utils.dispatcher.helpers import format_exception_message
from app.core.utils.dispatcher.truncate_tool_result import truncate_tool_messages_for_budget
from app.models.message import (
    InternalMessage,
    MessageRole,
)
from app.models.profile import (
    Profile,
    ProfileConfig,
)


def _is_tool_enabled(tool_name: str, cfg: ProfileConfig) -> bool:
    enabled_tools = getattr(getattr(cfg, "tool", None), "enabled_tools", None)
    if not isinstance(enabled_tools, list):
        return False
    return tool_name in {name for name in enabled_tools if isinstance(name, str)}


def _build_tool_disabled_result(tool_name: str) -> str:
    return json.dumps(
        {
            "error": f"Tool {tool_name} is not enabled in the active profile",
            "tool_name": tool_name,
            "status": "failed",
        },
        ensure_ascii=False,
    )


def _build_background_task_queued_result(tool_name: str, task_id: int) -> str:
    return json.dumps(
        {
            "status": "queued",
            "tool_name": tool_name,
            "task_id": task_id,
            "message": BACKGROUND_TASK_QUEUED_PROMPT.format(tool_name=tool_name),
        },
        ensure_ascii=False,
    )


def _build_background_task_unsupported_result(tool_name: str) -> str:
    return json.dumps(
        {
            "status": "failed",
            "tool_name": tool_name,
            "error": f"Tool {tool_name} does not support background execution.",
            "instruction": BACKGROUND_TASK_UNSUPPORTED_PROMPT.format(tool_name=tool_name),
        },
        ensure_ascii=False,
    )


def _build_tool_error_result(tool_name: str, error_message: str) -> str:
    return json.dumps(
        {
            "status": "failed",
            "tool_name": tool_name,
            "error": error_message,
        },
        ensure_ascii=False,
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
    context_window_k: int = 4,
    allow_background_submission: bool = True,
) -> InternalMessage:
    tool_name = tool_call.name
    args = dict(tool_call.arguments or {})
    run_in_background = bool(args.pop("run_in_background", False))

    LogManager.log_tool_call(turn, tool_name, json.dumps(args, ensure_ascii=False), session_id, uid)

    if not _is_tool_enabled(tool_name, cfg):
        cmd_result = _build_tool_disabled_result(tool_name)
    elif run_in_background and (not allow_background_submission or not tool_schema_has_parameter(tool_name, "run_in_background")):
        cmd_result = _build_background_task_unsupported_result(tool_name)
    else:
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

    if cmd_result is None and run_in_background:
        from app.core.background_tasks.manager import background_task_manager

        task = await background_task_manager.submit(
            db,
            uid=uid,
            session_id=session_id,
            profile=profile,
            tool_call_id=tool_call.id,
            tool_name=tool_name,
            arguments=args,
            allowed_knowledge_base_ids=allowed_knowledge_base_ids,
            source="llm_tool_call",
        )
        cmd_result = _build_background_task_queued_result(tool_name, task.id)

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
                dispatch_context = build_dispatch_context(
                    mode="interactive",
                    source="interactive_tool",
                    uid=uid,
                    session_id=session_id,
                    profile=profile,
                    db=db,
                    allowed_knowledge_base_ids=allowed_knowledge_base_ids,
                )
                instance.set_runtime_context(
                    dispatch_context=dispatch_context,
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
                ).warning(t("LOG_TOOL_ABORTED", tool_name=tool_name, duration=f"{duration:.3f}s"))

                if not task.done():
                    task.cancel()
                raise
            except Exception as exc:
                cmd_result = _build_tool_error_result(tool_name, format_exception_message(exc))
            finally:
                if active_tasks is not None:
                    active_tasks.discard(task)
        else:
            cmd_result = json.dumps(
                {"error": f"Tool {tool_name} not registered"},
                ensure_ascii=False,
            )

    tool_msg = InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=tool_call.id,
        content=cmd_result,
    )
    truncation_stats = truncate_tool_messages_for_budget(
        tool_msgs=[tool_msg],
        context_window_k=context_window_k,
        budget_tokens=max(1, (context_window_k * 1024) // 2),
        uid=uid,
        session_id=session_id,
    )
    if truncation_stats.truncated_count:
        get_logger("dispatcher").bind(
            uid=uid,
            session_id=session_id,
            tool_name=tool_name,
        ).warning(t("LOG_TOOL_RESULT_TRUNCATED", tool_name=tool_name, context_window_k=context_window_k))

    # 使用原始工具结果记录日志，确保文件日志保留完整数据用于审计；
    # ws/db sink 中的字符级截断仅影响前端推送与数据库存储，不影响文件日志
    LogManager.log_tool_result(turn, cmd_result, session_id, uid)

    return tool_msg
