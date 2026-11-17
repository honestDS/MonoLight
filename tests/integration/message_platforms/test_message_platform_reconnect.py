import asyncio
from types import SimpleNamespace

import aiohttp
import httpx
import pytest
from fastapi import FastAPI

import app.core.crypto as crypto_module
import app.core.message_platforms.manager as manager_module
import app.core.message_platforms.weixin_openclaw as weixin_module
from app.adapters.weixin_openclaw.adapter import WeixinOpenClawAdapter
from app.api.v1 import message_platforms
from app.core.constants import ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.message_platforms.manager import MessagePlatformPollingManager
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.message_platform import MessagePlatform, MessagePlatformStatus, MessagePlatformType
from app.providers.database import get_db


async def _seed_platform(factory, status: MessagePlatformStatus) -> int:
    async with factory() as db:
        platform = MessagePlatform(
            name="reconnect-test",
            platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
            is_enabled=True,
            status=status,
            uid="owner",
            config={"token": "token", "poll_interval_ms": 0},
            state={
                "sync_buf": "saved-sync",
                "context_tokens": {"external-user": "saved-context"},
            },
            last_error="previous-error" if status == MessagePlatformStatus.ERROR else None,
        )
        db.add(platform)
        await db.commit()
        assert platform.id is not None
        return platform.id


