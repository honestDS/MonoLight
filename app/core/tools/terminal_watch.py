import json
from datetime import datetime, timedelta
from typing import Any

from app.core import constants
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim, session_reply_work_item_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.terminal.schemas import TERMINAL_SESSION_FINAL_STATUSES, TerminalSessionStatus
from app.core.terminal.watch_schema import TerminalWatchRequest
from app.core.terminal.watcher import watch_terminal
from app.core.utils.time import get_local_time
from app.models.session_reply_work_item import SessionReplyWorkStatus

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

    await ensure_session_reply_work_claim(db)
    claim = getattr(db, "info", {}).get(constants.SESSION_REPLY_WORK_CLAIM_INFO_KEY)
    create_kwargs = {
        "uid": uid,
        "session_id": session_id,
        "profile_id": profile_id,
        "terminal_session_id": request.terminal_session_id,
        "after_offset": request.after_offset,
    }
    if claim is not None:
        if not isinstance(claim, (tuple, list)) or len(claim) != 2 or not isinstance(claim[0], int) or isinstance(claim[0], bool) or claim[0] <= 0 or not isinstance(claim[1], str) or not claim[1]:
            raise RuntimeError(t(constants.ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
        work_id, worker_id = claim
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.uid != uid or work.session_id != session_id or work.profile_id != profile_id or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id:
            raise RuntimeError(t(constants.ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))
        create_kwargs["origin_work_id"] = work.id
        create_kwargs["origin_work_identity"] = work.dedupe_key

    await background_task_crud.create_terminal_auto_watch(db, **create_kwargs)


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
        extra = task.extra if isinstance(task.extra, dict) else {}
        auto_watch = extra.get("auto_terminal_watch") is True
        if auto_watch:
            observation_started_at = task.created_at
            if isinstance(extra.get("observation_started_at"), str):
                observation_started_at = datetime.fromisoformat(extra["observation_started_at"])
            deadline = observation_started_at + timedelta(seconds=tool_timeout)
        else:
            deadline = task.created_at + timedelta(seconds=tool_timeout)

        await self.db.commit()
        if auto_watch:
            result = await watch_terminal(
                task_id=task_id,
                uid=self.uid,
                session_id=self.session_id,
                request=request,
                deadline=deadline,
                read_on_timeout=True,
            )
            result["observed_at"] = get_local_time().isoformat()
        else:
            result = await watch_terminal(
                task_id=task_id,
                uid=self.uid,
                session_id=self.session_id,
                request=request,
                deadline=deadline,
            )
        return json.dumps(result, ensure_ascii=False)
