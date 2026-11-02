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
from app.core.exceptions import ParameterException
from app.core.i18n import t
from app.core.tools.base import BaseExecutor
from app.core.utils.scheduled_time import get_next_run_times
from app.core.utils.time import get_local_time
from app.models.scheduled_task import ScheduledTaskResponse
from app.schemas.scheduled_task import ScheduledTaskSchedule

MANAGE_SCHEDULED_TASKS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": MANAGE_SCHEDULED_TASKS_TOOL_NAME,
        "description": (
            "Create repeating delay or calendar tasks by default in the current user's current session. For a relative delay, provide interval_seconds in seconds; set run_once=true for a single delayed task. "
            "For a recurring calendar schedule, provide a five-field cron_expression (minute hour day month weekday) and an explicit IANA timezone; cron schedules require run_once=false. "
            "The first trigger waits for interval_seconds in delay mode; repeating delay tasks then trigger at that interval. "
            "After a single task is successfully enqueued, it is automatically disabled so it triggers only once; enqueue does not mean the LLM has completed execution. "
            "Absolute one-time timestamps are not supported; use interval_seconds for a relative delay or cron_expression for a recurring calendar schedule. "
            "Never use the server's default timezone. Determine an IANA timezone from the user's explicit time, location, or known preference; language is only a hint, so ask the user before creating when it is not reliable. "
            "Ask the user for a concrete execution time or delay/schedule before creating when none is provided; do not create an ambiguous schedule. "
            "After successful creation, explain in plain language the period, timezone, and future execution times returned by the tool. Calendar schedules skip months that do not contain the requested day. "
            "Cron day-of-month and weekday restrictions are combined as OR, not AND, so do not use both to request an AND condition. Holiday and lunar-calendar rules are not inferred; ask the user to clarify any ambiguous request. "
            "List all scheduled tasks, including disabled tasks, or delete a task only within the current user's current session. "
            "Deleting a task prevents future triggers but does not cancel already queued replies. "
            "Triggered tasks execute as independent AgentWork and do not receive conversation history, so create messages must be self-contained. "
            "Ownership and session come from runtime context and cannot be specified through arguments. Each trigger uses the session's current profile, which remains fixed for that execution."
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
                    "description": (
                        "Required only when operation=create. This must be a self-contained instruction that can be executed later without conversation history. "
                        "Resolve references such as 'that file' or 'the report above' at creation time into stable paths, IDs, or explicit details. "
                        "This exact request is sent to an independent AgentWork each time the task triggers."
                    ),
                },
                "interval_seconds": {
                    "type": "integer",
                    "minimum": 60,
                    "maximum": 2147483647,
                    "description": "Used only when operation=create. Provide exactly one of interval_seconds or cron_expression. This is a relative delay in seconds before the first trigger and, unless run_once=true, between repeating triggers; do not provide a timezone for delay mode.",
                },
                "cron_expression": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 255,
                    "description": "Used for create. Deterministic five-field Cron: minute hour day-of-month month weekday. Supports L for month-end and # for the nth weekday; random schedules are not supported. Requires timezone and run_once=false.",
                },
                "timezone": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 100,
                    "description": (
                        "Used only with cron_expression when operation=create. Required explicit IANA timezone, such as Asia/Shanghai or America/New_York; never infer or use the server default. "
                        "Use the user's explicit time, location, or known preference; language is only a hint, so ask before creating if the timezone is uncertain."
                    ),
                },
                "run_once": {
                    "type": "boolean",
                    "default": False,
                    "description": "Used only when operation=create; defaults to false. Set true only for a single interval_seconds delay; cron_expression requires false.",
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


class _CreateArguments(ScheduledTaskSchedule):
    model_config = ConfigDict(extra="forbid", strict=True, str_strip_whitespace=True)

    name: str = Field(min_length=1, max_length=100)
    message: str = Field(min_length=1)
    interval_seconds: int | None = Field(default=None, ge=60, le=2147483647)


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
        except ParameterException as exc:
            return _failure(operation, exc.message)
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
                message=params.message,
                interval_seconds=params.interval_seconds,
                cron_expression=params.cron_expression,
                timezone=params.timezone,
                run_once=params.run_once,
            )
        except Exception:
            await db.rollback()
            raise

        first_next_run = task.next_run_at
        if first_next_run.tzinfo is None:
            first_next_run = first_next_run.replace(tzinfo=get_local_time().tzinfo)
        next_run_times = [first_next_run]
        if not task.run_once:
            next_run_times.extend(
                get_next_run_times(
                    interval_seconds=task.interval_seconds,
                    cron_expression=task.cron_expression,
                    timezone=task.timezone,
                    run_once=task.run_once,
                    after=first_next_run,
                    count=2,
                )
            )

        return json.dumps(
            {
                "status": "success",
                "operation": operation,
                "message": t(MSG_SCHEDULED_TASK_CREATED),
                "task": ScheduledTaskResponse.model_validate(task).model_dump(mode="json"),
                "next_run_times": [run_time.isoformat() for run_time in next_run_times],
            },
            ensure_ascii=False,
        )
