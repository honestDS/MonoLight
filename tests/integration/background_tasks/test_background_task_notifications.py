import asyncio
from secrets import token_hex
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event, select

import app.core.background_tasks.external_work as external_work_module
import app.core.background_tasks.manager as manager_module
import app.core.background_tasks.recovery as recovery_module
import app.core.background_tasks.reply_trigger as reply_trigger_module
import app.core.background_tasks.runner as runner_module
import app.models  # noqa: F401
from app.core.crud.profile.profile import profile_crud
from app.core.crud.task.background import background_task_crud
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig
from app.core.notifications.protocol import (
    Notification,
    NotificationResource,
    NotificationScope,
    NotificationTopic,
)
from app.core.notifications.runtime import NotificationRuntime
from app.core.notifications.server import NotificationServer
from app.models.background_task import (
    BackgroundTask,
    BackgroundTaskReplyMode,
    BackgroundTaskReplyStatus,
    BackgroundTaskStatus,
)
from app.models.profile import Profile, ProfileConfig
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.user import User
from app.providers.database.client import CancellationSafeAsyncSession

_UID = "background-notification-user"
_SESSION_ID = "background-notification-session"
_PROFILE_ID = 1
_TOOL_NAME = "notification_test_tool"
_CLIENT_ID = "background-notification-test"
_WAIT_SECONDS = 5


class _ControlledExecutor:
    @classmethod
    def reset(cls) -> None:
        cls.started = asyncio.Queue()
        cls.cancelled = asyncio.Queue()
        cls.gates = {}
        cls.task_ids = {}

    def __init__(self, *, project_root: str, uid: str) -> None:
        self.project_root = project_root
        self.uid = uid
        self.config: ProfileConfig | None = None
        self.dispatch_context: Any = None

    def set_config(self, cfg: ProfileConfig) -> None:
        self.config = cfg

    def set_runtime_context(self, *, dispatch_context: Any) -> None:
        self.dispatch_context = dispatch_context

    async def execute(self, **arguments: Any) -> dict[str, str]:
        name = arguments["name"]
        type(self).task_ids[name] = self.dispatch_context.task_id
        await type(self).started.put(name)
        gate = type(self).gates.setdefault(name, asyncio.Event())
        try:
            await gate.wait()
        except asyncio.CancelledError:
            await type(self).cancelled.put(name)
            raise
        return {"status": "success", "content": name}


@pytest_asyncio.fixture
async def background_task_notifications(database_factory, monkeypatch: pytest.MonkeyPatch):
    async with database_factory(
        foreign_keys=True,
        wal=True,
        session_class=CancellationSafeAsyncSession,
    ) as factory:
        configs = ProfileConfig().model_dump()
        configs["tool"]["background_task_max_concurrency"] = 1
        async with factory() as db:
            db.add_all(
                [
                    User(uid=_UID, username="background_notification_user"),
                    Profile(
                        id=_PROFILE_ID,
                        uid=_UID,
                        name="background notification profile",
                        configs=configs,
                    ),
                    ChatSession(
                        session_id=_SESSION_ID,
                        uid=_UID,
                        profile_id=_PROFILE_ID,
                    ),
                ]
            )
            await db.commit()

        engine = factory.kw["bind"]
        assert engine is not None
        for module in (
            manager_module,
            runner_module,
            recovery_module,
            external_work_module,
            reply_trigger_module,
        ):
            monkeypatch.setattr(module, "AsyncSessionLocal", factory)

        class ControlledExecutor(_ControlledExecutor):
            pass

        ControlledExecutor.reset()
        monkeypatch.setitem(runner_module.TOOL_EXECUTOR_MAP, _TOOL_NAME, ControlledExecutor)

        token = token_hex(32)
        server = NotificationServer(NotificationConfig(host="127.0.0.1", port=0, token=token))
        runtime = None
        manager = None
        try:
            await asyncio.wait_for(server.start(), timeout=_WAIT_SECONDS)
            host, port = server.address
            client = NotificationClient(
                NotificationConfig(host=host, port=port, token=token),
                _CLIENT_ID,
            )
            runtime = NotificationRuntime(client, bind=engine.sync_engine)
            inbox = runtime.subscribe(
                (
                    NotificationTopic.BACKGROUND_TASK,
                    NotificationTopic.PROFILE_CONFIG,
                    NotificationTopic.SYSTEM_SETTINGS,
                )
            )
            runtime.start()
            await asyncio.wait_for(runtime.wait_ready(), timeout=_WAIT_SECONDS)

            manager = manager_module.BackgroundTaskManager()
            scan_completed = asyncio.Event()
            dispatch_pending_replies = manager.dispatch_pending_replies

            async def _signal_scan_completed(profile_ids: set[int] | None = None) -> None:
                await dispatch_pending_replies(profile_ids)
                scan_completed.set()

            manager.dispatch_pending_replies = _signal_scan_completed
            manager.start(inbox=inbox)
            await asyncio.wait_for(scan_completed.wait(), timeout=_WAIT_SECONDS)
            yield SimpleNamespace(
                factory=factory,
                engine=engine.sync_engine,
                inbox=inbox,
                manager=manager,
                runtime=runtime,
                scan_completed=scan_completed,
                server=server,
                executor=ControlledExecutor,
            )
        finally:
            if manager is not None:
                await manager.stop()
            if runtime is not None:
                await runtime.stop()
            await server.stop()


