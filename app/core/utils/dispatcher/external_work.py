import json

from app.core.constants import (
    ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN,
    SESSION_REPLY_WORK_CLAIM_INFO_KEY,
    SHELL_TOOL_NAME,
    TERMINAL_TOOL_NAME,
    TERMINAL_WATCH_TOOL_NAME,
)
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.utils.dispatcher.session_todo_snapshot import strip_session_todo_snapshot
from app.core.utils.dispatcher.terminal_watch_snapshot import strip_terminal_watch_snapshot
from app.models.message import InternalMessage, InternalToolCall
from app.models.session_reply_work_item import SessionReplyWorkStatus


def build_external_wait_response(
    tool_calls: list[InternalToolCall],
    tool_results: list[InternalMessage],
    response_id: str | None = None,
) -> dict | None:
    tool_calls_by_id = {tool_call.id: tool_call for tool_call in tool_calls}
    pending_tools: list[dict] = []

    for tool_result in tool_results:
        try:
            payload = json.loads(strip_session_todo_snapshot(strip_terminal_watch_snapshot(tool_result.content)) or "")
        except (TypeError, ValueError):
            continue

        if not isinstance(payload, dict) or payload.get("status") != "queued":
            continue
        task_id = payload.get("task_id")
        if not isinstance(task_id, int) or isinstance(task_id, bool) or task_id <= 0:
            continue

        tool_call = tool_calls_by_id.get(tool_result.tool_call_id)
        if tool_call is None:
            continue
        pending_tools.append(
            {
                "task_id": task_id,
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "tool_result_message_id": tool_result.id,
                "response_id": response_id,
            }
        )

    if not pending_tools:
        return None
    return {
        "status": "waiting_external_work",
        "pending_tools": pending_tools,
    }


async def build_terminal_wait_response(
    db,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    tool_calls: list[InternalToolCall],
    tool_results: list[InternalMessage],
    response_id: str | None = None,
) -> dict | None:
    pure_response = build_external_wait_response(tool_calls, tool_results, response_id)
    if pure_response is not None:
        for pending_tool in pure_response["pending_tools"]:
            if pending_tool.get("tool_name") == TERMINAL_WATCH_TOOL_NAME:
                pending_tool["terminal_watch"] = True

    claim_info = db.info.get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
    if claim_info is None:
        return pure_response
    if not isinstance(claim_info, (tuple, list)) or len(claim_info) != 2 or not isinstance(claim_info[0], int) or isinstance(claim_info[0], bool) or claim_info[0] <= 0 or not isinstance(claim_info[1], str) or not claim_info[1]:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))

    origin_work_id, worker_id = claim_info
    tool_calls_by_id = {tool_call.id: tool_call for tool_call in tool_calls}
    terminal_candidates: dict[str, tuple[InternalToolCall, InternalMessage]] = {}
    for tool_result in tool_results:
        tool_call = tool_calls_by_id.get(tool_result.tool_call_id)
        if (
            tool_call is None
            or tool_call.name
            not in {
                SHELL_TOOL_NAME,
                TERMINAL_TOOL_NAME,
            }
            or (tool_call.name == TERMINAL_TOOL_NAME and tool_call.arguments.get("operation") not in ("read", "write"))
        ):
            continue

        content = strip_session_todo_snapshot(strip_terminal_watch_snapshot(tool_result.content))
        try:
            payload = json.loads(content or "")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue

        terminal_session_id = payload.get("terminal_session_id")
        if not isinstance(terminal_session_id, str) or not terminal_session_id or payload.get("error") or payload.get("status") == "failed" or payload.get("status") == "execution_unknown" or tool_result.tool_execution_status == "failed" or tool_result.tool_execution_status == "execution_unknown":
            continue
        terminal_candidates.setdefault(terminal_session_id, (tool_call, tool_result))
    if not terminal_candidates:
        return pure_response

    origin_work = await session_reply_work_item_crud.get(db, origin_work_id)
    if origin_work is None or origin_work.uid != uid or origin_work.session_id != session_id or origin_work.profile_id != profile_id or origin_work.status != SessionReplyWorkStatus.RUNNING or origin_work.locked_by != worker_id:
        raise RuntimeError(t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
    origin_work_identity = origin_work.dedupe_key

    pending_watch_results = None
    observed_tools: list[dict] = []
    for terminal_session_id, (tool_call, tool_result) in terminal_candidates.items():
        active_watch = await background_task_crud.get_active_terminal_watch(
            db,
            uid,
            session_id,
            terminal_session_id,
            automatic_only=True,
        )
        if active_watch is not None and active_watch.profile_id == profile_id and active_watch.extra.get("origin_work_id") == origin_work_id and active_watch.extra.get("origin_work_identity") == origin_work_identity:
            watch_task = active_watch
        else:
            if pending_watch_results is None:
                pending_watch_results = await background_task_crud.list_pending_terminal_watch_results(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    origin_work_id=origin_work_id,
                    origin_work_identity=origin_work_identity,
                )
            watch_task = None
            for candidate in reversed(pending_watch_results):
                if candidate.arguments.get("terminal_session_id") == terminal_session_id:
                    watch_task = candidate
                    break

        if watch_task is None:
            continue
        observed_tools.append(
            {
                "task_id": watch_task.id,
                "tool_call_id": tool_call.id,
                "tool_name": tool_call.name,
                "tool_result_message_id": tool_result.id,
                "response_id": response_id,
                "terminal_watch": True,
                "terminal_auto_watch": True,
                "terminal_session_id": terminal_session_id,
            }
        )

    pending_tools_by_id: dict[int, dict] = {}
    if pure_response is not None:
        for pending_tool in pure_response["pending_tools"]:
            pending_tools_by_id.setdefault(pending_tool["task_id"], pending_tool)
    for observed_tool in observed_tools:
        pending_tools_by_id.setdefault(observed_tool["task_id"], observed_tool)

    if not pending_tools_by_id:
        return None
    return {
        "status": "waiting_external_work",
        "pending_tools": list(pending_tools_by_id.values()),
    }


__all__ = ["build_external_wait_response", "build_terminal_wait_response"]
