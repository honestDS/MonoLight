import asyncio
from secrets import token_hex
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI

import app.core.crypto as crypto_module
import app.core.message_platforms.manager as manager_module
import app.models  # noqa: F401
from app.api.v1.message_platforms import router
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.manager import MessagePlatformPollingManager
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig
from app.core.notifications.protocol import Notification, NotificationTopic
from app.core.notifications.runtime import NotificationRuntime
from app.core.notifications.server import NotificationServer
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.message_platform import (
    MessagePlatform,
    MessagePlatformCreate,
    MessagePlatformStatus,
    MessagePlatformType,
    MessagePlatformUpdate,
)
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.user import User
from app.providers.database import get_db

_UID = "message-platform-notification-owner"
_PROFILE_ID = 1
_SESSION_ID = "message-platform-notification-session"
_WAIT_SECONDS = 5.0
_QUIET_SECONDS = 0.05


class RecordingHandler(MessagePlatformHandler):
    platform_type = MessagePlatformType.WEIXIN_OPENCLAW
    sources = frozenset()

    def __init__(self, factory) -> None:
        self.factory = factory
        self.started: asyncio.Queue[tuple[int, Any]] = asyncio.Queue()
        self.stopped: asyncio.Queue[tuple[int, Any]] = asyncio.Queue()
        self.active: dict[int, Any] = {}

    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        return bool(platform is not None and platform.is_enabled and platform.status in (MessagePlatformStatus.CONNECTED, MessagePlatformStatus.ERROR))

    async def run(self, platform_id: int) -> None:
        async with self.factory() as db:
            platform = await db.get(MessagePlatform, platform_id)
            assert platform is not None
            version = platform.config["version"]

        self.active[platform_id] = version
        self.started.put_nowait((platform_id, version))
        try:
            await asyncio.Event().wait()
        finally:
            self.active.pop(platform_id, None)
            self.stopped.put_nowait((platform_id, version))

    async def send_session_event(
        self,
        uid: str,
        session_id: str,
        source: str,
        event: dict[str, Any],
    ) -> bool:
        return True


@pytest_asyncio.fixture
async def message_platform_notifications(database_factory, monkeypatch: pytest.MonkeyPatch):
    async with database_factory(foreign_keys=True, wal=True) as factory:
        monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
        monkeypatch.setattr(manager_module, "AsyncSessionLocal", factory)

        async with factory() as db:
            db.add(User(uid=_UID, username="message-platform-notification-owner"))
            await db.flush()
            db.add(Profile(id=_PROFILE_ID, uid=_UID, name="notification-profile", configs={}))
            await db.flush()
            db.add(ChatSession(session_id=_SESSION_ID, uid=_UID, profile_id=_PROFILE_ID))
            await db.commit()

        token = token_hex(32)
        server = NotificationServer(NotificationConfig(host="127.0.0.1", port=0, token=token))
        runtime = None
        manager = None
        try:
            await asyncio.wait_for(server.start(), timeout=_WAIT_SECONDS)
            host, port = server.address
            config = NotificationConfig(host=host, port=port, token=token)
            runtime = NotificationRuntime(
                NotificationClient(config, f"message-platform-test:{token_hex(8)}"),
                bind=factory.kw["bind"].sync_engine,
            )
            config_inbox = runtime.subscribe((NotificationTopic.MESSAGE_PLATFORM,))
            observer_inbox = runtime.subscribe((NotificationTopic.MESSAGE_PLATFORM,))
            runtime.start()
            await asyncio.wait_for(runtime.wait_ready(), timeout=_WAIT_SECONDS)
            for inbox in (config_inbox, observer_inbox):
                assert inbox.take().full_scan

            handler = RecordingHandler(factory)
            manager = MessagePlatformPollingManager((handler,))
            reload_completions: asyncio.Queue[None] = asyncio.Queue()
            original_reload = manager.reload

            async def track_reload(*, platform_ids=None, restart_ids=None) -> None:
                await original_reload(platform_ids=platform_ids, restart_ids=restart_ids)
                reload_completions.put_nowait(None)

            manager.reload = track_reload
            manager.start(config_inbox=config_inbox)
            await _wait_for_reload(reload_completions)

            yield SimpleNamespace(
                factory=factory,
                runtime=runtime,
                config_inbox=config_inbox,
                observer_inbox=observer_inbox,
                handler=handler,
                manager=manager,
                reload_completions=reload_completions,
            )
        finally:
            try:
                if manager is not None:
                    await asyncio.wait_for(manager.stop(), timeout=_WAIT_SECONDS * 2)
            finally:
                try:
                    if runtime is not None:
                        await asyncio.wait_for(runtime.stop(), timeout=_WAIT_SECONDS * 2)
                finally:
                    await asyncio.wait_for(server.stop(), timeout=_WAIT_SECONDS * 2)