@pytest.mark.parametrize(
    "initial_status",
    [MessagePlatformStatus.CONNECTED, MessagePlatformStatus.ERROR],
)
@pytest.mark.asyncio
async def test_weixin_openclaw_polling_recovers_after_network_failures(
    database_factory,
    monkeypatch,
    initial_status: MessagePlatformStatus,
):
    async with database_factory(wal=True) as factory:
        monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
        monkeypatch.setattr(weixin_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(manager_module, "AsyncSessionLocal", factory)

        platform_id = await _seed_platform(factory, initial_status)
        manager = MessagePlatformPollingManager()
        failures = [
            TimeoutError(),
            aiohttp.ConnectionTimeoutError("connect timeout"),
            ConnectionError("offline-1"),
            ConnectionError("offline-2"),
            aiohttp.SocketTimeoutError("idle long poll"),
        ]
        failure_order = []
        requests = []
        waits = []
        expected_wait_errors = iter(["TimeoutError", "connect timeout", "offline-1", "offline-2", "offline-2"])
        recovered = asyncio.Event()

        async def controlled_sleep(seconds: float) -> None:
            assert seconds == 30
            waits.append(seconds)
            async with factory() as db:
                platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
                assert platform is not None
                assert platform.status == MessagePlatformStatus.ERROR
                assert platform.last_error == next(expected_wait_errors)

            await manager.reload()
            await asyncio.sleep(0)

        monkeypatch.setattr(
            weixin_module,
            "asyncio",
            SimpleNamespace(CancelledError=asyncio.CancelledError, sleep=controlled_sleep),
        )

        async def request_json(adapter, method, endpoint, *, payload=None, **kwargs):
            assert method == "POST"
            assert endpoint == "ilink/bot/getupdates"
            requests.append(payload)
            if failures:
                error = failures.pop(0)
                failure_order.append((type(error), str(error)))
                raise error

            if len(requests) == 6:
                return {"ret": 0, "get_updates_buf": "saved-sync", "msgs": []}

            assert len(requests) == 7
            async with factory() as db:
                platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
                assert platform is not None
                assert platform.status == MessagePlatformStatus.CONNECTED
                assert platform.last_error == ""
                assert platform.state["sync_buf"] == "saved-sync"
                assert platform.state["context_tokens"] == {"external-user": "saved-context"}
            recovered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(WeixinOpenClawAdapter, "request_json", request_json)

        try:
            await manager.reload()
            await asyncio.wait_for(recovered.wait(), timeout=5)

            assert failure_order == [
                (TimeoutError, ""),
                (aiohttp.ConnectionTimeoutError, "connect timeout"),
                (ConnectionError, "offline-1"),
                (ConnectionError, "offline-2"),
                (aiohttp.SocketTimeoutError, "idle long poll"),
            ]
            assert len(requests) == 7
            assert [request["get_updates_buf"] for request in requests] == ["saved-sync"] * 7
            assert waits == [30] * 5
        finally:
            await manager.stop()


@pytest.mark.parametrize(
    "initial_status",
    [MessagePlatformStatus.CONNECTED, MessagePlatformStatus.ERROR],
)
@pytest.mark.asyncio
async def test_weixin_openclaw_socket_timeout_preserves_status_until_success(
    database_factory,
    monkeypatch,
    initial_status: MessagePlatformStatus,
):
    async with database_factory(wal=True) as factory:
        monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
        monkeypatch.setattr(weixin_module, "AsyncSessionLocal", factory)
        monkeypatch.setattr(manager_module, "AsyncSessionLocal", factory)

        platform_id = await _seed_platform(factory, initial_status)
        manager = MessagePlatformPollingManager()
        requests = []
        waits = []
        recovered = asyncio.Event()

        async def controlled_sleep(seconds: float) -> None:
            assert seconds == 30
            waits.append(seconds)
            async with factory() as db:
                platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
                assert platform is not None
                assert platform.status == MessagePlatformStatus.ERROR
                assert platform.last_error == "previous-error"
            await asyncio.sleep(0)

        monkeypatch.setattr(
            weixin_module,
            "asyncio",
            SimpleNamespace(CancelledError=asyncio.CancelledError, sleep=controlled_sleep),
        )

        async def request_json(adapter, method, endpoint, *, payload=None, **kwargs):
            assert method == "POST"
            assert endpoint == "ilink/bot/getupdates"
            requests.append(payload)
            assert payload["get_updates_buf"] == "saved-sync"

            if len(requests) == 1:
                raise aiohttp.SocketTimeoutError("idle long poll")

            if len(requests) == 2:
                async with factory() as db:
                    platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
                    assert platform is not None
                    assert platform.status == initial_status
                    assert platform.last_error == ("previous-error" if initial_status == MessagePlatformStatus.ERROR else None)
                return {"ret": 0, "get_updates_buf": "saved-sync", "msgs": []}

            assert len(requests) == 3
            async with factory() as db:
                platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
                assert platform is not None
                assert platform.status == MessagePlatformStatus.CONNECTED
                assert platform.last_error == ("" if initial_status == MessagePlatformStatus.ERROR else None)
                assert platform.state["sync_buf"] == "saved-sync"
            recovered.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(WeixinOpenClawAdapter, "request_json", request_json)

        try:
            await manager.reload()
            await asyncio.wait_for(recovered.wait(), timeout=5)
        finally:
            await manager.stop()

        assert len(requests) == 3
        assert waits == ([30] if initial_status == MessagePlatformStatus.ERROR else [])


@pytest.mark.parametrize("outcome", ["success", "failure"])
@pytest.mark.parametrize(
    "change",
    [
        "disable",
        "waiting-login",
        "disconnected",
        "deleted",
        "token-removed",
        "token-changed",
        "owner-changed",
    ],
)
@pytest.mark.asyncio
async def test_weixin_openclaw_polling_preserves_concurrent_platform_changes(
    database_factory,
    monkeypatch,
    outcome: str,
    change: str,
):
    async with database_factory(wal=True) as factory:
        monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
        monkeypatch.setattr(weixin_module, "AsyncSessionLocal", factory)
        platform_id = await _seed_platform(factory, MessagePlatformStatus.ERROR)
        waits = []
        requests = []
        snapshots = []

        async def controlled_sleep(seconds: float) -> None:
            assert seconds == 30
            waits.append(seconds)
            await asyncio.sleep(0)

        monkeypatch.setattr(
            weixin_module,
            "asyncio",
            SimpleNamespace(CancelledError=asyncio.CancelledError, sleep=controlled_sleep),
        )

        async def change_platform():
            async with factory() as db:
                platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
                assert platform is not None

                if change == "disable":
                    platform.is_enabled = False
                elif change == "waiting-login":
                    platform.status = MessagePlatformStatus.WAITING_LOGIN
                elif change == "disconnected":
                    platform.status = MessagePlatformStatus.DISCONNECTED
                elif change == "deleted":
                    await db.delete(platform)
                    await db.commit()
                    snapshots.append(None)
                    return
                elif change == "token-removed":
                    platform.config = {**platform.config, "token": ""}
                elif change == "token-changed":
                    platform.config = {**platform.config, "token": "new-token"}
                else:
                    assert change == "owner-changed"
                    platform.uid = "new-owner"

                await db.commit()
                await db.refresh(platform)
                snapshots.append(platform.model_dump())

        async def request_json(adapter, method, endpoint, *, payload=None, **kwargs):
            assert method == "POST"
            assert endpoint == "ilink/bot/getupdates"
            assert payload["get_updates_buf"] == "saved-sync"
            requests.append(payload)

            if len(requests) == 1:
                assert adapter.config.token == "token"
                assert adapter.config.owner_uid == "owner"
                await change_platform()
                if outcome == "success":
                    return {"ret": 0, "get_updates_buf": "stale-sync", "msgs": []}
                raise ConnectionError("stale-failure")

            assert change in {"token-changed", "owner-changed"}
            assert len(requests) == 2
            assert adapter.config.token == ("new-token" if change == "token-changed" else "token")
            assert adapter.config.owner_uid == ("new-owner" if change == "owner-changed" else "owner")
            raise asyncio.CancelledError

        monkeypatch.setattr(WeixinOpenClawAdapter, "request_json", request_json)
        handler = weixin_module.WeixinOpenClawPlatformHandler()
        needs_second_request = change in {"token-changed", "owner-changed"}

        if needs_second_request:
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(handler.run(platform_id), timeout=5)
        else:
            await asyncio.wait_for(handler.run(platform_id), timeout=5)

        assert len(requests) == (2 if needs_second_request else 1)
        assert waits == ([30] if outcome == "failure" else [])

        async with factory() as db:
            platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)

        snapshot = snapshots[0]
        if snapshot is None:
            assert change == "deleted"
            assert platform is None
        else:
            assert platform is not None
            for field in (
                "id",
                "name",
                "platform_type",
                "is_enabled",
                "status",
                "uid",
                "config",
                "state",
                "last_error",
            ):
                assert getattr(platform, field) == snapshot[field]
            assert platform.state["sync_buf"] == "saved-sync"
            assert platform.state["context_tokens"] == {"external-user": "saved-context"}
            assert platform.last_error == "previous-error"

            if change == "disable":
                assert platform.is_enabled is False
            elif change == "waiting-login":
                assert platform.status == MessagePlatformStatus.WAITING_LOGIN
            elif change == "disconnected":
                assert platform.status == MessagePlatformStatus.DISCONNECTED
            elif change == "token-removed":
                assert platform.get_config_secret("token") == ""
            elif change == "token-changed":
                assert platform.get_config_secret("token") == "new-token"
            elif change == "owner-changed":
                assert platform.uid == "new-owner"