def _gate(context, name: str) -> asyncio.Event:
    return context.executor.gates.setdefault(name, asyncio.Event())


async def _create_task(
    context,
    name: str,
    *,
    auto_reply: bool = False,
    extra: dict[str, Any] | None = None,
) -> BackgroundTask:
    async with context.factory() as db:
        task = await background_task_crud.create_task(
            db,
            uid=_UID,
            session_id=_SESSION_ID,
            profile_id=_PROFILE_ID,
            tool_call_id=f"background-notification-call-{name}",
            tool_name=_TOOL_NAME,
            arguments={"name": name},
            auto_reply=auto_reply,
            extra=extra,
        )
    assert task.id is not None
    return task


async def _wait_started(context) -> str:
    async with asyncio.timeout(_WAIT_SECONDS):
        return await context.executor.started.get()


async def _get_task(context, task_id: int) -> BackgroundTask | None:
    async with context.factory() as db:
        return await db.get(BackgroundTask, task_id)


async def _wait_for_status(
    context,
    task_id: int,
    status: BackgroundTaskStatus,
) -> BackgroundTask:
    async with asyncio.timeout(_WAIT_SECONDS):
        while True:
            task = await _get_task(context, task_id)
            if task is not None and task.status == status:
                return task
            await asyncio.sleep(0.02)


async def _wait_for_reply_status(
    context,
    task_id: int,
    status: BackgroundTaskReplyStatus,
) -> BackgroundTask:
    async with asyncio.timeout(_WAIT_SECONDS):
        while True:
            task = await _get_task(context, task_id)
            if task is not None and task.reply_status == status:
                return task
            await asyncio.sleep(0.02)


async def _read_summary_work_items(context, task_id: int) -> list[SessionReplyWorkItem]:
    async with context.factory() as db:
        result = await db.execute(
            select(SessionReplyWorkItem).where(
                SessionReplyWorkItem.source_type == SessionReplySourceType.BACKGROUND_TASK,
                SessionReplyWorkItem.source_id == str(task_id),
            )
        )
        return list(result.scalars().all())


@pytest.mark.asyncio
async def test_committed_task_notification_runs_once_over_tcp(background_task_notifications) -> None:
    context = background_task_notifications
    name = "tcp-immediate"
    gate = _gate(context, name)
    task = await _create_task(context, name)

    assert await _wait_started(context) == name
    assert context.executor.task_ids[name] == task.id
    running = await _wait_for_status(context, task.id, BackgroundTaskStatus.RUNNING)
    assert running.attempt_count == 1

    await context.runtime.client.publish(
        Notification(
            topic=NotificationTopic.BACKGROUND_TASK,
            event="duplicate",
            source=_CLIENT_ID,
            scope=NotificationScope(profile_id=_PROFILE_ID),
            resource=NotificationResource(type="background_task", id=str(task.id)),
        )
    )
    await asyncio.sleep(0.1)
    assert context.executor.started.empty()

    gate.set()
    succeeded = await _wait_for_status(context, task.id, BackgroundTaskStatus.SUCCEEDED)
    assert succeeded.attempt_count == 1
    assert succeeded.result is not None
    assert succeeded.result["content"] == {"status": "success", "content": name}


