from datetime import datetime

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN
from app.core.i18n import t
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.providers.database.time import get_database_time

__all__ = ["interrupt_background_tasks"]


async def interrupt_background_tasks(db: AsyncSession, *, before: datetime) -> list[tuple[int, int | None, int | None]]:
    result = await db.execute(
        select(BackgroundTask)
        .where(
            BackgroundTask.status.in_((BackgroundTaskStatus.PENDING, BackgroundTaskStatus.RUNNING)),
            BackgroundTask.created_at <= before,
        )
        .order_by(BackgroundTask.id.asc())
    )
    tasks = list(result.scalars().all())
    interrupted: list[tuple[int, int | None, int | None]] = []
    if tasks:
        error = t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN)
        finished_at = await get_database_time(db)
        for task in tasks:
            if task.id is None:
                continue
            conditions = [
                BackgroundTask.id == task.id,
                BackgroundTask.status == task.status,
                BackgroundTask.locked_by.is_(None) if task.locked_by is None else BackgroundTask.locked_by == task.locked_by,
                BackgroundTask.lock_until.is_(None) if task.lock_until is None else BackgroundTask.lock_until == task.lock_until,
            ]
            update_result = await db.execute(
                update(BackgroundTask)
                .where(*conditions)
                .values(
                    status=BackgroundTaskStatus.FAILED,
                    error=error,
                    result={
                        "status": "execution_unknown",
                        "tool_name": task.tool_name,
                        "error": error,
                    },
                    extra={
                        **(task.extra if isinstance(task.extra, dict) else {}),
                        "execution_unknown": True,
                        "restart_interrupted": True,
                    },
                    finished_at=finished_at,
                    locked_by=None,
                    lock_until=None,
                    reply_status=BackgroundTaskReplyStatus.PENDING if task.auto_reply else BackgroundTaskReplyStatus.NONE,
                    reply_locked_by=None,
                    reply_lock_until=None,
                )
                .execution_options(synchronize_session=False)
            )
            if (update_result.rowcount or 0) == 1:
                interrupted.append((task.id, task.audit_record_id, task.audit_execution_record_id))
    await db.flush()
    return interrupted
