import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.log.broadcaster as broadcaster_module
import app.core.security as security
import app.tasks as tasks
from app.api.v1 import system
from app.core.constants import ERR_ONLY_ADMIN_ALLOWED, ERR_UNAUTHORIZED, MSG_LOG_LIST_SUCCESS, SYSTEM_LOG_REALTIME_GRACE_SECONDS
from app.core.i18n import t
from app.core.log.broadcaster import LogBroadcaster
from app.core.utils.time import get_local_time
from app.handler import register_handlers
from app.models.system_log import SystemLog
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.providers.database import get_db

TEST_JWT_SECRET = "system-log-permissions-integration-jwt-secret"
SETTINGS_PATH = "/api/v1/system/settings"
LOGS_PATH = "/api/v1/system/logs"
LOGS_WS_PATH = "/api/v1/system/logs/ws"


@pytest_asyncio.fixture
async def log_session_factory(
    database_factory,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        tables=[User.__table__, SystemSetting.__table__, SystemLog.__table__],
        wal=True,
        name="system-log-permissions.sqlite3",
    ) as session_factory:
        async with session_factory() as session:
            session.add_all(
                [
                    User(uid="admin", username="admin", is_active=True, is_superuser=True),
                    User(uid="user-a", username="user-a", is_active=True, is_superuser=False),
                    User(uid="user-b", username="user-b", is_active=True, is_superuser=False),
                    User(uid="disabled", username="disabled", is_active=False, is_superuser=True),
                    SystemLog(
                        level="INFO",
                        module="auth",
                        message="admin login",
                        uid="admin",
                        session_id="session-admin",
                        extra='{"source":"admin"}',
                        created_at=datetime(2026, 1, 1, 0, 0, 1, tzinfo=UTC),
                    ),
                    SystemLog(
                        level="WARNING",
                        module="chat",
                        message="user-a warning",
                        uid="user-a",
                        session_id="session-user-a",
                        extra='{"source":"user-a"}',
                        created_at=datetime(2026, 1, 1, 0, 0, 2, tzinfo=UTC),
                    ),
                    SystemLog(
                        level="ERROR",
                        module="files",
                        message="user-b error",
                        uid="user-b",
                        session_id="session-user-b",
                        extra='{"source":"user-b"}',
                        created_at=datetime(2026, 1, 1, 0, 0, 3, tzinfo=UTC),
                    ),
                ]
            )
            await session.commit()
        yield session_factory