@pytest.mark.asyncio
async def test_profile_concurrency_releases_pending_task_immediately(background_task_notifications) -> None:
    context = background_task_notifications
    first_name = "capacity-first"
    second_name = "capacity-second"
    first_gate = _gate(context, first_name)
    second_gate = _gate(context, second_name)
    first = await _create_task(context, first_name)
    assert await _wait_started(context) == first_name

    second = await _create_task(context, second_name)
    pending = await _wait_for_status(context, second.id, BackgroundTaskStatus.PENDING)
    assert pending.attempt_count == 0

    first_gate.set()
    assert await _wait_started(context) == second_name
    second_gate.set()
    first_done = await _wait_for_status(context, first.id, BackgroundTaskStatus.SUCCEEDED)
    second_done = await _wait_for_status(context, second.id, BackgroundTaskStatus.SUCCEEDED)
    assert first_done.attempt_count == 1
    assert second_done.attempt_count == 1


@pytest.mark.asyncio
async def test_profile_config_notification_increases_running_capacity(background_task_notifications) -> None:
    context = background_task_notifications
    first_name = "config-first"
    second_name = "config-second"
    first_gate = _gate(context, first_name)
    second_gate = _gate(context, second_name)
    first = await _create_task(context, first_name)
    assert await _wait_started(context) == first_name
    second = await _create_task(context, second_name)
    await _wait_for_status(context, second.id, BackgroundTaskStatus.PENDING)

    async with context.factory() as db:
        profile = await profile_crud.get(db, _PROFILE_ID)
        assert profile is not None
        configs = ProfileConfig.model_validate(profile.configs).model_dump()
        configs["tool"]["background_task_max_concurrency"] = 2
        await profile_crud.update(db, db_obj=profile, obj_in={"configs": configs})

    assert await _wait_started(context) == second_name
    first_running = await _get_task(context, first.id)
    assert first_running is not None
    assert first_running.status == BackgroundTaskStatus.RUNNING

    first_gate.set()
    second_gate.set()
    first_done = await _wait_for_status(context, first.id, BackgroundTaskStatus.SUCCEEDED)
    second_done = await _wait_for_status(context, second.id, BackgroundTaskStatus.SUCCEEDED)
    assert first_done.attempt_count == 1
    assert second_done.attempt_count == 1


@pytest.mark.asyncio
async def test_committed_cancel_notification_stops_running_tool_without_retry(background_task_notifications) -> None:
    context = background_task_notifications
    name = "cancel-running"
    _gate(context, name)
    task = await _create_task(context, name)
    assert await _wait_started(context) == name

    async with context.factory() as db:
        cancelled = await background_task_crud.cancel_user_task(db, task_id=task.id, uid=_UID)
    assert cancelled is not None
    assert cancelled.status == BackgroundTaskStatus.CANCELLED

    async with asyncio.timeout(_WAIT_SECONDS):
        assert await context.executor.cancelled.get() == name
    persisted = await _wait_for_status(context, task.id, BackgroundTaskStatus.CANCELLED)
    assert persisted.attempt_count == 1
    await asyncio.sleep(0.1)
    assert context.executor.started.empty()


