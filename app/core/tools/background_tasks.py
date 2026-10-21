import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.core.constants import (
    ERR_BACKGROUND_TASK_DB_CONTEXT_UNAVAILABLE,
    ERR_BACKGROUND_TASK_NOT_FOUND,
    ERR_TOOL_ARGUMENT_SCHEMA_INVALID,
    MANAGE_BACKGROUND_TASKS_TOOL_NAME,
    MSG_BACKGROUND_TASK_ALREADY_FINISHED,
    MSG_BACKGROUND_TASK_CANCELLED,
    MSG_BACKGROUND_TASK_LIST_SUCCESS,
)
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.models.background_task import BackgroundTaskResponse, BackgroundTaskStatus

from .base import BaseExecutor


class _ListBackgroundTasksArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str | None = None
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=100)


class _CancelBackgroundTaskArguments(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    task_id: int = Field(gt=0)


def _argument_error(operation: str, detail: object) -> str:
    return json.dumps(
        {
            "status": "failed",
            "operation": operation,
            "error": t(
                ERR_TOOL_ARGUMENT_SCHEMA_INVALID,
                tool_name=MANAGE_BACKGROUND_TASKS_TOOL_NAME,
                detail=json.dumps(detail, ensure_ascii=False),
            ),
        },
        ensure_ascii=False,
    )


MANAGE_BACKGROUND_TASKS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": MANAGE_BACKGROUND_TASKS_TOOL_NAME,
        "description": "List active background tasks or cancel a pending or running task owned by the current user.",
        "parameters": {
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": ["list", "cancel"],
                    "description": "Operation to perform: list active tasks or cancel a task.",
                },
                "session_id": {
                    "type": "string",
                    "description": "For list only: optional chat session id; defaults to the current session.",
                },
                "page": {
                    "type": "integer",
                    "description": "For list only: page number, starting from 1.",
                    "default": 1,
                    "minimum": 1,
                },
                "size": {
                    "type": "integer",
                    "description": "For list only: number of tasks per page, from 1 to 100.",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                },
                "task_id": {
                    "type": "integer",
                    "description": "For cancel only: positive id of a pending or running background task.",
                    "minimum": 1,
                },
            },
            "required": ["operation"],
            "additionalProperties": False,
        },
    },
}


class ManageBackgroundTasksExecutor(BaseExecutor):
    requires_audit = False

    async def execute(self, operation: str, **arguments) -> str:
        argument_model = {
            "list": _ListBackgroundTasksArguments,
            "cancel": _CancelBackgroundTaskArguments,
        }.get(operation)
        if argument_model is None:
            return _argument_error(operation, {"operation": operation, "allowed_operations": ["list", "cancel"]})

        try:
            parsed = argument_model.model_validate(arguments)
        except ValidationError as exc:
            return _argument_error(operation, exc.errors(include_url=False, include_context=False, include_input=False))

        if self.db is None:
            return json.dumps(
                {
                    "status": "failed",
                    "operation": operation,
                    "error": t(ERR_BACKGROUND_TASK_DB_CONTEXT_UNAVAILABLE),
                },
                ensure_ascii=False,
            )

        if operation == "list":
            offset = (parsed.page - 1) * parsed.size
            task_session_id = parsed.session_id or self.session_id
            tasks = await background_task_crud.list_active_user_tasks(
                self.db,
                uid=self.uid,
                session_id=task_session_id,
                skip=offset,
                limit=parsed.size,
            )
            return json.dumps(
                {
                    "status": "success",
                    "operation": operation,
                    "message": t(MSG_BACKGROUND_TASK_LIST_SUCCESS),
                    "page": parsed.page,
                    "size": parsed.size,
                    "session_id": task_session_id,
                    "tasks": [BackgroundTaskResponse.model_validate(task).model_dump(mode="json") for task in tasks],
                },
                ensure_ascii=False,
            )

        task = await background_task_crud.cancel_user_task(self.db, task_id=parsed.task_id, uid=self.uid)
        if not task:
            return json.dumps(
                {
                    "status": "failed",
                    "operation": operation,
                    "error": t(ERR_BACKGROUND_TASK_NOT_FOUND),
                    "task_id": parsed.task_id,
                },
                ensure_ascii=False,
            )

        if task.status == BackgroundTaskStatus.CANCELLED and task.audit_record_id is not None:
            # 避免工具注册与审计初始化之间形成循环依赖。
            from app.core.audit.confirmation import update_confirmation_message_status

            await update_confirmation_message_status(self.db, audit_record_id=task.audit_record_id)

        return json.dumps(
            {
                "status": "success",
                "operation": operation,
                "message": t(MSG_BACKGROUND_TASK_CANCELLED) if task.status == BackgroundTaskStatus.CANCELLED else t(MSG_BACKGROUND_TASK_ALREADY_FINISHED),
                "task": BackgroundTaskResponse.model_validate(task).model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