@pytest_asyncio.fixture
async def api_app(
    log_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(security, "AsyncSessionLocal", log_session_factory)
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

    app = FastAPI()
    register_handlers(app)
    app.include_router(system.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with log_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app


@pytest_asyncio.fixture
async def log_broadcaster(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[LogBroadcaster]:
    broadcaster = LogBroadcaster()
    monkeypatch.setattr(system, "log_broadcaster", broadcaster)
    yield broadcaster
    await broadcaster.stop()


def _token(subject: str, *, expires_delta: timedelta | None = None, **claims: Any) -> str:
    return security.create_access_token(data={"sub": subject, **claims}, expires_delta=expires_delta)


def _credential_token(credential: dict[str, Any]) -> str | None:
    kind = credential["kind"]
    if kind == "missing":
        return None
    if kind == "invalid":
        return "not-a-valid-jwt"
    if kind == "expired":
        return _token(credential["subject"], expires_delta=timedelta(seconds=-1))
    return _token(credential["subject"], **credential.get("claims", {}))


def _assert_error(response: httpx.Response, code: int, message: str) -> None:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert payload["message"] == message
    assert payload["data"] is None


async def _set_log_settings(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    *,
    retention_days: int,
    max_entries: int,
) -> dict[str, Any]:
    response = await client.post(
        SETTINGS_PATH,
        headers=headers,
        json={"log_retention_days": retention_days, "log_max_entries": max_entries},
    )
    payload = response.json()
    assert response.status_code == 200
    assert payload["code"] == 200
    assert payload["data"]["log_retention_days"] == retention_days
    assert payload["data"]["log_max_entries"] == max_entries
    return payload["data"]


async def _get_log_page(client: httpx.AsyncClient, headers: dict[str, str]) -> dict[str, Any]:
    response = await client.get(LOGS_PATH, headers=headers, params={"page": "1", "size": "100"})
    payload = response.json()
    assert response.status_code == 200
    assert payload["code"] == 200
    assert payload["message"] == t(MSG_LOG_LIST_SUCCESS)
    return payload["data"]


async def _wait_for_log_page(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    expected_ids: set[int],
    *,
    timeout: float = 5,
) -> dict[str, Any]:
    async def poll() -> dict[str, Any]:
        while True:
            page = await _get_log_page(client, headers)
            item_ids = {item["id"] for item in page["items"]}
            if page["total"] == len(expected_ids) and item_ids == expected_ids:
                return page
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), timeout=timeout)


async def _read_log_ids(session_factory: async_sessionmaker[AsyncSession]) -> list[int]:
    async with session_factory() as session:
        result = await session.execute(select(SystemLog.id).order_by(SystemLog.id))
        return list(result.scalars().all())


async def _wait_for_log_ids(
    session_factory: async_sessionmaker[AsyncSession],
    expected_ids: set[int],
    *,
    timeout: float = 5,
) -> list[int]:
    async def poll() -> list[int]:
        while True:
            ids = await _read_log_ids(session_factory)
            if set(ids) == expected_ids:
                return ids
            await asyncio.sleep(0.02)

    return await asyncio.wait_for(poll(), timeout=timeout)


@asynccontextmanager
async def _websocket_session(
    app: FastAPI,
    *,
    token: str | None,
    token_source: str,
) -> AsyncIterator[asyncio.Queue[dict[str, Any]]]:
    query_params = {
        "lang": "en",
        "uid": "admin",
        "role": "admin",
        "is_superuser": "true",
    }
    headers = [(b"host", b"testserver")]
    if token_source == "query" and token is not None:
        query_params["token"] = token
    elif token_source == "header" and token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode()))

    scope = {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "scheme": "ws",
        "server": ("testserver", 80),
        "client": ("testclient", 12345),
        "root_path": "",
        "path": LOGS_WS_PATH,
        "raw_path": LOGS_WS_PATH.encode(),
        "query_string": urlencode(query_params).encode(),
        "headers": headers,
        "extensions": {"websocket.http.response": {}},
    }
    incoming: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    outgoing: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
    await incoming.put({"type": "websocket.connect"})

    async def receive() -> dict[str, Any]:
        return await incoming.get()

    async def send(message: dict[str, Any]) -> None:
        await outgoing.put(message)

    task = asyncio.create_task(app(scope, receive, send))
    try:
        yield outgoing
    finally:
        await incoming.put({"type": "websocket.disconnect", "code": 1000})
        await asyncio.wait_for(task, timeout=5)


CREDENTIALS = (
    {"id": "ordinary", "kind": "user", "subject": "user-a", "expected_status": 403},
    {
        "id": "forged-superuser",
        "kind": "user",
        "subject": "user-a",
        "claims": {"is_superuser": True, "uid": "admin", "role": "admin"},
        "expected_status": 403,
    },
    {"id": "missing", "kind": "missing", "expected_status": 401},
    {"id": "invalid", "kind": "invalid", "expected_status": 401},
    {"id": "expired", "kind": "expired", "subject": "user-a", "expected_status": 401},
    {"id": "disabled", "kind": "user", "subject": "disabled", "expected_status": 401},
    {"id": "deleted", "kind": "user", "subject": "deleted", "expected_status": 401},
)

