from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.constants import END_SESSION_TOOL_NAME

if TYPE_CHECKING:
    from app.models.message import InternalMessage

END_SESSION_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": END_SESSION_TOOL_NAME,
        "description": (
            "This control signal ends the current tool loop. By default, a separate final text response is generated; set skip_reply to true to complete normally with no final text. "
            'Use exactly one structured end_session call as the only tool call in the response, with arguments {}, {"skip_reply": false}, or {"skip_reply": true}. '
            "Use a normal response when an unanswered user question, necessary deliverable, or risk requires communication."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "skip_reply": {
                    "type": "boolean",
                    "default": False,
                },
            },
            "required": [],
            "additionalProperties": False,
        },
    },
}


def _get_end_session_arguments(message: Any) -> dict[str, Any] | None:
    tool_calls = message.get("tool_calls") if isinstance(message, dict) else getattr(message, "tool_calls", None)
    if not isinstance(tool_calls, (list, tuple)) or len(tool_calls) != 1:
        return None

    tool_call = tool_calls[0]
    name = tool_call.get("name") if isinstance(tool_call, dict) else getattr(tool_call, "name", None)
    if name != END_SESSION_TOOL_NAME:
        return None

    arguments = tool_call.get("arguments") if isinstance(tool_call, dict) else getattr(tool_call, "arguments", None)
    if not isinstance(arguments, dict):
        return None
    if not arguments:
        return arguments
    if set(arguments) != {"skip_reply"} or not isinstance(arguments["skip_reply"], bool):
        return None
    return arguments


def is_end_session_signal(message: Any) -> bool:
    """Return whether the message is exactly one valid end-session call."""
    return _get_end_session_arguments(message) is not None


def is_skip_reply_signal(message: Any) -> bool:
    """Return whether the message requests completion without a final text response."""
    arguments = _get_end_session_arguments(message)
    return arguments is not None and arguments.get("skip_reply") is True


async def persist_end_session_control(
    db: Any,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    message: InternalMessage,
    skip_reply: bool,
    received_task_ids: list[int],
    dedupe_scope: str,
) -> tuple[InternalMessage, InternalMessage]:
    import hashlib
    import json

    from app.core.utils.dispatcher.save_message import save_message
    from app.models.message import InternalMessage, MessageRole, MessageType

    call = message.tool_calls[0]
    received_result_task_ids = list(dict.fromkeys(task_id for task_id in received_task_ids if isinstance(task_id, int) and not isinstance(task_id, bool) and task_id > 0))
    provider_metadata = dict(message.provider_metadata or {})
    provider_metadata["internal_control"] = "end_session"

    call_message = message.model_copy(deep=True)
    call_message.content = None
    call_message.reasoning_content = None
    call_message.attachments = None
    call_message.generated_images = None
    call_message.provider_metadata = provider_metadata

    result_message = InternalMessage(
        role=MessageRole.TOOL,
        tool_call_id=call.id,
        tool_execution_status="succeeded",
        content=json.dumps(
            {
                "status": "completed",
                "skip_reply": skip_reply,
                "received_result_task_ids": received_result_task_ids,
            }
        ),
        provider_metadata=dict(provider_metadata),
    )

    call_saved = await save_message(
        db,
        session_id,
        uid,
        MessageRole.ASSISTANT,
        MessageType.TOOL_CALL,
        call_message,
        profile_id,
        is_processed=True,
        dedupe_key=hashlib.sha256(json.dumps([dedupe_scope, call.id, "call"]).encode()).hexdigest(),
        commit=False,
    )
    call_message.id = call_saved.id
    call_message.created_at = call_saved.created_at

    result_saved = await save_message(
        db,
        session_id,
        uid,
        MessageRole.TOOL,
        MessageType.TOOL_RESULT,
        result_message,
        profile_id,
        is_processed=True,
        dedupe_key=hashlib.sha256(json.dumps([dedupe_scope, call.id, "result"]).encode()).hexdigest(),
        commit=False,
    )
    result_message.id = result_saved.id
    result_message.created_at = result_saved.created_at

    return call_message, result_message


__all__ = [
    "END_SESSION_TOOL_NAME",
    "END_SESSION_TOOL_SCHEMA",
    "is_end_session_signal",
    "is_skip_reply_signal",
    "persist_end_session_control",
]
