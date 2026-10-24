import json

from app.core.utils.dispatcher.session_todo_snapshot import strip_session_todo_snapshot
from app.models.message import InternalMessage, InternalToolCall


def build_external_wait_response(
    tool_calls: list[InternalToolCall],
    tool_results: list[InternalMessage],
    response_id: str | None = None,
) -> dict | None:
    tool_calls_by_id = {tool_call.id: tool_call for tool_call in tool_calls}
    pending_tools: list[dict] = []

    for tool_result in tool_results:
        try:
            payload = json.loads(strip_session_todo_snapshot(tool_result.content) or "")
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


__all__ = ["build_external_wait_response"]