def _platform_config(version: int) -> dict[str, Any]:
    return {"token": "notification-test-token", "version": version}


def _config_update_for_version(platform: MessagePlatform, version: int) -> MessagePlatformUpdate:
    config = dict(platform.config or {})
    config["version"] = version
    return MessagePlatformUpdate(config=config)


async def _wait_for_reload(reload_completions: asyncio.Queue[None]) -> None:
    async with asyncio.timeout(_WAIT_SECONDS):
        await reload_completions.get()


async def _create_platform(factory, *, name: str, version: int = 1) -> int:
    async with factory() as db:
        platform = await message_platform_crud.create(
            db,
            obj_in=MessagePlatformCreate(
                name=name,
                uid=_UID,
                profile_id=_PROFILE_ID,
                is_enabled=True,
                config=_platform_config(version),
            ),
            update_dict={"status": MessagePlatformStatus.CONNECTED},
            commit=True,
        )
        assert platform.id is not None
        return platform.id


async def _wait_for_notifications(
    inbox,
    *,
    platform_id: int,
    event: str | None = None,
) -> list[Notification]:
    async with asyncio.timeout(_WAIT_SECONDS):
        while True:
            assert await inbox.wait(_WAIT_SECONDS), "timed out waiting for committed platform notification"
            batch = inbox.take()
            if not batch.notifications:
                continue
            assert not batch.full_scan
            notifications = list(batch.notifications)
            assert len(notifications) == 1
            notification = notifications[0]
            assert notification.topic == NotificationTopic.MESSAGE_PLATFORM
            assert notification.resource is not None
            assert notification.resource.type == "message_platform"
            assert notification.resource.id == str(platform_id)
            if event is not None:
                assert notification.event == event
            return notifications


async def _expect_notification(context, platform_id: int, *, event: str | None = None) -> None:
    await _wait_for_notifications(context.observer_inbox, platform_id=platform_id, event=event)


async def _next_handler_event(queue: asyncio.Queue[tuple[int, Any]]) -> tuple[int, Any]:
    async with asyncio.timeout(_WAIT_SECONDS):
        return await queue.get()


async def _create_and_start(context, *, name: str) -> int:
    platform_id = await _create_platform(context.factory, name=name)
    await _expect_notification(context, platform_id)
    assert await _next_handler_event(context.handler.started) == (platform_id, 1)
    return platform_id


async def _assert_no_event(queue: asyncio.Queue[tuple[int, Any]]) -> None:
    try:
        await asyncio.wait_for(queue.get(), timeout=_QUIET_SECONDS)
    except TimeoutError:
        return
    raise AssertionError("unexpected message-platform handler lifecycle event")


async def _assert_handler_state(context, expected_active: dict[int, Any]) -> None:
    assert context.handler.active == expected_active
    await _assert_no_event(context.handler.started)
    await _assert_no_event(context.handler.stopped)


async def _assert_notifications_quiet(context) -> None:
    await asyncio.wait_for(context.runtime.flush(), timeout=_WAIT_SECONDS)
    assert not await context.observer_inbox.wait(_QUIET_SECONDS)


@pytest.mark.asyncio
async def test_crud_notifications_start_and_restart_only_the_changed_platform(message_platform_notifications) -> None:
    context = message_platform_notifications
    first_id = await _create_and_start(context, name="notification-first")
    second_id = await _create_and_start(context, name="notification-second")

    async with context.factory() as db:
        first = await message_platform_crud.get(db, first_id)
        assert first is not None
        await message_platform_crud.update(
            db,
            db_obj=first,
            obj_in=_config_update_for_version(first, 2),
            commit=True,
        )

    await _expect_notification(context, first_id)
    assert await _next_handler_event(context.handler.stopped) == (first_id, 1)
    assert await _next_handler_event(context.handler.started) == (first_id, 2)
    await _assert_handler_state(context, {first_id: 2, second_id: 1})


