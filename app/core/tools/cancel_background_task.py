import json

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.constants import (
    ERR_BACKGROUND_TASK_DB_CONTEXT_UNAVAILABLE,
    ERR_BACKGROUND_TASK_NOT_FOUND,
    MSG_BACKGROUND_TASK_ALREADY_FINISHED,
    MSG_BACKGROUND_TASK_CANCELLED,
)
from app.core.crud.background_task import background_task_crud
from app.core.i18n import t
from app.models.background_task import BackgroundTaskResponse, BackgroundTaskStatus

from .base import BaseExecutor

CANCEL_BACKGROUND_TASK_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "cancel_background_task",
        "description": "Cancel one of the current user's pending or running background tool tasks by task_id.",
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {
                    "type": "integer",
                    "description": "The background task id returned when a tool was queued for background execution.",
                }
            },
            "required": ["task_id"],
            "additionalProperties": False,
        },
    },
}


class CancelBackgroundTaskExecutor(BaseExecutor):
    async def execute(self, task_id: int) -> str:
        if self.db is None:
            return json.dumps(
                {
                    "status": "failed",
                    "error": t(ERR_BACKGROUND_TASK_DB_CONTEXT_UNAVAILABLE),
                },
                ensure_ascii=False,
            )

        task = await background_task_crud.cancel_user_task(self.db, task_id=task_id, uid=self.uid)
        if not task:
            return json.dumps(
                {
                    "status": "failed",
                    "error": t(ERR_BACKGROUND_TASK_NOT_FOUND),
                    "task_id": task_id,
                },
                ensure_ascii=False,
            )

        if task.status == BackgroundTaskStatus.CANCELLED and task.audit_record_id is not None:
            await update_confirmation_message_status(self.db, audit_record_id=task.audit_record_id)

        return json.dumps(
            {
                "status": "success",
                "message": t(MSG_BACKGROUND_TASK_CANCELLED) if task.status == BackgroundTaskStatus.CANCELLED else t(MSG_BACKGROUND_TASK_ALREADY_FINISHED),
                "task": BackgroundTaskResponse.model_validate(task).model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
