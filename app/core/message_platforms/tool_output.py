import json
from typing import Any

from app.core.constants import MSG_MESSAGE_PLATFORM_TOOL_USED
from app.core.i18n import t


def _tool_call_name(tool_call: dict[str, Any]) -> str:
    function = tool_call.get("function")
    function = function if isinstance(function, dict) else {}
    name = tool_call.get("name") or function.get("name") or "-"
    return str(name).strip() or "-"


def _extract_final_text(content: Any) -> str:
    if not isinstance(content, str):
        return str(content or "")
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return content
    if not isinstance(payload, dict):
        return content
    if payload.get("type") == "audit_confirmation":
        plain_text = payload.get("plain_text")
        return plain_text if isinstance(plain_text, str) else content
    if payload.get("type") == "assistant_files":
        text = payload.get("text")
        return text if isinstance(text, str) else content
    return content


def combine_proactive_reply_tool_output(event: dict[str, Any]) -> dict[str, Any]:
    history = event.get("history")
    if not isinstance(history, list):
        return event

    parts: list[str] = []
    has_tool_call = False
    for item in history:
        if not isinstance(item, dict):
            continue
        tool_calls = item.get("tool_calls")
        if item.get("role") != "assistant" or not isinstance(tool_calls, list) or not tool_calls:
            continue
        round_parts: list[str] = []
        content = item.get("content")
        if isinstance(content, str) and content.strip():
            round_parts.append(content.strip())
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            has_tool_call = True
            round_parts.append(t(MSG_MESSAGE_PLATFORM_TOOL_USED, name=_tool_call_name(tool_call)))
        if round_parts:
            parts.append("\n".join(round_parts))

    if not has_tool_call:
        return event

    final_text = _extract_final_text(event.get("content"))
    if final_text:
        parts.append(final_text)
    combined_event = {key: value for key, value in event.items() if key != "history"}
    combined_event["content"] = "\n\n".join(parts)
    return combined_event
