import inspect
import json
from typing import Any

from app.core.constants import (
    ERR_TOOL_TODO_CONTENT_INVALID,
    ERR_TOOL_TODO_CONTENT_TOO_LONG,
    ERR_TOOL_TODO_DUPLICATE_CONTENT,
    ERR_TOOL_TODO_ITEM_INVALID,
    ERR_TOOL_TODO_MULTIPLE_IN_PROGRESS,
    ERR_TOOL_TODO_OPERATION_INVALID,
    ERR_TOOL_TODO_READ_TODOS_FORBIDDEN,
    ERR_TOOL_TODO_STATUS_INVALID,
    ERR_TOOL_TODO_TODOS_INVALID,
    ERR_TOOL_TODO_TODOS_REQUIRED,
    ERR_TOOL_TODO_TOO_MANY_ITEMS,
    ERR_TOOL_TODO_UNAVAILABLE,
    ERR_TOOL_UNSUPPORTED_ARGUMENTS,
    MANAGE_TODO_TOOL_NAME,
    SESSION_TODO_ALLOWED_STATUSES,
    SESSION_TODO_MAX_CONTENT_CHARS,
    SESSION_TODO_MAX_ITEMS,
)
from app.core.crud.session.todo import session_todo_crud
from app.core.i18n import t
from app.core.tools.base import BaseExecutor

_TODO_STATUSES = ("pending", "in_progress", "completed")
_TODO_ARGUMENTS = frozenset({"operation", "todos"})
_MISSING = object()


MANAGE_TODO_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": MANAGE_TODO_TOOL_NAME,
        "description": "Read or replace the complete Todo plan for the current session.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["read", "write"],
                    "description": "The operation to perform.",
                },
                "todos": {
                    "type": "array",
                    "maxItems": SESSION_TODO_MAX_ITEMS,
                    "description": "The complete Todo list required for write.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {
                                "type": "string",
                                "minLength": 1,
                                "maxLength": SESSION_TODO_MAX_CONTENT_CHARS,
                            },
                            "status": {
                                "type": "string",
                                "enum": _TODO_STATUSES,
                            },
                        },
                        "required": ["content", "status"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}


def _success(operation: str, revision: int, todos: list[dict[str, str]]) -> str:
    return json.dumps(
        {
            "status": "success",
            "operation": operation,
            "revision": revision,
            "todos": todos,
        },
        ensure_ascii=False,
    )


def _failure(operation: str | None, error: str) -> str:
    payload: dict[str, Any] = {"status": "failed"}
    if operation is not None:
        payload["operation"] = operation
    payload["error"] = error
    return json.dumps(payload, ensure_ascii=False)


def validate_manage_todo_arguments(
    arguments: dict[str, Any],
) -> tuple[str | None, list[dict[str, str]] | None, str | None]:
    if not isinstance(arguments, dict):
        return None, None, t(ERR_TOOL_TODO_OPERATION_INVALID)

    raw_operation = arguments.get("operation")
    operation = raw_operation if isinstance(raw_operation, str) else None

    unsupported = sorted(
        (field for field in arguments if field not in _TODO_ARGUMENTS),
        key=str,
    )
    if unsupported:
        return (
            operation,
            None,
            t(
                ERR_TOOL_UNSUPPORTED_ARGUMENTS,
                tool_name=MANAGE_TODO_TOOL_NAME,
                fields=", ".join(str(field) for field in unsupported),
            ),
        )

    if not isinstance(raw_operation, str) or raw_operation not in {"read", "write"}:
        return operation, None, t(ERR_TOOL_TODO_OPERATION_INVALID)

    if raw_operation == "read":
        if "todos" in arguments:
            return operation, None, t(ERR_TOOL_TODO_READ_TODOS_FORBIDDEN)
        return operation, None, None

    if "todos" not in arguments:
        return operation, None, t(ERR_TOOL_TODO_TODOS_REQUIRED)

    todos = arguments["todos"]
    if not isinstance(todos, list):
        return operation, None, t(ERR_TOOL_TODO_TODOS_INVALID)
    if len(todos) > SESSION_TODO_MAX_ITEMS:
        return operation, None, t(ERR_TOOL_TODO_TOO_MANY_ITEMS, maximum=SESSION_TODO_MAX_ITEMS)

    normalized_todos: list[dict[str, str]] = []
    seen_contents: set[str] = set()
    in_progress_count = 0
    for item_index, item in enumerate(todos):
        if not isinstance(item, dict) or set(item) != {"content", "status"}:
            return operation, None, t(ERR_TOOL_TODO_ITEM_INVALID, index=item_index)

        content = item["content"]
        if not isinstance(content, str):
            return operation, None, t(ERR_TOOL_TODO_CONTENT_INVALID, index=item_index)
        content = content.strip()
        if not content:
            return operation, None, t(ERR_TOOL_TODO_CONTENT_INVALID, index=item_index)
        if len(content) > SESSION_TODO_MAX_CONTENT_CHARS:
            return (
                operation,
                None,
                t(
                    ERR_TOOL_TODO_CONTENT_TOO_LONG,
                    index=item_index,
                    maximum=SESSION_TODO_MAX_CONTENT_CHARS,
                ),
            )
        if content in seen_contents:
            return operation, None, t(ERR_TOOL_TODO_DUPLICATE_CONTENT, index=item_index)
        seen_contents.add(content)

        status = item["status"]
        if not isinstance(status, str) or status not in SESSION_TODO_ALLOWED_STATUSES:
            return operation, None, t(ERR_TOOL_TODO_STATUS_INVALID, index=item_index)
        if status == "in_progress":
            in_progress_count += 1
            if in_progress_count > 1:
                return operation, None, t(ERR_TOOL_TODO_MULTIPLE_IN_PROGRESS)

        normalized_todos.append({"content": content, "status": status})

    return operation, normalized_todos, None


