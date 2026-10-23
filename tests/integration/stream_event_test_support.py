from copy import deepcopy
from typing import Any, Literal


def dispatcher_stream_event(
    event_type: Literal[
        "agent_loop_start",
        "content",
        "reasoning",
        "turn_end",
        "tool_start",
        "tool_end",
        "done",
    ],
    *,
    response_id: str,
    turn: int = 0,
    **fields: Any,
) -> dict[str, Any]:
    event_fields = deepcopy(fields)

    if event_type == "done":
        response = event_fields["response"]
        event_fields.update(
            {
                "type": event_type,
                "session_id": event_fields["session_id"],
                "response_id": response_id,
                "history": deepcopy(response.get("history", [])),
                "files": deepcopy(response.get("files")),
                "response": response,
            }
        )
        return event_fields

    event_fields.update({"type": event_type, "response_id": response_id})

    if event_type in {"agent_loop_start", "content", "reasoning", "turn_end"}:
        event_fields["turn"] = turn
    elif event_type == "tool_start":
        event_fields.setdefault("tool_call_index", 0)
        event_fields.setdefault("tool_call_count", 1)

    return event_fields
