import asyncio
import secrets
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

import app.core.notifications.client as notification_client_module
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig
from app.core.notifications.inbox import NotificationInbox
from app.core.notifications.protocol import Notification
from app.core.notifications.runtime import NotificationRuntime
from app.core.notifications.server import NotificationServer
from app.core.notifications.transactions import notify_after_commit
from app.models.system_setting import SystemSetting
from app.providers.database import ensure_sqlite_outer_transaction

_WAIT_SECONDS = 5.0
_QUIET_SECONDS = 0.1
_TOPICS = ("session.reply", "task.background", "memory.job")


@pytest_asyncio.fixture
async def notification_context(database_factory):
    async with database_factory(tables=[SystemSetting.__table__]) as factory:
        token = secrets.token_hex(32)
        server = NotificationServer(NotificationConfig(host="127.0.0.1", port=0, token=token))
        servers = [server]
        runtime = None
        try:
            await asyncio.wait_for(server.start(), timeout=_WAIT_SECONDS)
            host, port = server.address
            config = NotificationConfig(host=host, port=port, token=token)
            runtime = NotificationRuntime(NotificationClient(config, "integration-runtime"), bind=factory.kw["bind"].sync_engine)
            inboxes = {
                "primary": runtime.subscribe(_TOPICS),
                "session_copy": runtime.subscribe(("session.reply",)),
            }
            runtime.start()
            await asyncio.wait_for(runtime.wait_ready(), timeout=_WAIT_SECONDS)
            for inbox in inboxes.values():
                batch = inbox.take()
                assert batch.full_scan
                assert not batch.notifications

            yield SimpleNamespace(
                factory=factory,
                runtime=runtime,
                inboxes=inboxes,
                config=config,
                server=server,
                servers=servers,
            )
        finally:
            try:
                if runtime is not None:
                    await asyncio.wait_for(runtime.stop(), timeout=_WAIT_SECONDS * 2)
            finally:
                await asyncio.wait_for(
                    asyncio.gather(
                        *(managed_server.stop() for managed_server in reversed(servers)),
                        return_exceptions=True,
                    ),
                    timeout=_WAIT_SECONDS * 2,
                )


def _notify_setting(
    session: AsyncSession,
    setting: SystemSetting,
    topic: str,
    event: str,
) -> None:
    assert setting.id is not None
    notify_after_commit(
        session,
        topic,
        event=event,
        resource_type="system_setting",
        resource_id=setting.id,
    )


async def _collect_notifications(
    inbox: NotificationInbox,
    expected_count: int,
) -> list[Notification]:
    deadline = asyncio.get_running_loop().time() + _WAIT_SECONDS
    notifications: list[Notification] = []
    while len(notifications) < expected_count:
        remaining = deadline - asyncio.get_running_loop().time()
        assert remaining > 0, "timed out waiting for notification delivery"
        assert await inbox.wait(remaining), "timed out waiting for notification delivery"
        batch = inbox.take()
        assert not batch.full_scan
        notifications.extend(batch.notifications)
    assert len(notifications) == expected_count
    return notifications


async def _assert_quiet(inbox: NotificationInbox, runtime: NotificationRuntime) -> None:
    await asyncio.wait_for(runtime.flush(), timeout=_WAIT_SECONDS * 2)
    assert not await inbox.wait(_QUIET_SECONDS)


async def _take_full_scan(inbox: NotificationInbox) -> None:
    assert await inbox.wait(_WAIT_SECONDS), "timed out waiting for runtime reconciliation wake"
    batch = inbox.take()
    assert batch.full_scan
    assert not batch.notifications


async def _read_values(factory, keys: tuple[str, ...]) -> dict[str, str]:
    async with factory() as reader:
        result = await reader.execute(select(SystemSetting).where(SystemSetting.key.in_(keys)))
        return {setting.key: setting.value for setting in result.scalars()}


def _resource_events(notifications: list[Notification]) -> set[tuple[str, str]]:
    assert all(notification.resource is not None for notification in notifications)
    return {(notification.resource.id, notification.event) for notification in notifications if notification.resource is not None}


