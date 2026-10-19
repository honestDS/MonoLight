import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.constants import (
    ERR_SCHEDULED_TASK_ARGUMENTS_INVALID,
    ERR_SCHEDULED_TASK_NOT_FOUND,
    ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND,
    ERR_SESSION_NOT_FOUND,
    ERR_TOOL_RUNTIME_CONTEXT_MISSING,
    MANAGE_SCHEDULED_TASKS_TOOL_NAME,
    MSG_SCHEDULED_TASK_CREATED,
    MSG_SCHEDULED_TASK_DELETED,
    MSG_SCHEDULED_TASK_LIST_SUCCESS,
)
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.i18n import t
from app.core.tools.base import BaseExecutor
from app.models.scheduled_task import ScheduledTaskResponse

MANAGE_SCHEDULED_TASKS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": MANAGE_SCHEDULED_TASKS_TOOL_NAME,
        "description": (
            "Create repeating interval tasks by default in the current user's current session. Set run_once=true for a single delayed task. "
            "The first trigger waits for interval_seconds in either mode; repeating tasks then trigger at that interval. "
            "After a single task is successfully enqueued, it is automatically disabled so it triggers only once; enqueue does not mean the LLM has completed execution. "
            "Absolute times and cron schedules are not supported. "
            "List all scheduled tasks, including disabled tasks, or delete a task only within the current user's current session. "
            "Deleting a task prevents future triggers but does not cancel already queued replies. "
            "Ownership, session, and the current runtime profile come from runtime context and cannot be specified through arguments."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["create", "list", "delete"],
                    "description": "Operation to perform: create, list, or delete.",
                },
                "name": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                    "description": "Required only when operation=create.",
                },
                "message": {
                    "type": "string",
                    "minLength": 1,
                    "description": "Required only when operation=create; this request is sent to the LLM each time the task triggers.",
                },
                "interval_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 2147483647,
                    "description": "Required only when operation=create; the initial wait before the first trigger, and the repeating interval unless run_once=true.",
                },
                "run_once": {
                    "type": "boolean",
                    "default": False,
                    "description": "Used only when operation=create; defaults to false for repeating tasks. Set true for a single delayed task.",
                },
                "page": {
                    "type": "integer",
                    "minimum": 1,
                    "default": 1,
                    "description": "Used only when operation=list.",
                },
                "size": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 100,
                    "default": 20,
                    "description": "Used only when operation=list.",
                },
                "task_id": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "Required only when operation=delete; use the task ID returned by create or list.",
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}


class _CreateArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1)
    interval_seconds: int = Field(ge=60, le=2147483647)
    run_once: bool = False


class _ListArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


class _DeleteArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    task_id: int = Field(ge=1)


def _failure(operation: str, error: str, details: list[dict[str, Any]] | None = None) -> str:
    result: dict[str, Any] = {
        "status": "failed",
        "operation": operation,
        "error": t(error),
    }
    if details is not None:
        result["details"] = details
    return json.dumps(result, ensure_ascii=False)


class ManageScheduledTasksExecutor(BaseExecutor):
    requires_audit = True

    async def execute(self, operation: str, **arguments: Any) -> str:
        if operation == "create":
            arguments_model = _CreateArguments
        elif operation == "list":
            arguments_model = _ListArguments
        elif operation == "delete":
            arguments_model = _DeleteArguments
        else:
            return _failure(operation, ERR_SCHEDULED_TASK_ARGUMENTS_INVALID)

        try:
            params = arguments_model.model_validate(arguments)
        except ValidationError as exc:
            return _failure(
                operation,
                ERR_SCHEDULED_TASK_ARGUMENTS_INVALID,
                exc.errors(include_url=False, include_context=False, include_input=False),
            )

        context = self.dispatch_context
        uid = context.uid if context is not None else self.uid
        session_id = context.session_id if context is not None else self.session_id
        profile = context.profile if context is not None else self.profile
        db = context.db if context is not None else self.db

        if not isinstance(uid, str) or not uid.strip() or not isinstance(session_id, str) or not session_id.strip() or db is None:
            return _failure(operation, ERR_TOOL_RUNTIME_CONTEXT_MISSING)

        session = await session_crud.get_by_session_id(db, session_id)
        if session is None or session.uid != uid:
            return _failure(operation, ERR_SESSION_NOT_FOUND)

        if operation == "list":
            tasks = await scheduled_task_crud.list_tasks(
                db,
                uid=uid,
                session_id=session_id,
                skip=(params.page - 1) * params.size,
                limit=params.size,
            )
            total = await scheduled_task_crud.count_tasks(db, uid=uid, session_id=session_id)
            return json.dumps(
                {
                    "status": "success",
                    "operation": operation,
                    "message": t(MSG_SCHEDULED_TASK_LIST_SUCCESS),
                    "page": params.page,
                    "size": params.size,
                    "total": total,
                    "tasks": [ScheduledTaskResponse.model_validate(task).model_dump(mode="json") for task in tasks],
                },
                ensure_ascii=False,
            )

        if operation == "delete":
            try:
                deleted = await scheduled_task_crud.delete_user_task(
                    db,
                    task_id=params.task_id,
                    uid=uid,
                    session_id=session_id,
                )
            except Exception:
                await db.rollback()
                raise
            if not deleted:
                return _failure(operation, ERR_SCHEDULED_TASK_NOT_FOUND)
            return json.dumps(
                {
                    "status": "success",
                    "operation": operation,
                    "message": t(MSG_SCHEDULED_TASK_DELETED),
                    "task_id": params.task_id,
                },
                ensure_ascii=False,
            )

        profile_id = getattr(profile, "id", None)
        if isinstance(profile_id, bool) or not isinstance(profile_id, int) or profile_id <= 0 or getattr(profile, "uid", None) != uid:
            return _failure(operation, ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND)

        try:
            locked_profile = await profile_crud.lock_for_runtime_use(db, profile_id=profile_id, uid=uid)
        except Exception:
            await db.rollback()
            raise
        if locked_profile is None or locked_profile.id != profile_id or locked_profile.uid != uid:
            await db.rollback()
            return _failure(operation, ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND)

        try:
            task = await scheduled_task_crud.create_scheduled_task(
                db,
                name=params.name,
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                message=params.message,
                interval_seconds=params.interval_seconds,
                run_once=params.run_once,
            )
        except Exception:
            await db.rollback()
            raise

        return json.dumps(
            {
                "status": "success",
                "operation": operation,
                "message": t(MSG_SCHEDULED_TASK_CREATED),
                "task": ScheduledTaskResponse.model_validate(task).model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
