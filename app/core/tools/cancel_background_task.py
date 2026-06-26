import json

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
                    "error": "Database context is not available.",
                },
                ensure_ascii=False,
            )

        from app.core.crud.background_task import background_task_crud
        from app.models.background_task import BackgroundTaskResponse, BackgroundTaskStatus

        task = await background_task_crud.cancel_user_task(self.db, task_id=task_id, uid=self.uid)
        if not task:
            return json.dumps(
                {
                    "status": "failed",
                    "error": "Background task not found.",
                    "task_id": task_id,
                },
                ensure_ascii=False,
            )

        return json.dumps(
            {
                "status": "success",
                "message": "Background task cancelled." if task.status == BackgroundTaskStatus.CANCELLED else "Background task is already finished.",
                "task": BackgroundTaskResponse.model_validate(task).model_dump(mode="json"),
            },
            ensure_ascii=False,
        )