@pytest.mark.asyncio
async def test_notifications_wait_for_commit_after_flush_and_savepoint(notification_context):
    context = notification_context
    async with context.factory() as writer:
        await ensure_sqlite_outer_transaction(writer)
        outer = SystemSetting(key="outer-setting", value="outer-committed")
        writer.add(outer)
        await writer.flush()
        _notify_setting(writer, outer, "session.reply", "outer_created")
        await _assert_quiet(context.inboxes["primary"], context.runtime)

        nested = SystemSetting(key="nested-setting", value="nested-committed")
        async with writer.begin_nested():
            writer.add(nested)
            await writer.flush()
            _notify_setting(writer, nested, "session.reply", "nested_created")
        await _assert_quiet(context.inboxes["primary"], context.runtime)

        await writer.commit()

    notifications = await _collect_notifications(context.inboxes["primary"], 2)
    assert {notification.topic for notification in notifications} == {"session.reply"}
    assert _resource_events(notifications) == {
        (str(outer.id), "outer_created"),
        (str(nested.id), "nested_created"),
    }
    assert await _read_values(
        context.factory,
        ("outer-setting", "nested-setting"),
    ) == {
        "outer-setting": "outer-committed",
        "nested-setting": "nested-committed",
    }
    await _assert_quiet(context.inboxes["primary"], context.runtime)


@pytest.mark.asyncio
async def test_root_rollback_then_next_commit_notifies(notification_context):
    context = notification_context
    async with context.factory() as writer:
        rolled_back = SystemSetting(key="rolled-back", value="discarded")
        writer.add(rolled_back)
        await writer.flush()
        _notify_setting(writer, rolled_back, "task.background", "discarded")
        await writer.rollback()
        await _assert_quiet(context.inboxes["primary"], context.runtime)

        committed = SystemSetting(key="committed-after-rollback", value="kept")
        writer.add(committed)
        await writer.flush()
        _notify_setting(writer, committed, "task.background", "committed")
        await writer.commit()

    notifications = await _collect_notifications(context.inboxes["primary"], 1)
    assert notifications[0].topic == "task.background"
    assert _resource_events(notifications) == {(str(committed.id), "committed")}
    assert await _read_values(
        context.factory,
        ("rolled-back", "committed-after-rollback"),
    ) == {"committed-after-rollback": "kept"}
    await _assert_quiet(context.inboxes["primary"], context.runtime)


@pytest.mark.parametrize("rollback_outer", (False, True), ids=("nested-rollback", "outer-rollback"))
@pytest.mark.asyncio
async def test_savepoint_notification_follows_transaction(notification_context, rollback_outer: bool):
    context = notification_context
    async with context.factory() as writer:
        await ensure_sqlite_outer_transaction(writer)
        parent = SystemSetting(key="parent-setting", value="parent")
        writer.add(parent)
        await writer.flush()
        _notify_setting(writer, parent, "memory.job", "parent_created")

        child = SystemSetting(key="child-setting", value="child")
        nested = await writer.begin_nested()
        writer.add(child)
        await writer.flush()
        _notify_setting(writer, child, "memory.job", "child_created")

        if rollback_outer:
            await nested.commit()
            await writer.rollback()
        else:
            await nested.rollback()
            await writer.commit()

    if rollback_outer:
        await _assert_quiet(context.inboxes["primary"], context.runtime)
        assert await _read_values(context.factory, ("parent-setting", "child-setting")) == {}
    else:
        notifications = await _collect_notifications(context.inboxes["primary"], 1)
        assert notifications[0].topic == "memory.job"
        assert _resource_events(notifications) == {(str(parent.id), "parent_created")}
        assert await _read_values(context.factory, ("parent-setting", "child-setting")) == {"parent-setting": "parent"}
        await _assert_quiet(context.inboxes["primary"], context.runtime)


