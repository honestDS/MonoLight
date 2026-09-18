import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.todo import session_todo_crud
from app.core.utils.context_messages import message_token_text
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole
from app.models.session_todo import SessionTodoPlan

__all__ = [
    "append_session_todo_snapshot",
    "has_session_todo_snapshot_target",
    "load_current_session_todo_snapshot",
    "measure_session_todo_snapshot_tokens",
]


def _serialize_todo_snapshot(plan: SessionTodoPlan) -> str | None:
    revision = plan.revision
    todos = plan.todos
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0 or not isinstance(todos, list):
        return None

    snapshot_todos: list[dict[str, str]] = []
    for todo in todos:
        if not isinstance(todo, dict):
            return None
        content = todo.get("content")
        status = todo.get("status")
        if not isinstance(content, str) or not isinstance(status, str):
            return None
        snapshot_todos.append({"content": content, "status": status})

    payload = json.dumps(
        {
            "revision": revision,
            "todos": snapshot_todos,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    payload = payload.replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return f"<current_session_todo_snapshot>{payload}</current_session_todo_snapshot>"


async def load_current_session_todo_snapshot(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
) -> str | None:
    session_exists, plan = await session_todo_crud.read(
        db,
        uid=uid,
        session_id=session_id,
    )
    if not session_exists or plan is None:
        return None
    return _serialize_todo_snapshot(plan)


def _snapshot_target_indices(messages: list[InternalMessage]) -> list[int]:
    assistant_index = next(
        (index for index in range(len(messages) - 1, -1, -1) if messages[index].role == MessageRole.ASSISTANT and isinstance(messages[index].tool_calls, list) and messages[index].tool_calls),
        None,
    )
    if assistant_index is not None:
        tool_call_ids = {tool_call.id for tool_call in messages[assistant_index].tool_calls or [] if isinstance(getattr(tool_call, "id", None), str) and tool_call.id}
        tool_indices = [index for index in range(assistant_index + 1, len(messages)) if messages[index].role == MessageRole.TOOL and messages[index].tool_call_id in tool_call_ids]
        if tool_indices:
            return [tool_indices[-1]]

    return []


def has_session_todo_snapshot_target(messages: list[InternalMessage]) -> bool:
    return bool(_snapshot_target_indices(messages))


def append_session_todo_snapshot(
    messages: list[InternalMessage],
    snapshot: str | None,
) -> list[InternalMessage]:
    updated_messages = list(messages)
    if not isinstance(snapshot, str) or not snapshot:
        return updated_messages

    for index in _snapshot_target_indices(messages):
        message = messages[index]
        if not isinstance(message.content, str) or snapshot in message.content:
            continue
        updated_message = message.model_copy(deep=True)
        updated_message.content = f"{message.content}\n\n{snapshot}"
        updated_messages[index] = updated_message
    return updated_messages


def measure_session_todo_snapshot_tokens(
    messages: list[InternalMessage],
    snapshot: str | None,
) -> int:
    if not isinstance(snapshot, str) or not snapshot:
        return 0

    updated_messages = append_session_todo_snapshot(messages, snapshot)
    additional_tokens = 0
    for index in _snapshot_target_indices(messages):
        before = messages[index]
        after = updated_messages[index]
        additional_tokens += max(
            0,
            estimate_tokens(message_token_text(after)) - estimate_tokens(message_token_text(before)),
        )
    return additional_tokens
