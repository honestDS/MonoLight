import json
from typing import Any

from app.core.constants import END_SESSION_TOOL_NAME
from app.core.tools.base import BaseExecutor

END_SESSION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": END_SESSION_TOOL_NAME,
        "description": "Call this tool exactly once by itself after completing the task. The summary is persisted as the user-visible final reply; do not output final text outside the tool call. This ends the current response only and does not delete or permanently close the chat history.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {
                    "type": "string",
                    "minLength": 1,
                    "pattern": r"[\s\S]*\S[\s\S]*",
                },
            },
            "required": ["summary"],
            "additionalProperties": False,
        },
    },
}


def extract_end_session_summary(message: Any) -> str | None:
    """Return the validated end-session summary from a sole matching tool call."""
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
    if not isinstance(tool_calls, (list, tuple)) or len(tool_calls) != 1:
        return None

    tool_call = tool_calls[0]
    name = tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None)
    if name != END_SESSION_TOOL_NAME:
        return None

    arguments = tool_call.get("arguments") if isinstance(tool_call, dict) else getattr(tool_call, "arguments", None)
    if not isinstance(arguments, dict) or set(arguments) != {"summary"}:
        return None

    summary = arguments.get("summary")
    if not isinstance(summary, str):
        return None
    summary = summary.strip()
    return summary or None


class EndSessionExecutor(BaseExecutor):
    requires_audit = False
    round_execution_policy = "exclusive"

    async def execute(self, summary: str) -> str:
        return json.dumps(
            {
                "status": "accepted",
                "tool_name": END_SESSION_TOOL_NAME,
            },
            ensure_ascii=False,
        )


__all__ = [
    "END_SESSION_TOOL_NAME",
    "END_SESSION_TOOL_SCHEMA",
    "extract_end_session_summary",
    "EndSessionExecutor",
]
