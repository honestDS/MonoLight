import json
from typing import Any

from app.models.message import InternalMessage, MessageRole


def build_virtual_tool_feedback_messages(
    message: InternalMessage,
    payload: dict[str, Any],
) -> list[InternalMessage]:
    if not message.tool_calls:
        return []

    feedback_messages = [message]
    for tool_call in message.tool_calls:
        feedback_payload = {
            **payload,
            "tool_call": {
                "id": tool_call.id,
                "name": tool_call.name,
                "arguments": tool_call.arguments,
            },
        }
        feedback_messages.append(
            InternalMessage(
                role=MessageRole.TOOL,
                tool_call_id=tool_call.id,
                content=json.dumps(feedback_payload, ensure_ascii=False),
            )
        )
    return feedback_messages


__all__ = ["build_virtual_tool_feedback_messages"]