LOG_REQUESTS = (
    {"id": "unfiltered", "params": None},
    {
        "id": "forged-query-filters",
        "params": {
            "uid": "admin",
            "role": "admin",
            "is_superuser": "true",
            "session_id": "session-admin",
        },
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", CREDENTIALS, ids=lambda credential: credential["id"])
@pytest.mark.parametrize("request_case", LOG_REQUESTS, ids=lambda request_case: request_case["id"])
async def test_system_logs_reject_unauthorized_credentials(
    api_app: FastAPI,
    credential: dict[str, Any],
    request_case: dict[str, Any],
) -> None:
    token = _credential_token(credential)
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get(LOGS_PATH, headers=headers, params=request_case["params"])

    expected_message = ERR_ONLY_ADMIN_ALLOWED if credential["expected_status"] == 403 else ERR_UNAUTHORIZED
    _assert_error(response, credential["expected_status"], t(expected_message))


@pytest.mark.asyncio
async def test_superuser_reads_and_filters_all_system_logs(api_app: FastAPI) -> None:
    token = _token("admin")
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        all_logs = await client.get(LOGS_PATH, headers=headers)
        page_two = await client.get(LOGS_PATH, headers=headers, params={"page": "2", "size": "1"})
        filtered = await client.get(
            LOGS_PATH,
            headers=headers,
            params={"level": "WARNING", "uid": "user-a", "session_id": "session-user-a"},
        )

    assert all_logs.status_code == 200
    all_payload = all_logs.json()
    assert all_payload["code"] == 200
    assert all_payload["message"] == t(MSG_LOG_LIST_SUCCESS)
    assert all_payload["data"]["total"] == 3
    assert [item["message"] for item in all_payload["data"]["items"]] == ["user-b error", "user-a warning", "admin login"]
    assert [item["uid"] for item in all_payload["data"]["items"]] == ["user-b", "user-a", "admin"]

    assert page_two.status_code == 200
    page_payload = page_two.json()
    assert page_payload["code"] == 200
    assert page_payload["data"]["total"] == 3
    assert page_payload["data"]["page"] == 2
    assert page_payload["data"]["size"] == 1
    assert page_payload["data"]["items"][0]["message"] == "user-a warning"
    assert page_payload["data"]["items"][0]["uid"] == "user-a"

    assert filtered.status_code == 200
    filtered_payload = filtered.json()
    assert filtered_payload["code"] == 200
    assert filtered_payload["data"]["total"] == 1
    assert filtered_payload["data"]["items"] == [
        {
            "level": "WARNING",
            "module": "chat",
            "message": "user-a warning",
            "uid": "user-a",
            "session_id": "session-user-a",
            "extra": '{"source":"user-a"}',
            "id": 2,
            "created_at": "2026-01-01T00:00:02",
        }
    ]


@pytest.mark.asyncio
async def test_system_log_cleaner_applies_live_retention_and_entry_limits(
    api_app: FastAPI,
    log_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": f"Bearer {_token('admin')}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        async with log_session_factory() as session:
            await session.execute(delete(SystemLog))
            now = get_local_time()
            old_log = SystemLog(level="INFO", module="cleanup", message="older than seven days", created_at=now - timedelta(days=10))
            recent_log = SystemLog(level="INFO", module="cleanup", message="inside retention", created_at=now - timedelta(days=1))
            session.add_all([old_log, recent_log])
            await session.commit()

        assert old_log.id is not None
        assert recent_log.id is not None
        await _set_log_settings(client, headers, retention_days=30, max_entries=10)
        monkeypatch.setattr(tasks, "AsyncSessionLocal", log_session_factory)
        monkeypatch.setattr(tasks, "SYSTEM_LOG_CLEANUP_BATCH_SIZE", 2)
        monkeypatch.setattr(tasks, "SYSTEM_LOG_CLEANUP_BATCH_PAUSE_SECONDS", 0.0)
        cleaner_task = asyncio.create_task(tasks.background_log_cleaner(interval_seconds=0.02))
        try:
            wide_page = await _wait_for_log_page(client, headers, {old_log.id, recent_log.id})
            assert [item["id"] for item in wide_page["items"]] == [recent_log.id, old_log.id]

            await _set_log_settings(client, headers, retention_days=7, max_entries=10)
            retained_page = await _wait_for_log_page(client, headers, {recent_log.id})
            assert retained_page["total"] == 1
            assert retained_page["items"][0]["message"] == "inside retention"
            assert await _read_log_ids(log_session_factory) == [recent_log.id]
            assert not cleaner_task.done()

            async with log_session_factory() as session:
                await session.execute(delete(SystemLog))
                final_now = get_local_time()
                same_time = final_now - timedelta(days=1)
                same_time_low = SystemLog(level="INFO", module="cleanup", message="same-time-low", created_at=same_time)
                older_one = SystemLog(level="INFO", module="cleanup", message="older-one", created_at=final_now - timedelta(days=6))
                older_two = SystemLog(level="INFO", module="cleanup", message="older-two", created_at=final_now - timedelta(days=5))
                same_time_middle = SystemLog(level="INFO", module="cleanup", message="same-time-middle", created_at=same_time)
                hole = SystemLog(level="INFO", module="cleanup", message="deleted-id-hole", created_at=final_now - timedelta(days=4))
                older_three = SystemLog(level="INFO", module="cleanup", message="older-three", created_at=final_now - timedelta(days=3))
                same_time_high = SystemLog(level="INFO", module="cleanup", message="same-time-high", created_at=same_time)
                older_four = SystemLog(level="INFO", module="cleanup", message="older-four", created_at=final_now - timedelta(days=2))
                same_time_latest = SystemLog(level="INFO", module="cleanup", message="same-time-latest", created_at=same_time)
                session.add_all(
                    [
                        same_time_low,
                        older_one,
                        older_two,
                        same_time_middle,
                        hole,
                        older_three,
                        same_time_high,
                        older_four,
                        same_time_latest,
                    ]
                )
                await session.commit()
                assert all(log.id is not None for log in (same_time_low, older_one, older_two, same_time_middle, hole, older_three, same_time_high, older_four, same_time_latest))
                assert same_time_low.id < older_one.id < older_two.id < same_time_middle.id < hole.id < older_three.id < same_time_high.id < older_four.id < same_time_latest.id
                hole_id = hole.id
                await session.delete(hole)
                await session.commit()

            await _set_log_settings(client, headers, retention_days=7, max_entries=2)
            expected_ids = {same_time_high.id, same_time_latest.id}
            final_page = await _wait_for_log_page(client, headers, expected_ids)
            assert final_page["total"] == 2
            assert [item["id"] for item in final_page["items"]] == [same_time_latest.id, same_time_high.id]
            assert [item["message"] for item in final_page["items"]] == ["same-time-latest", "same-time-high"]

            async with log_session_factory() as session:
                result = await session.execute(select(SystemLog.id, SystemLog.created_at, SystemLog.message).order_by(SystemLog.created_at.desc(), SystemLog.id.desc()))
                rows = result.all()
            assert [(row[0], row[2]) for row in rows] == [
                (same_time_latest.id, "same-time-latest"),
                (same_time_high.id, "same-time-high"),
            ]
            assert rows[0][1] == rows[1][1]
            assert hole_id not in {row[0] for row in rows}
            assert not cleaner_task.done()
        finally:
            cleaner_task.cancel()
            await asyncio.gather(cleaner_task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", CREDENTIALS, ids=lambda credential: credential["id"])
@pytest.mark.parametrize("token_source", ("query", "header"))
async def test_system_logs_websocket_rejects_unauthorized_credentials(
    api_app: FastAPI,
    log_broadcaster: LogBroadcaster,
    credential: dict[str, Any],
    token_source: str,
) -> None:
    token = _credential_token(credential)
    expected_status = credential["expected_status"]
    expected_message = ERR_ONLY_ADMIN_ALLOWED if expected_status == 403 else ERR_UNAUTHORIZED

    async with _websocket_session(api_app, token=token, token_source=token_source) as messages:
        response_start = await asyncio.wait_for(messages.get(), timeout=5)
        response_body = await asyncio.wait_for(messages.get(), timeout=5)

        assert response_start["type"] == "websocket.http.response.start"
        assert response_start["status"] == expected_status
        assert response_body["type"] == "websocket.http.response.body"
        payload = json.loads(response_body["body"])
        assert payload == {"code": expected_status, "message": t(expected_message), "data": None}
        assert messages.empty()

    assert not log_broadcaster.active_connections


@pytest.mark.asyncio
async def test_system_log_websocket_receives_new_log_after_live_cleanup(
    api_app: FastAPI,
    log_broadcaster: LogBroadcaster,
    log_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    headers = {"Authorization": f"Bearer {_token('admin')}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        await _set_log_settings(client, headers, retention_days=7, max_entries=1)
        async with log_session_factory() as session:
            await session.execute(delete(SystemLog))
            expired_log = SystemLog(
                level="INFO",
                module="cleanup",
                message="expired before cleaner",
                uid="cleanup-user",
                session_id="cleanup-session",
                extra='{"source":"expired"}',
                created_at=get_local_time() - timedelta(days=8),
            )
            session.add(expired_log)
            await session.commit()
        assert expired_log.id is not None

        monkeypatch.setattr(tasks, "AsyncSessionLocal", log_session_factory)
        monkeypatch.setattr(broadcaster_module, "AsyncSessionLocal", log_session_factory)
        read_gate = asyncio.Event()
        list_after_id_started = asyncio.Event()
        original_list_after_id = broadcaster_module.system_log_crud.list_after_id

        async def gated_list_after_id(db: AsyncSession, *, after_id: int, limit: int) -> list[SystemLog]:
            list_after_id_started.set()
            await read_gate.wait()
            return await original_list_after_id(db, after_id=after_id, limit=limit)

        monkeypatch.setattr(broadcaster_module.system_log_crud, "list_after_id", gated_list_after_id)
        cleaner_task: asyncio.Task[None] | None = None
        task_clock: dict[str, datetime] = {}
        try:
            await log_broadcaster.start()
            await asyncio.wait_for(list_after_id_started.wait(), timeout=5)
            async with _websocket_session(api_app, token=_token("admin"), token_source="query") as messages:
                accepted = await asyncio.wait_for(messages.get(), timeout=5)
                history_message = await asyncio.wait_for(messages.get(), timeout=5)
                assert accepted["type"] == "websocket.accept"
                assert history_message["type"] == "websocket.send"
                history = json.loads(history_message["text"])
                assert history["type"] == "history"
                assert [item["message"] for item in history["logs"]] == ["expired before cleaner"]

                written_at = get_local_time()
                first_log = SystemLog(
                    level="INFO",
                    module="system",
                    message="after cleanup first",
                    uid="new-user-a",
                    session_id="new-session-a",
                    extra='{"source":"after-cleanup-first"}',
                    created_at=written_at,
                )
                second_log = SystemLog(
                    level="INFO",
                    module="system",
                    message="after cleanup second",
                    uid="new-user-b",
                    session_id="new-session-b",
                    extra='{"source":"after-cleanup-second"}',
                    created_at=written_at,
                )
                async with log_session_factory() as session:
                    session.add_all([first_log, second_log])
                    await session.commit()
                assert first_log.id is not None
                assert second_log.id is not None
                assert first_log.id < second_log.id

                task_clock["now"] = written_at
                monkeypatch.setattr(tasks, "get_local_time", lambda: task_clock["now"])
                assert await tasks._cleanup_system_log_batch() == 1
                assert await _read_log_ids(log_session_factory) == [first_log.id, second_log.id]
                held_page = await _get_log_page(client, headers)
                assert held_page["total"] == 2
                assert [item["id"] for item in held_page["items"]] == [second_log.id, first_log.id]
                assert messages.empty()

                cleaner_task = asyncio.create_task(tasks.background_log_cleaner(interval_seconds=0.02))
                try:
                    read_gate.set()
                    received = [json.loads((await asyncio.wait_for(messages.get(), timeout=5))["text"]) for _ in range(2)]
                    assert [item["message"] for item in received] == ["after cleanup first", "after cleanup second"]
                    assert [item["uid"] for item in received] == ["new-user-a", "new-user-b"]
                    assert await _read_log_ids(log_session_factory) == [first_log.id, second_log.id]

                    task_clock["now"] = written_at + timedelta(seconds=SYSTEM_LOG_REALTIME_GRACE_SECONDS + 1)
                    final_page = await _wait_for_log_page(client, headers, {second_log.id})
                    assert final_page["total"] == 1
                    assert final_page["items"][0]["id"] == second_log.id
                    assert final_page["items"][0]["message"] == "after cleanup second"
                    assert await _read_log_ids(log_session_factory) == [second_log.id]
                    assert not cleaner_task.done()
                finally:
                    read_gate.set()
        finally:
            read_gate.set()
            if cleaner_task is not None:
                cleaner_task.cancel()
                await asyncio.gather(cleaner_task, return_exceptions=True)
            await log_broadcaster.stop()

    assert not log_broadcaster.active_connections


@pytest.mark.asyncio
@pytest.mark.parametrize("token_source", ("query", "header"))
async def test_superuser_receives_system_log_history_and_realtime_updates(
    api_app: FastAPI,
    log_broadcaster: LogBroadcaster,
    token_source: str,
) -> None:
    token = _token("admin")
    async with _websocket_session(api_app, token=token, token_source=token_source) as messages:
        accepted = await asyncio.wait_for(messages.get(), timeout=5)
        history_message = await asyncio.wait_for(messages.get(), timeout=5)

        assert accepted["type"] == "websocket.accept"
        assert history_message["type"] == "websocket.send"
        history = json.loads(history_message["text"])
        assert history["type"] == "history"
        assert [(item["message"], item["uid"], item["extra"]) for item in history["logs"]] == [
            ("admin login", "admin", {"source": "admin"}),
            ("user-a warning", "user-a", {"source": "user-a"}),
            ("user-b error", "user-b", {"source": "user-b"}),
        ]

        realtime_log = {
            "timestamp": "2026-01-01 00:00:04.000",
            "level": "INFO",
            "module": "system",
            "message": "live event",
            "uid": "user-c",
            "session_id": "session-user-c",
            "extra": {"source": "realtime"},
        }
        await log_broadcaster.broadcast(realtime_log)
        realtime_message = await asyncio.wait_for(messages.get(), timeout=5)

        assert realtime_message["type"] == "websocket.send"
        assert json.loads(realtime_message["text"]) == realtime_log
        assert messages.empty()

    assert not log_broadcaster.active_connections
