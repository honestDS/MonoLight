import copy
import json

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.message import message_crud
from app.core.crud.session.todo import session_todo_crud
from app.core.utils.dispatcher import terminal_watch_snapshot
from app.core.utils.tokenizer import estimate_tokens
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.session_todo import SessionTodoPlan

__all__ = [
    "append_session_todo_snapshot",
    "load_current_session_todo_snapshot",
    "measure_session_todo_snapshot_tokens",
    "parse_session_todo_snapshot",
    "persist_session_todo_snapshot_on_tool_results",
    "strip_session_todo_snapshots",
]


def parse_session_todo_snapshot(snapshot: object) -> dict[str, object] | None:
    if not isinstance(snapshot, dict):
        return None
    revision = snapshot.get("revision")
    todos = snapshot.get("todos")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision <= 0 or not isinstance(todos, list):
        return None
    normalized_todos: list[dict[str, str]] = []
    for todo in todos:
        if not isinstance(todo, dict):
            return None
        content = todo.get("content")
        status = todo.get("status")
        if not isinstance(content, str) or not isinstance(status, str):
            return None
        normalized_todos.append({"content": content, "status": status})
    return {"revision": revision, "todos": normalized_todos}


def _build_todo_snapshot(plan: SessionTodoPlan) -> dict[str, object] | None:
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

    return {
        "revision": revision,
        "todos": snapshot_todos,
    }


async def load_current_session_todo_snapshot(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
) -> dict[str, object] | None:
    session_exists, plan = await session_todo_crud.read(
        db,
        uid=uid,
        session_id=session_id,
    )
    if not session_exists or plan is None:
        return None
    return _build_todo_snapshot(plan)


def strip_session_todo_snapshots(messages: list[InternalMessage]) -> list[InternalMessage]:
    stripped: list[InternalMessage] = []
    for message in messages:
        stripped_message = message.model_copy(deep=True)
        stripped_message.current_session_todo = None
        stripped_message.current_terminal_watch = None
        stripped.append(stripped_message)
    return stripped


def append_session_todo_snapshot(
    messages: list[InternalMessage],
    snapshot: dict[str, object] | None,
) -> list[InternalMessage]:
    updated = [message.model_copy(deep=True) for message in messages]
    for message in updated:
        message.current_session_todo = None
    if snapshot is None:
        return updated

    target = None
    for message in reversed(updated):
        if message.role != MessageRole.TOOL:
            continue
        target = message
        break
    if target is not None:
        target.current_session_todo = copy.deepcopy(snapshot)
    else:
        for message in updated:
            if message.conversation_summary is not None:
                target = message
                break
        if target is None:
            return updated

        target.current_session_todo = copy.deepcopy(snapshot)
    return updated


def measure_session_todo_snapshot_tokens(snapshot: dict[str, object] | None) -> int:
    if snapshot is None:
        return 0
    serialized = json.dumps(
        {"current_session_todo": snapshot},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return estimate_tokens(serialized)


async def persist_session_todo_snapshot_on_tool_results(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    tool_results: list[InternalMessage],
    commit: bool = True,
) -> dict[str, object] | None:
    target = None
    for message in reversed(tool_results):
        if message.role != MessageRole.TOOL:
            continue
        if not isinstance(message.id, int):
            continue
        if isinstance(message.id, bool):
            continue
        if not isinstance(message.content, str):
            continue
        target = message
        break
    if target is None:
        return None

    stored = await message_crud.get(db, target.id)
    if stored is None:
        return None
    await db.refresh(stored)
    if stored.uid != uid or stored.session_id != session_id or stored.role != MessageRole.TOOL or stored.type != MessageType.TOOL_RESULT:
        return None

    if stored.model_context_suffix is None:
        todo_snapshot = None
        if stored.work_id is None:
            todo_snapshot = await load_current_session_todo_snapshot(
                db,
                uid=uid,
                session_id=session_id,
            )
        watch_snapshot = await terminal_watch_snapshot.load_current_terminal_watch_snapshot(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=stored.profile_id,
            message_work_id=stored.work_id,
        )
        snapshot_payload: dict[str, object] = {}
        if todo_snapshot is not None:
            snapshot_payload["current_session_todo"] = todo_snapshot
        if watch_snapshot is not None:
            snapshot_payload["current_terminal_watch"] = watch_snapshot
        suffix = json.dumps(snapshot_payload, ensure_ascii=False, separators=(",", ":"))
        await message_crud.update_model_context_suffix(
            db,
            message_id=stored.id,
            uid=uid,
            session_id=session_id,
            model_context_suffix=suffix,
            only_if_unset=True,
            commit=False,
        )
        if commit:
            await db.commit()
        await db.refresh(stored)

    from app.core.utils.message_parser import parse_db_messages_to_internal

    parsed_messages = parse_db_messages_to_internal([stored])
    if not parsed_messages:
        return None
    parsed_message = parsed_messages[0]
    target.content = parsed_message.content
    target.current_session_todo = copy.deepcopy(parsed_message.current_session_todo)
    target.current_terminal_watch = copy.deepcopy(parsed_message.current_terminal_watch)
    return target.current_session_todo
