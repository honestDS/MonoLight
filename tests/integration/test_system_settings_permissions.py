from collections.abc import AsyncIterator
from datetime import timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.security as security
from app.api.v1 import system
from app.core.constants import ERR_ONLY_ADMIN_ALLOWED, ERR_UNAUTHORIZED
from app.core.i18n import t
from app.handler import register_handlers
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.providers.database import get_db

TEST_PASSWORD = "integration-password"
TEST_JWT_SECRET = "system-settings-integration-jwt-secret"
SETTINGS_PATH = "/api/v1/system/settings"

RUNTIME_KEYS = (
    "log_locale",
    "temp_dir_max_size_mb",
    "audit_retention_days",
    "audit_report_email",
    "session_reply_max_concurrency",
)


@pytest.fixture(scope="module")
def password_hash() -> str:
    return security.get_password_hash(TEST_PASSWORD)


@pytest_asyncio.fixture
async def seed_data(
    setup_session_factory: async_sessionmaker[AsyncSession],
    password_hash: str,
) -> None:
    async with setup_session_factory() as session:
        session.add_all(
            [
                User(uid="admin", username="admin", is_active=True, is_superuser=True, hashed_password=password_hash),
                User(uid="user-a", username="user-a", is_active=True, is_superuser=False, hashed_password=password_hash),
                User(uid="disabled", username="disabled", is_active=False, is_superuser=False, hashed_password=password_hash),
                SystemSetting(key="log_locale", value="en-US"),
                SystemSetting(key="temp_dir_max_size_mb", value="2048"),
                SystemSetting(key="audit_retention_days", value="180"),
                SystemSetting(key="audit_report_email", value="initial@example.com"),
                SystemSetting(key="session_reply_max_concurrency", value="8"),
            ]
        )
        await session.commit()


@pytest_asyncio.fixture
async def api_app(
    seed_data: None,
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(security, "AsyncSessionLocal", setup_session_factory)
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

    app = FastAPI()
    register_handlers(app)
    app.include_router(system.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with setup_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app


async def _settings_snapshot(setup_session_factory: async_sessionmaker[AsyncSession]) -> tuple[tuple[Any, ...], ...]:
    async with setup_session_factory() as session:
        result = await session.execute(select(SystemSetting).order_by(SystemSetting.id))
        return tuple((item.id, item.key, item.value) for item in result.scalars().all())


def _token(username: str, **claims: Any) -> str:
    return security.create_access_token(data={"sub": username, **claims})


async def _request(
    client: httpx.AsyncClient,
    method: str,
    *,
    token: str | None = None,
    json: dict[str, Any] | None = None,
    params: dict[str, str] | None = None,
) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return await client.request(method, SETTINGS_PATH, headers=headers, json=json, params=params)


def _assert_error(response: httpx.Response, code: int, message: str) -> None:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert payload["message"] == message
    assert payload["data"] is None


VALID_SETTINGS = {
    "log_locale": "en",
    "temp_dir_max_size_mb": 3072,
    "audit_retention_days": 365,
    "audit_report_email": "updated@example.com",
    "session_reply_max_concurrency": 12,
}


FORBIDDEN_REQUESTS = (
    {"id": "normal-get", "method": "GET", "token_claims": {}, "json": None, "params": None},
    {"id": "normal-valid-post", "method": "POST", "token_claims": {}, "json": VALID_SETTINGS, "params": None},
    {
        "id": "forged-superuser-get",
        "method": "GET",
        "token_claims": {"is_superuser": True},
        "json": None,
        "params": {"uid": "admin", "role": "admin"},
    },
    {
        "id": "forged-superuser-valid-post",
        "method": "POST",
        "token_claims": {"is_superuser": True},
        "json": VALID_SETTINGS,
        "params": {"uid": "admin", "role": "admin"},
    },
)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FORBIDDEN_REQUESTS, ids=lambda case: case["id"])
async def test_non_superuser_cannot_read_or_write_system_settings(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    case: dict[str, Any],
) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        before = await _settings_snapshot(setup_session_factory)
        response = await _request(
            client,
            case["method"],
            token=_token("user-a", **case["token_claims"]),
            json=case["json"],
            params=case["params"],
        )
        _assert_error(response, 403, t(ERR_ONLY_ADMIN_ALLOWED))
        assert await _settings_snapshot(setup_session_factory) == before


UNAUTHORIZED_CREDENTIALS = ("missing", "invalid", "expired", "disabled")
UNAUTHORIZED_REQUESTS = (
    {"id": "get", "method": "GET", "json": None},
    {"id": "post", "method": "POST", "json": VALID_SETTINGS},
)


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", UNAUTHORIZED_CREDENTIALS)
@pytest.mark.parametrize("case", UNAUTHORIZED_REQUESTS, ids=lambda case: case["id"])
async def test_system_settings_rejects_unauthorized_credentials_without_changes(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    credential: str,
    case: dict[str, Any],
) -> None:
    if credential == "missing":
        token = None
    elif credential == "invalid":
        token = "not-a-valid-jwt"
    elif credential == "expired":
        token = security.create_access_token(data={"sub": "user-a"}, expires_delta=timedelta(seconds=-1))
    else:
        token = _token("disabled")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        before = await _settings_snapshot(setup_session_factory)
        response = await _request(client, case["method"], token=token, json=case["json"])
        _assert_error(response, 401, t(ERR_UNAUTHORIZED))
        assert await _settings_snapshot(setup_session_factory) == before


@pytest.mark.asyncio
async def test_superuser_reads_updates_and_persists_normalized_system_settings(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = _token("admin")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        initial = await client.get(SETTINGS_PATH, headers={"Authorization": f"Bearer {token}"})
        assert initial.status_code == 200
        assert initial.json()["code"] == 200
        assert initial.json()["data"] == {
            "log_locale": "en",
            "temp_dir_max_size_mb": 2048,
            "audit_retention_days": 180,
            "audit_report_email": "initial@example.com",
            "session_reply_max_concurrency": 8,
        }

        updated = await client.post(
            SETTINGS_PATH,
            headers={"Authorization": f"Bearer {token}"},
            json={
                "log_locale": " EN-US ",
                "temp_dir_max_size_mb": 3072,
                "audit_retention_days": 365,
                "audit_report_email": "  updated@example.com  ",
                "session_reply_max_concurrency": 12,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["code"] == 200
        assert updated.json()["data"] == VALID_SETTINGS

        persisted = {key: value for _, key, value in await _settings_snapshot(setup_session_factory) if key in RUNTIME_KEYS}
        assert persisted == {
            "log_locale": "en",
            "temp_dir_max_size_mb": "3072",
            "audit_retention_days": "365",
            "audit_report_email": "updated@example.com",
            "session_reply_max_concurrency": "12",
        }

        after = await client.get(SETTINGS_PATH, headers={"Authorization": f"Bearer {token}"})
        assert after.status_code == 200
        assert after.json()["code"] == 200
        assert after.json()["data"] == VALID_SETTINGS
