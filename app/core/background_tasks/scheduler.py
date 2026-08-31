import asyncio
from datetime import timedelta

from sqlalchemy import update

from app.core.crud.account.user import user_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.time import get_local_time
from app.models.message import Message, MessageRole, MessageType
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

SCHEDULED_TASK_POLL_INTERVAL_SECONDS = 1
SCHEDULED_TASK_FETCH_LIMIT = 100


class ScheduledTaskScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._active_dispatch_tasks: set[asyncio.Task] = set()
        self._inflight_task_ids: set[int] = set()

    def start(self) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._task = asyncio.create_task(self._run_loop())
        return self._task

    async def stop(self) -> None:
        self._stop_event.set()
        for task in self._active_dispatch_tasks:
            task.cancel()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        if self._active_dispatch_tasks:
            await asyncio.gather(*self._active_dispatch_tasks, return_exceptions=True)

    async def _run_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self.dispatch_due_tasks()
            except Exception:
                logger.bind(component="scheduled_task_scheduler").exception(t("LOG_SCHEDULED_TASK_DISPATCH_FAILED"))
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=SCHEDULED_TASK_POLL_INTERVAL_SECONDS)
            except TimeoutError:
                continue

    async def dispatch_due_tasks(self) -> None:
        async with AsyncSessionLocal() as db:
            scheduled_tasks = await scheduled_task_crud.list_due(db, limit=SCHEDULED_TASK_FETCH_LIMIT)
        for scheduled_task in scheduled_tasks:
            if scheduled_task.id is None or scheduled_task.id in self._inflight_task_ids:
                continue
            self._inflight_task_ids.add(scheduled_task.id)
            task = asyncio.create_task(self._dispatch_one(scheduled_task.id))
            self._active_dispatch_tasks.add(task)
            task.add_done_callback(self._active_dispatch_tasks.discard)
            task.add_done_callback(lambda _task, task_id=scheduled_task.id: self._inflight_task_ids.discard(task_id))

    async def _dispatch_one(self, scheduled_task_id: int) -> None:
        async with AsyncSessionLocal() as db:
            scheduled_task = await scheduled_task_crud.get(db, scheduled_task_id)
            if scheduled_task is None:
                return
            await self._dispatch_one_with_db(db, scheduled_task)

    async def _dispatch_one_with_db(self, db, scheduled_task: ScheduledTask) -> None:
        log = logger.bind(
            uid=scheduled_task.uid,
            session_id=scheduled_task.session_id,
            scheduled_task_id=scheduled_task.id,
            scheduled_task_name=scheduled_task.name,
        )
        session = await session_crud.get_by_session_id(db, scheduled_task.session_id)
        if not session or session.uid != scheduled_task.uid:
            log.warning(t("LOG_SCHEDULED_TASK_SESSION_MISSING"))
            await scheduled_task_crud.mark_skipped(db, scheduled_task=scheduled_task)
            return

        profile = await profile_crud.get_with_relations(db, scheduled_task.profile_id) if scheduled_task.profile_id else None
        if not profile or not profile.id or profile.uid != scheduled_task.uid:
            log.bind(profile_id=scheduled_task.profile_id).warning(t("LOG_SCHEDULED_TASK_PROFILE_MISSING"))
            await scheduled_task_crud.disable_task(db, scheduled_task=scheduled_task)
            return

        old_next_run_at = scheduled_task.next_run_at
        now = get_local_time()
        claimed = await db.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == scheduled_task.id,
                ScheduledTask.status == ScheduledTaskStatus.ENABLED,
                ScheduledTask.next_run_at == old_next_run_at,
                ScheduledTask.next_run_at <= now,
            )
            .values(
                last_run_at=now,
                run_count=ScheduledTask.run_count + 1,
                next_run_at=now + timedelta(seconds=scheduled_task.interval_seconds),
                updated_at=now,
            )
            .execution_options(synchronize_session=False)
        )
        if (claimed.rowcount or 0) != 1:
            await db.rollback()
            return

        user = await user_crud.get_by_uid(db, scheduled_task.uid)
        username = user.username if user else "Unknown"
        log.info(t("LOG_DISPATCHER_USER_MESSAGE", username=username, message=scheduled_task.message, attachments=str(None)))
        trigger_message = Message(
            session_id=scheduled_task.session_id,
            uid=scheduled_task.uid,
            role=MessageRole.USER,
            type=MessageType.SCHEDULED_TASK_TRIGGER,
            content=scheduled_task.message,
            profile_id=profile.id,
            is_processed=True,
        )
        db.add(trigger_message)
        await db.flush()
        await db.execute(update(ScheduledTask).where(ScheduledTask.id == scheduled_task.id).values(last_message_id=trigger_message.id).execution_options(synchronize_session=False))
        await session_reply_queue_manager.enqueue_scheduled_summary(
            db,
            uid=scheduled_task.uid,
            session_id=scheduled_task.session_id,
            profile_id=profile.id,
            scheduled_task_id=scheduled_task.id,
            trigger_message_id=trigger_message.id,
            commit=False,
        )
        await db.commit()
        log.bind(message_id=trigger_message.id, profile_id=profile.id).info(t("LOG_SCHEDULED_TASK_MESSAGE_QUEUED"))


scheduled_task_scheduler = ScheduledTaskScheduler()
