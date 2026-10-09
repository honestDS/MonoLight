from typing import Any

from app.core.constants import END_SESSION_TOOL_NAME

END_SESSION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": END_SESSION_TOOL_NAME,
        "description": (
            "This is a control signal for goal mode to end the current task execution/tool loop. It does not delete or close a chat session and does not mean the task succeeded. "
            "Use it when the task is complete, no further useful and authorized tool action is needed, continuation is unsafe, or user input is required; do not call unrelated tools merely to continue. "
            "Invoke it exactly once as a real structured tool call, with this as the only tool call in the response and arguments exactly {}. Do not include a summary or any other fields, and do not merely name the tool or simulate JSON in text. "
            "Do not provide a user-facing final reply in the same response; after accepting this signal, the system will request the final reply separately."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
            "required": [],
            "additionalProperties": False,
        },
    },
}


def is_end_session_signal(message: Any) -> bool:
    """Return whether the message is exactly one parameterless end-session call."""
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
    if not isinstance(tool_calls, (list, tuple)) or len(tool_calls) != 1:
        return False

    tool_call = tool_calls[0]
    name = tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None)
    if name != END_SESSION_TOOL_NAME:
        return False

    arguments = tool_call.get("arguments") if isinstance(tool_call, dict) else getattr(tool_call, "arguments", None)
    return isinstance(arguments, dict) and not arguments


__all__ = [
    "END_SESSION_TOOL_NAME",
    "END_SESSION_TOOL_SCHEMA",
    "is_end_session_signal",
]