@pytest.mark.parametrize("operation", ("disable", "delete"))
@pytest.mark.asyncio
async def test_crud_disable_or_delete_stops_only_the_target_platform(message_platform_notifications, operation: str) -> None:
    context = message_platform_notifications
    first_id = await _create_and_start(context, name=f"notification-{operation}-first")
    second_id = await _create_and_start(context, name=f"notification-{operation}-second")

    async with context.factory() as db:
        if operation == "disable":
            first = await message_platform_crud.get(db, first_id)
            assert first is not None
            await message_platform_crud.update(
                db,
                db_obj=first,
                obj_in=MessagePlatformUpdate(is_enabled=False),
                commit=True,
            )
        else:
            await message_platform_crud.remove(db, id=first_id)

    await _expect_notification(context, first_id)
    assert await _next_handler_event(context.handler.stopped) == (first_id, 1)
    await _assert_handler_state(context, {second_id: 1})


@pytest.mark.asyncio
async def test_runtime_state_and_unchanged_connected_status_do_not_notify_or_restart(message_platform_notifications) -> None:
    context = message_platform_notifications
    platform_id = await _create_and_start(context, name="notification-runtime-state")

    async with context.factory() as db:
        platform = await message_platform_crud.get(db, platform_id)
        assert platform is not None
        await message_platform_crud.update_runtime_state(
            db,
            platform=platform,
            state={"sync_buf": "persisted-cursor", "context_tokens": {"external-user": "persisted-context"}},
        )

    async with context.factory() as reader:
        persisted = await reader.get(MessagePlatform, platform_id)
        assert persisted is not None
        assert persisted.state == {
            "sync_buf": "persisted-cursor",
            "context_tokens": {"external-user": "persisted-context"},
        }
    await _assert_notifications_quiet(context)
    await _assert_handler_state(context, {platform_id: 1})

    async with context.factory() as db:
        platform = await message_platform_crud.get(db, platform_id)
        assert platform is not None
        await message_platform_crud.update_runtime_state(
            db,
            platform=platform,
            status=MessagePlatformStatus.CONNECTED,
        )

    async with context.factory() as reader:
        persisted = await reader.get(MessagePlatform, platform_id)
        assert persisted is not None
        assert persisted.status == MessagePlatformStatus.CONNECTED
    await _assert_notifications_quiet(context)
    await _assert_handler_state(context, {platform_id: 1})


@pytest.mark.asyncio
async def test_uncommitted_crud_update_rollback_is_silent_then_commit_restarts_once(message_platform_notifications) -> None:
    context = message_platform_notifications
    platform_id = await _create_and_start(context, name="notification-rollback")

    async with context.factory() as db:
        platform = await message_platform_crud.get(db, platform_id)
        assert platform is not None
        await message_platform_crud.update(
            db,
            db_obj=platform,
            obj_in=_config_update_for_version(platform, 2),
            commit=False,
        )
        await db.rollback()

    await _assert_notifications_quiet(context)
    async with context.factory() as reader:
        persisted = await reader.get(MessagePlatform, platform_id)
        assert persisted is not None
        assert persisted.config["version"] == 1
    await _assert_handler_state(context, {platform_id: 1})

    async with context.factory() as db:
        platform = await message_platform_crud.get(db, platform_id)
        assert platform is not None
        await message_platform_crud.update(
            db,
            db_obj=platform,
            obj_in=_config_update_for_version(platform, 2),
            commit=True,
        )

    await _expect_notification(context, platform_id)
    assert await _next_handler_event(context.handler.stopped) == (platform_id, 1)
    assert await _next_handler_event(context.handler.started) == (platform_id, 2)
    await _assert_handler_state(context, {platform_id: 2})


@pytest.mark.asyncio
async def test_recover_api_emits_restart_and_restarts_connected_platform(message_platform_notifications) -> None:
    context = message_platform_notifications
    platform_id = await _create_and_start(context, name="notification-api-recover")
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        async with context.factory() as db:
            yield db

    async def override_current_user():
        return SimpleNamespace(uid=_UID, is_superuser=False)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post("/api/v1/message-platforms/recover", params={"platform_id": platform_id})

    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    assert payload["data"]["id"] == platform_id
    assert payload["data"]["status"] == MessagePlatformStatus.CONNECTED
    await _expect_notification(context, platform_id, event="restart")
    assert await _next_handler_event(context.handler.stopped) == (platform_id, 1)
    assert await _next_handler_event(context.handler.started) == (platform_id, 1)

    async with context.factory() as reader:
        persisted = await reader.get(MessagePlatform, platform_id)
        assert persisted is not None
        assert persisted.status == MessagePlatformStatus.CONNECTED
    await _assert_handler_state(context, {platform_id: 1})


