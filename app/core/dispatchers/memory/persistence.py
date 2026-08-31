import json
from datetime import datetime
from hashlib import sha256
from typing import Any

from app.core.constants import (
    ERR_MEMORY_RECALL_ASSISTANT_DEDUPE_RECORD_INVALID,
    ERR_MEMORY_RECALL_TOOL_DEDUPE_RECORD_INVALID,
    ERR_MEMORY_RECALL_TOOL_DEDUPE_RECORD_MISMATCHED,
    ERR_MEMORY_RECALL_TOOL_DEDUPE_RECORD_ORPHAN,
)
from app.core.crud.session.message import message_crud
from app.core.i18n import t
from app.core.tools.longterm_memory import (
    MANAGE_LONGTERM_MEMORY_TOOL_NAME,
    validate_longterm_memory_arguments,
)
from app.core.utils.dispatcher.helpers import process_single_tool_with_isolated_db
from app.core.utils.dispatcher.save_assistant_message import save_assistant_message
from app.core.utils.dispatcher.save_tool_response import save_tool_response
from app.models.message import (
    InternalMessage,
    InternalToolCall,
    MessageRole,
    MessageType,
)

from .types import (
    MemoryRecallContext,
    get_profile_id,
)


def build_dedupe_key(prefix: str, context: MemoryRecallContext) -> str:
    payload = "\x1f".join(
        (
            context.uid,
            context.session_id,
            str(get_profile_id(context)),
            str(context.current_user_boundary_message_id),
        )
    )
    digest = sha256(payload.encode("utf-8")).hexdigest()[:32]
    return f"memory-recall-{prefix}:{digest}"


def _enum_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _row_matches(
    row: Any,
    context: MemoryRecallContext,
    role: MessageRole,
    message_type: MessageType,
) -> bool:
    profile_id = get_profile_id(context)
    return bool(
        row is not None
        and getattr(row, "uid", None) == context.uid
        and getattr(row, "session_id", None) == context.session_id
        and profile_id is not None
        and getattr(row, "profile_id", None) == profile_id
        and _enum_value(getattr(row, "role", None)) == role.value
        and _enum_value(getattr(row, "type", None)) == message_type.value
        and isinstance(getattr(row, "id", None), int)
        and getattr(row, "id", 0) > 0
    )


def _row_to_internal(row: Any) -> InternalMessage | None:
    content = getattr(row, "content", None)
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
        if not isinstance(payload, dict):
            return None
        payload["id"] = row.id
        created_at = getattr(row, "created_at", None)
        if isinstance(created_at, datetime):
            payload["created_at"] = created_at.timestamp()
        elif isinstance(created_at, (int, float)) and not isinstance(created_at, bool):
            payload["created_at"] = created_at
        return InternalMessage.model_validate(payload)
    except (TypeError, ValueError):
        return None


def _has_content(value: Any) -> bool:
    if isinstance(value, str):
        return bool(value.strip())
    return bool(value)


def is_valid_recall_call(message: InternalMessage | None) -> bool:
    if message is None or message.role != MessageRole.ASSISTANT:
        return False
    if _has_content(message.content) or _has_content(message.refusal):
        return False
    if len(message.tool_calls or []) != 1:
        return False
    tool_call = message.tool_calls[0]
    operation, error = validate_longterm_memory_arguments(tool_call.arguments)
    return tool_call.name == MANAGE_LONGTERM_MEMORY_TOOL_NAME and operation == "recall" and error is None


def _tool_result_matches(
    message: InternalMessage | None,
    tool_call: InternalToolCall,
) -> bool:
    return bool(message is not None and message.role == MessageRole.TOOL and message.tool_call_id == tool_call.id and isinstance(message.content, str))


def append_once(messages: list[InternalMessage], message: InternalMessage) -> None:
    if isinstance(message.id, int) and any(item.id == message.id for item in messages):
        return
    messages.append(message.model_copy(deep=True))


