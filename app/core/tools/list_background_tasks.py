import json

from app.core import constants
from app.core.crud.background_task import background_task_crud
from app.core.i18n import t
from app.models.background_task import BackgroundTaskResponse

from .base import BaseExecutor

LIST_BACKGROUND_TASKS_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": "list_background_tasks",
        "description": "List the current user's active background tool tasks, optionally limited to a chat session. Only pending and running tasks are returned.",
        "parameters": {
            "type": "object",
            "properties": {
                "session_id": {
                    "type": "string",
                    "description": "Optional chat session id used to filter background tasks.",
                },
                "page": {
                    "type": "integer",
                    "description": "Page number, starting from 1.",
                    "default": 1,
                    "minimum": 1,
                },
                "size": {
                    "type": "integer",
                    "description": "Number of tasks per page. Maximum 100.",
                    "default": 20,
                    "minimum": 1,
                    "maximum": 100,
                },
            },
        },
    },
}


class ListBackgroundTasksExecutor(BaseExecutor):
    async def execute(self, session_id: str | None = None, page: int = 1, size: int = 20) -> str:
        if self.db is None:
            return json.dumps(
                {
                    "status": "failed",
                    "error": t(constants.ERR_BACKGROUND_TASK_DB_CONTEXT_UNAVAILABLE),
                },
                ensure_ascii=False,
            )

        page = max(1, int(page or 1))
        size = min(100, max(1, int(size or 20)))
        offset = (page - 1) * size
        task_session_id = session_id or self.session_id
        tasks = await background_task_crud.list_active_user_tasks(
            self.db,
            uid=self.uid,
            session_id=task_session_id,
            skip=offset,
            limit=size,
        )

        return json.dumps(
            {
                "status": "success",
                "message": t(constants.MSG_BACKGROUND_TASK_LIST_SUCCESS),
                "page": page,
                "size": size,
                "session_id": task_session_id,
                "tasks": [BackgroundTaskResponse.model_validate(task).model_dump(mode="json") for task in tasks],
            },
            ensure_ascii=False,
        )
