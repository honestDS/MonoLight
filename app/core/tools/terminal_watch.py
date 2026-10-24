import hashlib
import json
from datetime import timedelta
from typing import Any

from app.core import constants
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.terminal.schemas import TERMINAL_SESSION_FINAL_STATUSES, TerminalSessionStatus
from app.core.terminal.watch_schema import TerminalWatchRequest
from app.core.terminal.watcher import watch_terminal

from .base import BaseExecutor

__all__ = ["TERMINAL_WATCH_TOOL_SCHEMA", "TerminalWatchExecutor", "ensure_terminal_auto_watch"]


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


async def ensure_terminal_auto_watch(
    db: Any,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    terminal_session_id: str,
    after_offset: int,
    session_status: Any,
) -> None:
    if db is None or not isinstance(uid, str) or not uid.strip() or len(uid) > 100 or not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 100 or not isinstance(profile_id, int) or isinstance(profile_id, bool) or profile_id <= 0:
        return

    try:
        status = TerminalSessionStatus(session_status)
        request = TerminalWatchRequest(
            terminal_session_id=terminal_session_id,
            after_offset=after_offset,
            condition="process_exit",
        )
    except (TypeError, ValueError):
        return

    if status in TERMINAL_SESSION_FINAL_STATUSES:
        return

    if not callable(getattr(db, "execute", None)) or not callable(getattr(db, "commit", None)):
        return

    active_watch = await background_task_crud.get_active_terminal_watch(
        db,
        uid,
        session_id,
        request.terminal_session_id,
    )
    if active_watch is not None:
        return

    digest = hashlib.sha256(f"{request.terminal_session_id}:{request.after_offset}".encode()).hexdigest()[:32]
    await background_task_crud.create_task(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        tool_call_id=f"terminal-auto-watch:{digest}",
        tool_name=constants.TERMINAL_WATCH_TOOL_NAME,
        arguments={
            "terminal_session_id": request.terminal_session_id,
            "after_offset": request.after_offset,
            "condition": "process_exit",
        },
        auto_reply=True,
        extra={
            "source": "terminal_auto_watch",
            "auto_terminal_watch": True,
            "terminal_session_id": request.terminal_session_id,
            "submission_context": [],
        },
    )


class TerminalWatchExecutor(BaseExecutor):
    requires_audit = False

    def _tool_timeout(self) -> float:
        try:
            timeout = self.cfg.tool.tool_timeout
        except Exception:
            return 30.0
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
            return float(timeout)
        return 30.0

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

        tool_timeout = self._tool_timeout()
        deadline = task.created_at + timedelta(seconds=tool_timeout)
        await self.db.commit()
        result = await watch_terminal(
            task_id=task_id,
            uid=self.uid,
            session_id=self.session_id,
            request=request,
            deadline=deadline,
        )
        return json.dumps(result, ensure_ascii=False)