async def load_dedupe_messages(
    context: MemoryRecallContext,
) -> tuple[InternalMessage | None, InternalMessage | None, str, str]:
    assistant_key = build_dedupe_key("assistant", context)
    tool_key = build_dedupe_key("tool", context)
    assistant_row = await message_crud.get_by_dedupe_key(context.db, assistant_key)
    tool_row = await message_crud.get_by_dedupe_key(context.db, tool_key)

    assistant_message = None
    if assistant_row is not None:
        if not _row_matches(assistant_row, context, MessageRole.ASSISTANT, MessageType.TOOL_CALL):
            raise ValueError(t(ERR_MEMORY_RECALL_ASSISTANT_DEDUPE_RECORD_INVALID))
        assistant_message = _row_to_internal(assistant_row)
        if not is_valid_recall_call(assistant_message):
            raise ValueError(t(ERR_MEMORY_RECALL_ASSISTANT_DEDUPE_RECORD_INVALID))

    tool_message = None
    if tool_row is not None:
        if not _row_matches(tool_row, context, MessageRole.TOOL, MessageType.TOOL_RESULT):
            raise ValueError(t(ERR_MEMORY_RECALL_TOOL_DEDUPE_RECORD_INVALID))
        tool_message = _row_to_internal(tool_row)
        if tool_message is None:
            raise ValueError(t(ERR_MEMORY_RECALL_TOOL_DEDUPE_RECORD_INVALID))

    if tool_message is not None and assistant_message is None:
        raise ValueError(t(ERR_MEMORY_RECALL_TOOL_DEDUPE_RECORD_ORPHAN))
    if assistant_message is not None and tool_message is not None:
        if not _tool_result_matches(tool_message, assistant_message.tool_calls[0]):
            raise ValueError(t(ERR_MEMORY_RECALL_TOOL_DEDUPE_RECORD_MISMATCHED))
    return assistant_message, tool_message, assistant_key, tool_key


async def _emit_recall_event(
    context: MemoryRecallContext,
    tool_call: InternalToolCall,
    assistant_message: InternalMessage,
    response_id: str,
    tool_result: InternalMessage | None = None,
) -> None:
    callback = context.stream_event_callback
    if callback is None or not context.show_tool_calls:
        return
    if tool_result is None:
        await callback({"type": "agent_loop_start", "turn": 0, "response_id": response_id})
        turn_end: dict[str, Any] = {"type": "turn_end", "response_id": response_id}
        if assistant_message.id is not None:
            turn_end["message_id"] = assistant_message.id
        await callback(turn_end)
        await callback(
            {
                "type": "tool_start",
                "name": tool_call.name,
                "arguments": tool_call.arguments,
                "tool_call_id": tool_call.id,
                "response_id": response_id,
                "tool_call_index": 0,
                "tool_call_count": 1,
            }
        )
        return
    await callback(
        {
            "type": "tool_end",
            "name": tool_call.name,
            "result": tool_result.content,
            "tool_call_id": tool_call.id,
            "response_id": response_id,
        }
    )


async def save_and_execute_recall(
    context: MemoryRecallContext,
    assistant_message: InternalMessage,
    *,
    assistant_key: str,
    tool_key: str,
    response_id: str,
    assistant_already_saved: bool = False,
) -> None:
    tool_call = assistant_message.tool_calls[0]
    if not assistant_already_saved:
        await save_assistant_message(
            context.db,
            context.session_id,
            context.uid,
            get_profile_id(context),
            assistant_message,
            dedupe_key=assistant_key,
        )
    append_once(context.messages, assistant_message)
    append_once(context.turn_messages, assistant_message)
    await _emit_recall_event(context, tool_call, assistant_message, response_id)
    await context.db.commit()
    tool_result = await process_single_tool_with_isolated_db(
        tool_call,
        context.profile,
        context.cfg,
        context.messages,
        context.username,
        context.session_id,
        0,
        context.uid,
        allowed_knowledge_base_ids=context.allowed_knowledge_base_ids,
        context_window_k=context.chat_params["context_window_k"],
        context_summary_boundary_message_id=context.upper_message_id,
        source_message_id=context.current_user_boundary_message_id,
    )
    stored_tool_result = await save_tool_response(
        context.db,
        context.session_id,
        context.uid,
        get_profile_id(context),
        tool_result,
        context.messages,
        context.turn_messages,
        dedupe_key=tool_key,
    )
    await _emit_recall_event(context, tool_call, assistant_message, response_id, stored_tool_result)


__all__ = [
    "append_once",
    "build_dedupe_key",
    "is_valid_recall_call",
    "load_dedupe_messages",
    "save_and_execute_recall",
]