def _plan_value(plan: Any, field: str) -> Any:
    if isinstance(plan, dict):
        return plan.get(field, _MISSING)
    return getattr(plan, field, _MISSING)


def _normalized_plan(plan: Any) -> tuple[int, list[dict[str, str]]] | None:
    revision = _plan_value(plan, "revision")
    todos = _plan_value(plan, "todos")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        return None
    _, normalized_todos, error = validate_manage_todo_arguments({"operation": "write", "todos": todos})
    if error is not None or normalized_todos is None:
        return None
    return revision, normalized_todos


async def _rollback_safely(db: Any) -> None:
    try:
        rollback = getattr(db, "rollback", None)
        if not callable(rollback):
            return
        result = rollback()
        if inspect.isawaitable(result):
            await result
    except Exception:
        pass


class ManageTodoExecutor(BaseExecutor):
    requires_audit = False

    def _runtime_identity(self) -> tuple[Any, Any]:
        dispatch_context = self.dispatch_context
        if dispatch_context is not None:
            return (
                getattr(dispatch_context, "uid", None),
                getattr(dispatch_context, "session_id", None),
            )
        return self.uid, self.session_id

    async def execute(self, operation: str, todos: Any = _MISSING) -> str:
        arguments: dict[str, Any] = {"operation": operation}
        if todos is not _MISSING:
            arguments["todos"] = todos

        normalized_operation, normalized_todos, validation_error = validate_manage_todo_arguments(arguments)
        if validation_error:
            return _failure(normalized_operation, validation_error)

        dispatch_context = self.dispatch_context
        if self.db is None and dispatch_context is not None:
            context_db = getattr(dispatch_context, "db", None)
            if context_db is not None:
                self.db = context_db

        runtime_uid, runtime_session_id = self._runtime_identity()
        if self.db is None or not isinstance(runtime_uid, str) or not runtime_uid.strip() or not isinstance(runtime_session_id, str) or not runtime_session_id.strip():
            return _failure(normalized_operation, t(ERR_TOOL_TODO_UNAVAILABLE))

        try:
            if normalized_operation == "read":
                read_result = await session_todo_crud.read(
                    self.db,
                    uid=runtime_uid,
                    session_id=runtime_session_id,
                )
                if not isinstance(read_result, tuple) or len(read_result) != 2:
                    await _rollback_safely(self.db)
                    return _failure(normalized_operation, t(ERR_TOOL_TODO_UNAVAILABLE))
                session_exists, plan = read_result
                if session_exists is not True:
                    await _rollback_safely(self.db)
                    return _failure(normalized_operation, t(ERR_TOOL_TODO_UNAVAILABLE))
                if plan is None:
                    return _success(normalized_operation, 0, [])
                normalized_plan = _normalized_plan(plan)
                if normalized_plan is None:
                    await _rollback_safely(self.db)
                    return _failure(normalized_operation, t(ERR_TOOL_TODO_UNAVAILABLE))
                revision, stored_todos = normalized_plan
                return _success(normalized_operation, revision, stored_todos)

            plan = await session_todo_crud.write(
                self.db,
                uid=runtime_uid,
                session_id=runtime_session_id,
                todos=normalized_todos,
            )
            if plan is None:
                await _rollback_safely(self.db)
                return _failure(normalized_operation, t(ERR_TOOL_TODO_UNAVAILABLE))
            normalized_plan = _normalized_plan(plan)
            if normalized_plan is None:
                await _rollback_safely(self.db)
                return _failure(normalized_operation, t(ERR_TOOL_TODO_UNAVAILABLE))
            revision, stored_todos = normalized_plan
            return _success(normalized_operation, revision, stored_todos)
        except Exception:
            await _rollback_safely(self.db)
            return _failure(normalized_operation, t(ERR_TOOL_TODO_UNAVAILABLE))


__all__ = [
    "MANAGE_TODO_TOOL_NAME",
    "MANAGE_TODO_TOOL_SCHEMA",
    "ManageTodoExecutor",
    "validate_manage_todo_arguments",
]
