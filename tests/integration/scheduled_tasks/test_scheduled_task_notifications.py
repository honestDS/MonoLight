import asyncio
from datetime import datetime, timedelta
from secrets import token_hex
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.core.background_tasks.scheduler as scheduler_module
import app.models  # noqa: F401
from app.core.crud.task.scheduled import scheduled_task_crud
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig
from app.core.notifications.protocol import NotificationTopic
from app.core.notifications.runtime import NotificationRuntime
from app.core.notifications.server import NotificationServer
from app.core.notifications.transactions import notify_after_commit
from app.core.utils.time import get_local_time
from app.models.profile import Profile, ProfileConfig
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User

UID = "notification-user"
SESSION_ID = "notification-session"
PROFILE_ID = 1


@pytest_asyncio.fixture
async def scheduled_task_notifications(database_factory, monkeypatch: pytest.MonkeyPatch):
    async with database_factory(foreign_keys=True, wal=True) as factory:
        async with factory() as db:
            db.add(User(uid=UID, username="notification-user"))
            await db.flush()
            await db.commit()

            db.add(
                Profile(
                    id=PROFILE_ID,
                    uid=UID,
                    name="notification-profile",
                    configs=ProfileConfig().model_dump(),
                    is_default=True,
                )
            )
            await db.flush()
            await db.commit()

            db.add(ChatSession(session_id=SESSION_ID, uid=UID, profile_id=PROFILE_ID))
            await db.flush()
            await db.commit()

        engine = factory.kw["bind"]
        assert engine is not None
        sync_engine = engine.sync_engine
        monkeypatch.setattr(scheduler_module, "AsyncSessionLocal", factory)

        token = token_hex(32)
        async with NotificationServer(NotificationConfig(host="127.0.0.1", port=0, token=token)) as server:
            host, port = server.address
            config = NotificationConfig(host=host, port=port, token=token)
            client = NotificationClient(config, f"scheduled-task-test:{token_hex(8)}")
            runtime = NotificationRuntime(client, bind=sync_engine)
            inbox = runtime.subscribe((NotificationTopic.SCHEDULED_TASK,))

            scan_events: asyncio.Queue[int] = asyncio.Queue()
            scan_count = 0
            original_get_next_run_at = scheduler_module.scheduled_task_crud.get_next_run_at

            async def track_next_run_at(*args, **kwargs):
                nonlocal scan_count
                result = await original_get_next_run_at(*args, **kwargs)
                scan_count += 1
                scan_events.put_nowait(scan_count)
                return result

            async def wait_for_scans(count: int) -> None:
                async with asyncio.timeout(5):
                    while scan_count < count:
                        await scan_events.get()

            monkeypatch.setattr(
                scheduler_module.scheduled_task_crud,
                "get_next_run_at",
                track_next_run_at,
            )
            scheduler = scheduler_module.ScheduledTaskScheduler()
            try:
                runtime.start()
                async with asyncio.timeout(5):
                    await runtime.wait_ready()
                inbox.take()
                scheduler.start(inbox=inbox)
                await wait_for_scans(1)
                yield SimpleNamespace(
                    factory=factory,
                    engine=sync_engine,
                    inbox=inbox,
                    runtime=runtime,
                    scheduler=scheduler,
                    server=server,
                    wait_for_scans=wait_for_scans,
                )
            finally:
                await scheduler.stop()
                await runtime.stop()


async def _create_run_once_task(
    factory: async_sessionmaker[AsyncSession],
    *,
    name: str,
    due_in: timedelta,
) -> tuple[int, datetime]:
    async with factory() as db:
        task = await scheduled_task_crud.create_scheduled_task(
            db,
            name=name,
            uid=UID,
            session_id=SESSION_ID,
            message=f"Run {name}",
            interval_seconds=60,
            run_once=True,
        )
        next_run_at = get_local_time() + due_in
        task = await scheduled_task_crud.update_scheduled_task(
            db,
            scheduled_task=task,
            obj_in={"next_run_at": next_run_at},
        )
        assert task.id is not None
        return task.id, next_run_at


