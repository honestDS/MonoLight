import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.message import message_crud
from app.core.crud.session.todo import session_todo_crud
from app.models.message import InternalMessage, MessageRole
from app.models.session_todo import SessionTodoPlan

__all__ = [
    "load_current_session_todo_snapshot",
    "persist_session_todo_snapshot_on_tool_results",
    "strip_session_todo_snapshot",
]

_SNAPSHOT_OPEN = "<current_session_todo_snapshot>"
_SNAPSHOT_CLOSE = "</current_session_todo_snapshot>"


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
    return f"{_SNAPSHOT_OPEN}{payload}{_SNAPSHOT_CLOSE}"


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


def strip_session_todo_snapshot(content: str | None) -> str | None:
    if not isinstance(content, str) or not content.endswith(_SNAPSHOT_CLOSE):
        return content

    marker_index = content.rfind(f"\n\n{_SNAPSHOT_OPEN}")
    if marker_index < 0:
        return content

    payload_start = marker_index + 2 + len(_SNAPSHOT_OPEN)
    payload_end = len(content) - len(_SNAPSHOT_CLOSE)
    try:
        payload = json.loads(content[payload_start:payload_end])
    except (TypeError, ValueError):
        return content

    if not isinstance(payload, dict) or not isinstance(payload.get("revision"), int) or not isinstance(payload.get("todos"), list):
        return content
    return content[:marker_index]


async def persist_session_todo_snapshot_on_tool_results(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    tool_results: list[InternalMessage],
) -> str | None:
    target = next(
        (message for message in reversed(tool_results) if message.role == MessageRole.TOOL and isinstance(message.id, int) and not isinstance(message.id, bool) and isinstance(message.content, str)),
        None,
    )
    if target is None:
        return None

    snapshot = await load_current_session_todo_snapshot(
        db,
        uid=uid,
        session_id=session_id,
    )
    await message_crud.update_model_context_suffix(
        db,
        message_id=target.id,
        uid=uid,
        session_id=session_id,
        model_context_suffix=snapshot,
        commit=False,
    )
    await db.commit()

    base_content = strip_session_todo_snapshot(target.content) or ""
    target.content = f"{base_content}\n\n{snapshot}" if snapshot else base_content
    return snapshot