@pytest.mark.asyncio
async def test_outer_rollback_discards_notification_and_next_commit_runs_in_same_session(background_task_notifications) -> None:
    context = background_task_notifications
    rolled_back_name = "rolled-back"
    committed_name = "committed-after-rollback"
    gate = _gate(context, committed_name)
    async with context.factory() as db:
        rolled_back = await background_task_crud.create_task(
            db,
            uid=_UID,
            session_id=_SESSION_ID,
            profile_id=_PROFILE_ID,
            tool_call_id=f"background-notification-call-{rolled_back_name}",
            tool_name=_TOOL_NAME,
            arguments={"name": rolled_back_name},
            auto_reply=False,
            commit=False,
        )
        rolled_back_id = rolled_back.id
        assert rolled_back_id is not None
        await db.rollback()

        async with context.factory() as reader:
            assert await reader.get(BackgroundTask, rolled_back_id) is None
        await asyncio.sleep(0.1)
        assert context.executor.started.empty()

        committed = await background_task_crud.create_task(
            db,
            uid=_UID,
            session_id=_SESSION_ID,
            profile_id=_PROFILE_ID,
            tool_call_id=f"background-notification-call-{committed_name}",
            tool_name=_TOOL_NAME,
            arguments={"name": committed_name},
            auto_reply=False,
        )
    assert committed.id is not None
    assert await _wait_started(context) == committed_name

    gate.set()
    completed = await _wait_for_status(context, committed.id, BackgroundTaskStatus.SUCCEEDED)
    assert completed.attempt_count == 1


@pytest.mark.asyncio
async def test_low_frequency_recovery_finds_task_without_notification(
    background_task_notifications,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    context = background_task_notifications
    monkeypatch.setattr(manager_module, "BACKGROUND_TASK_RECOVERY_INTERVAL_SECONDS", 0.1)
    context.scan_completed.clear()
    context.inbox.wake()
    await asyncio.wait_for(context.scan_completed.wait(), timeout=_WAIT_SECONDS)

    name = "recovered-without-notification"
    gate = _gate(context, name)
    async with context.factory() as db:
        task = BackgroundTask(
            uid=_UID,
            session_id=_SESSION_ID,
            profile_id=_PROFILE_ID,
            tool_call_id=f"background-notification-call-{name}",
            tool_name=_TOOL_NAME,
            arguments={"name": name},
            auto_reply=False,
            reply_status=BackgroundTaskReplyStatus.NONE,
        )
        db.add(task)
        await db.commit()
        assert task.id is not None
        task_id = task.id

    assert await _wait_started(context) == name
    gate.set()
    recovered = await _wait_for_status(context, task_id, BackgroundTaskStatus.SUCCEEDED)
    assert recovered.attempt_count == 1


@pytest.mark.asyncio
async def test_auto_reply_queues_one_ready_background_summary(background_task_notifications) -> None:
    context = background_task_notifications
    name = "auto-reply-summary"
    gate = _gate(context, name)
    task = await _create_task(
        context,
        name,
        auto_reply=True,
        extra={"reply_mode": BackgroundTaskReplyMode.SUMMARY.value},
    )
    assert await _wait_started(context) == name
    gate.set()

    await _wait_for_status(context, task.id, BackgroundTaskStatus.SUCCEEDED)
    replied = await _wait_for_reply_status(context, task.id, BackgroundTaskReplyStatus.RUNNING)
    work_items = await _read_summary_work_items(context, task.id)
    assert replied.reply_status == BackgroundTaskReplyStatus.RUNNING
    assert len(work_items) == 1
    assert work_items[0].work_type == SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY
    assert work_items[0].status == SessionReplyWorkStatus.READY_FOR_LLM


@pytest.mark.asyncio
async def test_idle_manager_does_not_poll_background_tasks_or_profiles(background_task_notifications) -> None:
    context = background_task_notifications
    select_count = 0

    def _count_task_and_profile_selects(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        nonlocal select_count
        normalized = statement.lstrip().lower()
        if normalized.startswith("select") and any(table_name in normalized for table_name in ("background_task", "profile")):
            select_count += 1

    event.listen(context.engine, "after_cursor_execute", _count_task_and_profile_selects)
    try:
        baseline = select_count
        await asyncio.sleep(1.2)
        assert select_count == baseline
    finally:
        event.remove(context.engine, "after_cursor_execute", _count_task_and_profile_selects)