@pytest.mark.parametrize("change", ["disable", "deleted", "waiting-login", "token-removed"])
@pytest.mark.asyncio
async def test_weixin_openclaw_polling_stops_after_platform_change_during_retry_wait(
    database_factory,
    monkeypatch,
    change: str,
):
    async with database_factory(wal=True) as factory:
        monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
        monkeypatch.setattr(weixin_module, "AsyncSessionLocal", factory)
        platform_id = await _seed_platform(factory, MessagePlatformStatus.CONNECTED)
        waits = []
        requests = []
        snapshots = []

        async def controlled_sleep(seconds: float) -> None:
            assert seconds == 30
            waits.append(seconds)
            async with factory() as db:
                platform = await message_platform_crud.get(db, platform_id)
                assert platform is not None
                assert platform.status == MessagePlatformStatus.ERROR
                assert platform.last_error == "offline"

                if change == "disable":
                    platform.is_enabled = False
                elif change == "deleted":
                    await db.delete(platform)
                    await db.commit()
                    snapshots.append(None)
                    await asyncio.sleep(0)
                    return
                elif change == "waiting-login":
                    platform.status = MessagePlatformStatus.WAITING_LOGIN
                else:
                    assert change == "token-removed"
                    platform.config = {**platform.config, "token": ""}

                await db.commit()
                await db.refresh(platform)
                snapshots.append(platform.model_dump())
            await asyncio.sleep(0)

        monkeypatch.setattr(
            weixin_module,
            "asyncio",
            SimpleNamespace(CancelledError=asyncio.CancelledError, sleep=controlled_sleep),
        )

        async def request_json(adapter, method, endpoint, *, payload=None, **kwargs):
            assert method == "POST"
            assert endpoint == "ilink/bot/getupdates"
            assert payload["get_updates_buf"] == "saved-sync"
            requests.append(payload)
            raise ConnectionError("offline")

        monkeypatch.setattr(WeixinOpenClawAdapter, "request_json", request_json)
        handler = weixin_module.WeixinOpenClawPlatformHandler()
        await asyncio.wait_for(handler.run(platform_id), timeout=5)

        assert len(requests) == 1
        assert waits == [30]

        async with factory() as db:
            platform = await message_platform_crud.get(db, platform_id)

        snapshot = snapshots[0]
        if snapshot is None:
            assert change == "deleted"
            assert platform is None
        else:
            assert platform is not None
            for field in ("is_enabled", "status", "last_error", "state", "config"):
                assert getattr(platform, field) == snapshot[field]
            assert platform.state["sync_buf"] == "saved-sync"
            assert platform.state["context_tokens"] == {"external-user": "saved-context"}
            assert (
                platform.status
                == {
                    "disable": MessagePlatformStatus.ERROR,
                    "waiting-login": MessagePlatformStatus.WAITING_LOGIN,
                    "token-removed": MessagePlatformStatus.ERROR,
                }[change]
            )
            assert platform.last_error == "offline"
            assert platform.is_enabled is (change != "disable")
            if change == "token-removed":
                assert platform.get_config_secret("token") == ""