async def _read_work_items(
    factory: async_sessionmaker[AsyncSession],
) -> list[SessionReplyWorkItem]:
    async with factory() as db:
        result = await db.execute(
            select(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.uid == UID,
                SessionReplyWorkItem.session_id == SESSION_ID,
                SessionReplyWorkItem.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN,
            )
            .order_by(SessionReplyWorkItem.id)
        )
        return list(result.scalars().all())


async def _wait_for_work_items(
    factory: async_sessionmaker[AsyncSession],
    *,
    count: int = 1,
    timeout: float = 5,
) -> list[SessionReplyWorkItem]:
    async with asyncio.timeout(timeout):
        while True:
            items = await _read_work_items(factory)
            if len(items) >= count:
                return items
            await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_committed_task_notification_dispatches_due_task_once(
    scheduled_task_notifications,
) -> None:
    task_id, _ = await _create_run_once_task(
        scheduled_task_notifications.factory,
        name="notified-due-task",
        due_in=timedelta(seconds=-1),
    )

    work_items = await _wait_for_work_items(scheduled_task_notifications.factory)
    assert len(work_items) == 1
    work = work_items[0]
    assert work.work_type == SessionReplyWorkType.AGENT_WORK
    assert work.source_type == SessionReplySourceType.SCHEDULED_TASK_RUN
    assert work.status == SessionReplyWorkStatus.READY_FOR_LLM

    async with scheduled_task_notifications.factory() as db:
        task = await db.get(ScheduledTask, task_id)
    assert task is not None
    assert task.status == ScheduledTaskStatus.DISABLED
    assert task.run_count == 1


@pytest.mark.asyncio
async def test_future_due_task_dispatches_from_scheduler_deadline_without_another_notification(
    scheduled_task_notifications,
) -> None:
    task_id, due_at = await _create_run_once_task(
        scheduled_task_notifications.factory,
        name="deadline-task",
        due_in=timedelta(seconds=0.3),
    )

    work_items = await _wait_for_work_items(
        scheduled_task_notifications.factory,
        timeout=3,
    )
    assert len(work_items) == 1
    assert work_items[0].work_type == SessionReplyWorkType.AGENT_WORK

    async with scheduled_task_notifications.factory() as db:
        task = await db.get(ScheduledTask, task_id)
    assert task is not None
    assert task.status == ScheduledTaskStatus.DISABLED
    assert task.run_count == 1
    assert task.last_run_at is not None
    last_run_at = task.last_run_at
    if last_run_at.tzinfo is None and due_at.tzinfo is not None:
        last_run_at = last_run_at.replace(tzinfo=due_at.tzinfo)
    assert last_run_at >= due_at
    assert (last_run_at - due_at).total_seconds() < 3


@pytest.mark.asyncio
async def test_database_reconciliation_recovers_due_task_without_notification(
    scheduled_task_notifications,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler_module, "NOTIFICATION_RECONCILE_INTERVAL_SECONDS", 0.1)
    await scheduled_task_notifications.wait_for_scans(1)
    scheduled_task_notifications.inbox.wake()
    await scheduled_task_notifications.wait_for_scans(2)

    async with scheduled_task_notifications.factory() as db:
        task = ScheduledTask(
            name="reconciled-task",
            uid=UID,
            session_id=SESSION_ID,
            message="Run after reconciliation",
            interval_seconds=60,
            run_once=True,
            next_run_at=get_local_time() - timedelta(seconds=1),
        )
        db.add(task)
        await db.commit()
        assert task.id is not None
        task_id = task.id

    work_items = await _wait_for_work_items(
        scheduled_task_notifications.factory,
        timeout=3,
    )
    assert len(work_items) == 1
    assert work_items[0].work_type == SessionReplyWorkType.AGENT_WORK

    async with scheduled_task_notifications.factory() as db:
        persisted_task = await db.get(ScheduledTask, task_id)
    assert persisted_task is not None
    assert persisted_task.status == ScheduledTaskStatus.DISABLED
    assert persisted_task.run_count == 1