@pytest.mark.asyncio
async def test_reconciliation_restarts_platform_after_unnotified_orm_config_change(message_platform_notifications, monkeypatch: pytest.MonkeyPatch) -> None:
    context = message_platform_notifications
    monkeypatch.setattr(manager_module, "NOTIFICATION_RECONCILE_INTERVAL_SECONDS", 0.1)

    async with context.factory() as db:
        platform = MessagePlatform(
            name="notification-reconciliation",
            platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
            uid=_UID,
            profile_id=_PROFILE_ID,
            is_enabled=True,
            status=MessagePlatformStatus.CONNECTED,
            config=_platform_config(1),
        )
        db.add(platform)
        await db.commit()
        assert platform.id is not None
        platform_id = platform.id

    context.config_inbox.wake()
    await _wait_for_reload(context.reload_completions)
    assert await _next_handler_event(context.handler.started) == (platform_id, 1)

    async with context.factory() as db:
        platform = await db.get(MessagePlatform, platform_id)
        assert platform is not None
        platform.config = _platform_config(2)
        await db.commit()

    assert await _next_handler_event(context.handler.stopped) == (platform_id, 1)
    assert await _next_handler_event(context.handler.started) == (platform_id, 2)
    await _assert_notifications_quiet(context)
    await _assert_handler_state(context, {platform_id: 2})


@pytest.mark.parametrize("notification_case", ("changed", "full_scan", "dropped"))
@pytest.mark.asyncio
async def test_manual_recover_survives_notification_merge_and_reconciliation(
    message_platform_notifications,
    monkeypatch: pytest.MonkeyPatch,
    notification_case: str,
) -> None:
    context = message_platform_notifications
    target_id = await _create_and_start(context, name="notification-recover-target")
    unrelated_id = await _create_and_start(context, name="notification-recover-unrelated")
    for _ in range(context.reload_completions.qsize()):
        context.reload_completions.get_nowait()

    monkeypatch.setattr(manager_module, "NOTIFICATION_RECONCILE_INTERVAL_SECONDS", 0.1)
    original_reload = context.manager.reload
    paused = asyncio.Event()
    release = asyncio.Event()

    async def _hold_reload(*, platform_ids=None, restart_ids=None) -> None:
        await original_reload(platform_ids=platform_ids, restart_ids=restart_ids)
        paused.set()
        await release.wait()

    monkeypatch.setattr(context.manager, "reload", _hold_reload)

    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        async with context.factory() as db:
            yield db

    async def override_current_user():
        return SimpleNamespace(uid=_UID, is_superuser=False)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_current_user

    try:
        context.config_inbox.wake()
        await asyncio.wait_for(paused.wait(), timeout=_WAIT_SECONDS)

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            recover_response = await client.post(
                "/api/v1/message-platforms/recover",
                params={"platform_id": target_id},
            )
            assert recover_response.status_code == 200
            recover_payload = recover_response.json()
            assert recover_payload["code"] == 200
            assert recover_payload["data"]["id"] == target_id
            await _expect_notification(context, target_id, event="restart")

            update_response = await client.post(
                "/api/v1/message-platforms/update",
                params={"platform_id": target_id},
                json={"name": "notification-recover-renamed"},
            )
            assert update_response.status_code == 200
            assert update_response.json()["code"] == 200
            await _expect_notification(context, target_id, event="changed")

        if notification_case == "full_scan":
            context.config_inbox.wake()
        elif notification_case == "dropped":
            context.config_inbox.take()
    finally:
        release.set()

    assert await _next_handler_event(context.handler.stopped) == (target_id, 1)
    assert await _next_handler_event(context.handler.started) == (target_id, 1)
    await _assert_handler_state(context, {target_id: 1, unrelated_id: 1})

    async with context.factory() as reader:
        persisted = await reader.get(MessagePlatform, target_id)
        assert persisted is not None
        assert persisted.name == "notification-recover-renamed"
