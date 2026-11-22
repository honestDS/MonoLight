import asyncio

from sqlalchemy import update

from app.core.constants import (
    END_SESSION_SCOPE_TASK,
    NOTIFICATION_ERROR_RETRY_SECONDS,
    NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
)
from app.core.crud.account.user import user_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.notifications.inbox import NotificationInbox
from app.core.notifications.protocol import NotificationTopic
from app.core.profile.selection import resolve_profile_for_session
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.scheduled_time import get_next_run_times
from app.core.utils.time import get_local_time
from app.models.agent_work import AgentWorkReportTarget, AgentWorkSpec
from app.models.message import Message, MessageRole, MessageType
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.session_reply_work_item import SessionReplySourceType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

SCHEDULED_TASK_FETCH_LIMIT = 100


class ScheduledTaskScheduler:
    def __init__(self) -> None:
        self._task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._active_dispatch_tasks: set[asyncio.Task] = set()
        self._inflight_task_ids: set[int] = set()
        self._inbox = NotificationInbox()

    def start(self, *, inbox: NotificationInbox | None = None) -> asyncio.Task:
        if self._task and not self._task.done():
            return self._task
        self._stop_event.clear()
        self._inbox = inbox if inbox is not None else NotificationInbox()
        self._inbox.wake()
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
        loop = asyncio.get_running_loop()
        next_reconcile_at = 0.0
        next_due_at = 0.0
        while not self._stop_event.is_set():
            try:
                now = loop.time()
                batch = self._inbox.take()
                full_scan = batch.full_scan or now >= next_reconcile_at or now >= next_due_at
                task_ids = None
                if full_scan:
                    next_reconcile_at = now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS
                if not full_scan:
                    task_ids = batch.resource_ids(NotificationTopic.SCHEDULED_TASK, "scheduled_task")
                if task_ids is None or task_ids:
                    await self.dispatch_due_tasks(task_ids=task_ids)

                async with AsyncSessionLocal() as db:
                    next_run_at = await scheduled_task_crud.get_next_run_at(
                        db,
                        exclude_ids=set(self._inflight_task_ids),
                    )
                local_now = get_local_time()
                if next_run_at is not None and next_run_at.tzinfo is None:
                    next_run_at = next_run_at.replace(tzinfo=local_now.tzinfo)
                next_due_at = float("inf")
                if next_run_at is not None:
                    next_due_at = loop.time() + max(0.0, (next_run_at - local_now).total_seconds())

                await self._inbox.wait(max(0.0, min(next_reconcile_at, next_due_at) - loop.time()))
            except Exception:
                logger.bind(component="scheduled_task_scheduler").exception(t("LOG_SCHEDULED_TASK_DISPATCH_FAILED"))
                self._inbox.wake()
                await asyncio.sleep(NOTIFICATION_ERROR_RETRY_SECONDS)

    async def dispatch_due_tasks(self, *, task_ids: set[int] | None = None) -> None:
        async with AsyncSessionLocal() as db:
            scheduled_tasks = await scheduled_task_crud.list_due(
                db,
                limit=SCHEDULED_TASK_FETCH_LIMIT,
                task_ids=task_ids,
                exclude_ids=set(self._inflight_task_ids),
            )
        for scheduled_task in scheduled_tasks:
            if scheduled_task.id is None or scheduled_task.id in self._inflight_task_ids:
                continue
            self._inflight_task_ids.add(scheduled_task.id)
            task = asyncio.create_task(self._run_dispatch(scheduled_task.id))
            self._active_dispatch_tasks.add(task)
            task.add_done_callback(lambda completed_task, task_id=scheduled_task.id: self._dispatch_finished(task_id, completed_task))

    def _dispatch_finished(self, task_id: int, task: asyncio.Task) -> None:
        self._active_dispatch_tasks.discard(task)
        self._inflight_task_ids.discard(task_id)
        self._inbox.wake()

    async def _dispatch_one(self, scheduled_task_id: int) -> None:
        async with AsyncSessionLocal() as db:
            scheduled_task = await scheduled_task_crud.get(db, scheduled_task_id)
            if scheduled_task is None:
                return
            await self._dispatch_one_with_db(db, scheduled_task)

    async def _run_dispatch(self, scheduled_task_id: int) -> None:
        try:
            await self._dispatch_one(scheduled_task_id)
        except Exception:
            logger.bind(component="scheduled_task_scheduler", scheduled_task_id=scheduled_task_id).exception(t("LOG_SCHEDULED_TASK_DISPATCH_FAILED"))
            await asyncio.sleep(NOTIFICATION_ERROR_RETRY_SECONDS)

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

        resolved_profile = await resolve_profile_for_session(
            db,
            uid=scheduled_task.uid,
            session_id=scheduled_task.session_id,
        )
        resolved_profile_id = resolved_profile.id if resolved_profile is not None else None
        profile = (
            await profile_crud.lock_for_runtime_use(
                db,
                profile_id=resolved_profile_id,
                uid=scheduled_task.uid,
            )
            if resolved_profile_id is not None
            else None
        )
        if not profile or not profile.id:
            log.bind(profile_id=resolved_profile_id).warning(t("LOG_SCHEDULED_TASK_PROFILE_MISSING"))
            await scheduled_task_crud.disable_task(db, scheduled_task=scheduled_task)
            return

        old_next_run_at = scheduled_task.next_run_at
        now = get_local_time()
        if scheduled_task.run_once:
            next_run_at = old_next_run_at
        else:
            next_run_at = get_next_run_times(
                interval_seconds=scheduled_task.interval_seconds,
                cron_expression=scheduled_task.cron_expression,
                timezone=scheduled_task.timezone,
                after=now,
                count=1,
            )[0]
        claimed = await db.execute(
            update(ScheduledTask)
            .where(
                ScheduledTask.id == scheduled_task.id,
                ScheduledTask.status == ScheduledTaskStatus.ENABLED,
                ScheduledTask.next_run_at == old_next_run_at,
                ScheduledTask.next_run_at <= now,
            )
            .values(
                status=ScheduledTaskStatus.DISABLED if scheduled_task.run_once else ScheduledTaskStatus.ENABLED,
                last_run_at=now,
                run_count=ScheduledTask.run_count + 1,
                next_run_at=next_run_at,
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
        await message_crud.persist(
            db,
            message=trigger_message,
            commit=False,
            update_session_activity=False,
        )
        await db.execute(update(ScheduledTask).where(ScheduledTask.id == scheduled_task.id).values(last_message_id=trigger_message.id).execution_options(synchronize_session=False))
        scheduled_work, _created = await session_reply_queue_manager.enqueue_agent_work(
            db,
            uid=scheduled_task.uid,
            session_id=scheduled_task.session_id,
            profile_id=profile.id,
            source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
            source_id=trigger_message.id,
            dedupe_key=f"scheduled-agent-work:{scheduled_task.id}:{trigger_message.id}",
            spec=AgentWorkSpec(
                instruction=scheduled_task.message,
                execution_scope=END_SESSION_SCOPE_TASK,
                report_target=AgentWorkReportTarget.SESSION,
                instruction_message_id=trigger_message.id,
            ),
            commit=False,
        )
        trigger_message.work_id = scheduled_work.id
        db.add(trigger_message)
        await db.flush()
        await db.commit()
        log.bind(message_id=trigger_message.id, profile_id=profile.id).info(t("LOG_SCHEDULED_TASK_MESSAGE_QUEUED"))