@pytest.mark.asyncio
async def test_idle_scheduler_does_not_poll_scheduled_tasks_before_reconcile_deadline(
    scheduled_task_notifications,
) -> None:
    scheduled_task_selects = 0

    def count_scheduled_task_selects(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal scheduled_task_selects
        normalized = statement.lstrip().lower()
        if normalized.startswith("select") and "scheduled_task" in normalized:
            scheduled_task_selects += 1

    event.listen(
        scheduled_task_notifications.engine,
        "after_cursor_execute",
        count_scheduled_task_selects,
    )
    try:
        baseline = scheduled_task_selects
        await asyncio.sleep(1.2)
        assert scheduled_task_selects - baseline == 0
    finally:
        event.remove(
            scheduled_task_notifications.engine,
            "after_cursor_execute",
            count_scheduled_task_selects,
        )


@pytest.mark.asyncio
async def test_fetch_limit_does_not_block_third_due_task_behind_inflight_dispatches(
    scheduled_task_notifications,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(scheduler_module, "SCHEDULED_TASK_FETCH_LIMIT", 2)
    started: asyncio.Queue[int] = asyncio.Queue()
    release = asyncio.Event()
    dispatch_lock = asyncio.Lock()
    original_dispatch_one = scheduled_task_notifications.scheduler._dispatch_one

    async def gated_dispatch_one(task_id: int) -> None:
        await started.put(task_id)
        await release.wait()
        async with dispatch_lock:
            await original_dispatch_one(task_id)

    scheduled_task_notifications.scheduler._dispatch_one = gated_dispatch_one

    async with scheduled_task_notifications.factory() as db:
        tasks = [
            ScheduledTask(
                name=f"batch-task-{index}",
                uid=UID,
                session_id=SESSION_ID,
                message=f"Run batch task {index}",
                interval_seconds=60,
                run_once=True,
                next_run_at=get_local_time() - timedelta(seconds=1),
            )
            for index in range(3)
        ]
        db.add_all(tasks)
        await db.flush()
        task_ids = {task.id for task in tasks}
        assert all(task_id is not None for task_id in task_ids)
        task_ids = {task_id for task_id in task_ids if task_id is not None}
        notify_after_commit(
            db,
            NotificationTopic.SCHEDULED_TASK,
            uid=UID,
            session_id=SESSION_ID,
        )
        await db.commit()

    started_ids: list[int] = []
    async with asyncio.timeout(5):
        for _ in range(3):
            started_ids.append(await started.get())
    assert set(started_ids) == task_ids

    release.set()
    work_items = await _wait_for_work_items(
        scheduled_task_notifications.factory,
        count=3,
        timeout=5,
    )
    assert len(work_items) == 3
    assert len({item.source_id for item in work_items}) == 3
    assert all(item.work_type == SessionReplyWorkType.AGENT_WORK for item in work_items)

    async with scheduled_task_notifications.factory() as db:
        persisted_tasks = [await db.get(ScheduledTask, task_id) for task_id in task_ids]
    assert all(task is not None for task in persisted_tasks)
    assert all(task.status == ScheduledTaskStatus.DISABLED for task in persisted_tasks)
    assert all(task.run_count == 1 for task in persisted_tasks)
    assert {str(task.last_message_id) for task in persisted_tasks} == {item.source_id for item in work_items}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["disable", "delete"])
async def test_disabling_or_deleting_imminent_task_prevents_due_work(
    scheduled_task_notifications,
    operation: str,
) -> None:
    task_id, due_at = await _create_run_once_task(
        scheduled_task_notifications.factory,
        name=f"cancelled-task-{operation}",
        due_in=timedelta(seconds=1),
    )

    async with scheduled_task_notifications.factory() as db:
        task = await db.get(ScheduledTask, task_id)
        assert task is not None
        assert get_local_time() < due_at
        if operation == "disable":
            await scheduled_task_crud.disable_task(db, scheduled_task=task)
        else:
            await scheduled_task_crud.delete_task(db, scheduled_task=task)

    await asyncio.sleep(max(0, (due_at - get_local_time()).total_seconds()) + 0.2)
    assert await _read_work_items(scheduled_task_notifications.factory) == []

    async with scheduled_task_notifications.factory() as db:
        persisted_task = await db.get(ScheduledTask, task_id)
    if operation == "disable":
        assert persisted_task is not None
        assert persisted_task.status == ScheduledTaskStatus.DISABLED
    else:
        assert persisted_task is None