@pytest.mark.asyncio
async def test_transaction_coalesces_and_routes_topics(notification_context):
    context = notification_context
    async with context.factory() as writer:
        setting = SystemSetting(key="multi-topic-setting", value="persisted")
        writer.add(setting)
        await writer.flush()
        _notify_setting(writer, setting, "session.reply", "superseded")
        _notify_setting(writer, setting, "session.reply", "latest")
        _notify_setting(writer, setting, "task.background", "task_ready")
        _notify_setting(writer, setting, "memory.job", "memory_ready")
        await writer.commit()

    primary = await _collect_notifications(context.inboxes["primary"], 3)
    session_copy = await _collect_notifications(context.inboxes["session_copy"], 1)
    assert {(notification.topic, notification.event) for notification in primary} == {
        ("session.reply", "latest"),
        ("task.background", "task_ready"),
        ("memory.job", "memory_ready"),
    }
    assert all(notification.resource is not None for notification in primary)
    assert {notification.resource.id for notification in primary if notification.resource} == {str(setting.id)}
    session_notification = next(notification for notification in primary if notification.topic == "session.reply")
    assert session_copy == [session_notification]
    assert await _read_values(context.factory, ("multi-topic-setting",)) == {"multi-topic-setting": "persisted"}
    await _assert_quiet(context.inboxes["primary"], context.runtime)
    await _assert_quiet(context.inboxes["session_copy"], context.runtime)


@pytest.mark.asyncio
async def test_publish_failure_reconnect_wakes_full_scan(notification_context, monkeypatch: pytest.MonkeyPatch):
    context = notification_context
    monkeypatch.setattr(notification_client_module, "NOTIFICATION_RECONNECT_MIN_SECONDS", 0.02)
    monkeypatch.setattr(notification_client_module, "NOTIFICATION_RECONNECT_MAX_SECONDS", 0.05)

    await asyncio.wait_for(context.server.stop(), timeout=_WAIT_SECONDS * 2)
    async with context.factory() as writer:
        setting = SystemSetting(key="offline-commit", value="durable")
        writer.add(setting)
        await writer.flush()
        _notify_setting(writer, setting, "session.reply", "offline_created")
        await writer.commit()

    await asyncio.wait_for(context.runtime.flush(), timeout=_WAIT_SECONDS * 2)
    assert await _read_values(context.factory, ("offline-commit",)) == {"offline-commit": "durable"}
    assert not await context.inboxes["primary"].wait(_QUIET_SECONDS)

    restored_server = NotificationServer(context.config)
    context.servers.append(restored_server)
    await asyncio.wait_for(restored_server.start(), timeout=_WAIT_SECONDS)
    for inbox in context.inboxes.values():
        await _take_full_scan(inbox)
    assert await _read_values(context.factory, ("offline-commit",)) == {"offline-commit": "durable"}


@pytest.mark.asyncio
async def test_stopped_runtime_restarts_subscriptions_once(notification_context):
    context = notification_context
    await asyncio.wait_for(context.runtime.stop(), timeout=_WAIT_SECONDS * 2)

    async with context.factory() as writer:
        stopped = SystemSetting(key="while-stopped", value="durable")
        writer.add(stopped)
        await writer.flush()
        _notify_setting(writer, stopped, "session.reply", "while_stopped")
        await writer.commit()

    await asyncio.wait_for(context.runtime.flush(), timeout=_WAIT_SECONDS)
    assert not await context.inboxes["primary"].wait(_QUIET_SECONDS)

    context.runtime.start()
    context.runtime.start()
    await asyncio.wait_for(context.runtime.wait_ready(), timeout=_WAIT_SECONDS)
    for inbox in context.inboxes.values():
        await _take_full_scan(inbox)

    async with context.factory() as writer:
        restarted = SystemSetting(key="after-restart", value="delivered")
        writer.add(restarted)
        await writer.flush()
        _notify_setting(writer, restarted, "session.reply", "after_restart")
        await writer.commit()

    await asyncio.wait_for(context.runtime.flush(), timeout=_WAIT_SECONDS * 2)
    primary = await _collect_notifications(context.inboxes["primary"], 1)
    session_copy = await _collect_notifications(context.inboxes["session_copy"], 1)
    assert _resource_events(primary) == {(str(restarted.id), "after_restart")}
    assert session_copy == primary
    assert await _read_values(context.factory, ("while-stopped", "after-restart")) == {
        "while-stopped": "durable",
        "after-restart": "delivered",
    }
    await _assert_quiet(context.inboxes["primary"], context.runtime)
    await _assert_quiet(context.inboxes["session_copy"], context.runtime)
