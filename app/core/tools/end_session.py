from typing import Any

from app.core.constants import END_SESSION_TOOL_NAME

END_SESSION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": END_SESSION_TOOL_NAME,
        "description": (
            "Call this tool exactly once by itself after completing the task, when the task cannot proceed safely, or when user input is required. "
            "This tool takes no arguments and only signals that the execution phase has ended. Do not include a user-facing final reply in the same response; "
            "the system will request the final reply separately."
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