@pytest.mark.parametrize("legacy_error", [False, True])
@pytest.mark.asyncio
async def test_expired_weixin_qrcode_requires_explicit_recovery(
    database_factory,
    monkeypatch,
    legacy_error: bool,
):
    async with database_factory(wal=True) as factory:
        monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
        monkeypatch.setattr(weixin_module, "AsyncSessionLocal", factory)
        platform_id = await _seed_platform(factory, MessagePlatformStatus.CONNECTED)

        app = FastAPI()
        register_handlers(app)
        app.include_router(message_platforms.router, prefix="/api/v1")

        async def override_get_db():
            async with factory() as db:
                yield db

        async def override_current_user():
            return SimpleNamespace(uid="owner", is_superuser=False)

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[get_current_user] = override_current_user

        qrcode_status_params = []
        update_requests = []
        polling_authorized = False

        async def request_json(adapter, method, endpoint, *, params=None, payload=None, **kwargs):
            nonlocal polling_authorized
            if endpoint == "ilink/bot/get_bot_qrcode":
                assert method == "GET"
                assert kwargs["token_required"] is False
                return {
                    "ret": 0,
                    "qrcode": "new-qrcode",
                    "qrcode_img_content": "new-qrcode-image",
                }

            if endpoint == "ilink/bot/get_qrcode_status":
                assert method == "GET"
                assert kwargs["token_required"] is False
                qrcode_status_params.append(params)
                return {"ret": 0, "status": "expired"}

            assert method == "POST"
            assert endpoint == "ilink/bot/getupdates"
            update_requests.append((adapter.config.token, payload))
            if not polling_authorized:
                raise asyncio.CancelledError

            assert adapter.config.token == "token"
            assert payload["get_updates_buf"] == "saved-sync"
            if len(update_requests) == 1:
                return {"ret": 0, "get_updates_buf": "saved-sync", "msgs": []}

            assert len(update_requests) == 2
            raise asyncio.CancelledError

        monkeypatch.setattr(WeixinOpenClawAdapter, "request_json", request_json)
        handler = weixin_module.WeixinOpenClawPlatformHandler()

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            start_response = await client.post(f"/api/v1/message-platforms/{platform_id}/weixin-openclaw/login/start")
            assert start_response.status_code == 200
            start_data = start_response.json()["data"]
            assert start_data["qrcode"] == "new-qrcode"
            assert start_data["qrcode_img_content"] == "new-qrcode-image"
            assert start_data["status"] == MessagePlatformStatus.WAITING_LOGIN.value

            status_response = await client.get(f"/api/v1/message-platforms/{platform_id}/weixin-openclaw/login/status")
            assert status_response.status_code == 200
            status_data = status_response.json()["data"]
            assert status_data["qrcode_status"] == "expired"
            assert status_data["status"] == MessagePlatformStatus.DISCONNECTED.value

            async with factory() as db:
                platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
                assert platform is not None
                assert platform.status == MessagePlatformStatus.DISCONNECTED
                assert platform.get_config_secret("token") == "token"
                assert platform.state["qrcode"] == ""
                assert platform.state["qrcode_img_content"] == ""
                assert platform.state["qr_error"] == ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED
                assert platform.state["sync_buf"] == "saved-sync"
                assert platform.state["context_tokens"] == {"external-user": "saved-context"}
                assert platform.last_error == ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED

            assert qrcode_status_params == [{"qrcode": "new-qrcode"}]

            stopped_status = MessagePlatformStatus.DISCONNECTED
            if legacy_error:
                async with factory() as db:
                    platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
                    assert platform is not None
                    platform.status = MessagePlatformStatus.ERROR
                    await db.commit()
                stopped_status = MessagePlatformStatus.ERROR

            await asyncio.wait_for(handler.run(platform_id), timeout=5)
            assert update_requests == []

            async with factory() as db:
                platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
                assert platform is not None
                assert platform.status == stopped_status
                assert platform.last_error == ERR_MESSAGE_PLATFORM_QRCODE_EXPIRED
                assert platform.get_config_secret("token") == "token"
                assert platform.state["sync_buf"] == "saved-sync"
                assert platform.state["context_tokens"] == {"external-user": "saved-context"}

            recover_response = await client.post(
                "/api/v1/message-platforms/recover",
                params={"platform_id": platform_id},
            )
            assert recover_response.status_code == 200
            recover_data = recover_response.json()["data"]
            assert recover_data["status"] == MessagePlatformStatus.CONNECTED.value
            polling_authorized = True

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(handler.run(platform_id), timeout=5)

        assert len(update_requests) == 2
        assert [token for token, _ in update_requests] == ["token", "token"]
        assert [payload["get_updates_buf"] for _, payload in update_requests] == ["saved-sync", "saved-sync"]

        async with factory() as db:
            platform = await message_platform_crud.get_for_update(db, platform_id=platform_id)
            assert platform is not None
            assert platform.status == MessagePlatformStatus.CONNECTED
            assert platform.get_config_secret("token") == "token"
            assert platform.state["sync_buf"] == "saved-sync"
            assert platform.state["context_tokens"] == {"external-user": "saved-context"}
