import json
from datetime import timedelta

from app.core import constants
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.terminal.watch_schema import TerminalWatchRequest
from app.core.terminal.watcher import watch_terminal

from .base import BaseExecutor

__all__ = ["TERMINAL_WATCH_TOOL_SCHEMA", "TerminalWatchExecutor"]


TERMINAL_WATCH_TOOL_SCHEMA = {
    "type": "function",
    "function": {
        "name": constants.TERMINAL_WATCH_TOOL_NAME,
        "description": (
            "Perform a one-shot background observation of an interactive terminal session. "
            "The system suspends the task while observing instead of polling the LLM. "
            "Use manage_background_tasks to cancel the task. "
            "output_contains uses raw literal matching, not a regular expression. "
            "quiet/output only report observed terminal output timing and do not prove business completion. "
            "Set after_offset from terminal_write.read_offset or terminal_read.next_offset."
        ),
        "parameters": TerminalWatchRequest.model_json_schema(),
    },
}


class TerminalWatchExecutor(BaseExecutor):
    requires_audit = False

    async def execute(self, **arguments) -> str:
        request = TerminalWatchRequest.model_validate(arguments)
        dispatch_context = self.dispatch_context
        task_id = dispatch_context.task_id if dispatch_context is not None else None
        tool_call_id = dispatch_context.tool_call_id if dispatch_context is not None else None
        if self.db is None or not self.session_id or task_id is None or not tool_call_id:
            raise RuntimeError(t(constants.ERR_TOOL_RUNTIME_CONTEXT_MISSING))

        task = await background_task_crud.get(self.db, task_id)
        if task is None or task.uid != self.uid or task.session_id != self.session_id:
            raise RuntimeError(t(constants.ERR_TOOL_RUNTIME_CONTEXT_MISSING))

        deadline = task.created_at + timedelta(seconds=request.timeout_seconds)
        await self.db.commit()
        result = await watch_terminal(
            task_id=task_id,
            uid=self.uid,
            session_id=self.session_id,
            request=request,
            deadline=deadline,
        )
        return json.dumps(result, ensure_ascii=False)
