import asyncio
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
import app.handler as app_handler
from app.api.v1 import auth
from app.handler import register_handlers, register_middlewares
from app.models.user import User
from app.providers.database import get_db

AUTH_LOGIN = "/api/v1/auth/login"
AUTH_CHANGE_PASSWORD = "/api/v1/auth/change-password"
AUTH_ME = "/api/v1/auth/me"
OLD_PASSWORD = "integration-old-password"
TEST_JWT_SECRET = "auth-password-change-integration-jwt-secret"
VALID_NEW_PASSWORD = "integration-new-password"


@pytest.fixture(scope="module")
def password_hashes() -> dict[str, str]:
    return {
        "standard": security.get_password_hash(OLD_PASSWORD),
        "legacy": security.get_password_hash("x"),
    }


@pytest_asyncio.fixture
async def db_session(
    setup_session_factory: async_sessionmaker[AsyncSession],
    password_hashes: dict[str, str],
) -> AsyncIterator[AsyncSession]:
    async with setup_session_factory() as session:
        session.add_all(
            [
                User(uid="normal-user", username="normal-user", is_active=True, is_superuser=False, hashed_password=password_hashes["standard"]),
                User(uid="super-user", username="super-admin", is_active=True, is_superuser=True, hashed_password=password_hashes["standard"]),
                User(uid="peer-user", username="peer-user", is_active=True, is_superuser=False, hashed_password=password_hashes["standard"]),
                User(uid="disabled-user", username="disabled-user", is_active=False, is_superuser=False, hashed_password=password_hashes["standard"]),
                User(uid="no-password-user", username="no-password-user", is_active=True, is_superuser=False, hashed_password=None),
                User(uid="legacy-user", username="legacy-user", is_active=True, is_superuser=False, hashed_password=password_hashes["legacy"]),
            ]
        )
        await session.commit()
        yield session


@pytest_asyncio.fixture
async def api_app(
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(security, "AsyncSessionLocal", setup_session_factory)
    monkeypatch.setattr(app_handler, "AsyncSessionLocal", setup_session_factory)
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

    app = FastAPI()
    register_handlers(app)
    register_middlewares(app)
    app.include_router(auth.router, prefix="/api/v1/auth")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with setup_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app


async def _snapshot(factory: async_sessionmaker[AsyncSession]) -> tuple[tuple[Any, ...], ...]:
    async with factory() as session:
        result = await session.execute(select(User).order_by(User.id))
        return tuple((user.username, user.uid, user.is_active, user.is_superuser, user.hashed_password) for user in result.scalars().all())


def _row(snapshot: tuple[tuple[Any, ...], ...], uid: str) -> tuple[Any, ...]:
    return next(row for row in snapshot if row[1] == uid)


def _assert_standard(response: httpx.Response, status_code: int) -> dict[str, Any]:
    payload = response.json()
    assert response.status_code == status_code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == status_code
    return payload


def _assert_no_secrets(
    response: httpx.Response,
    *,
    secrets: tuple[str, ...] = (),
    hashes: tuple[str, ...] = (),
) -> None:
    response_text = response.text
    assert all(secret not in response_text for secret in secrets)
    assert all(stored_hash not in response_text for stored_hash in hashes)


def _assert_public_user(
    data: dict[str, Any],
    *,
    uid: str,
    username: str,
    is_active: bool,
    is_superuser: bool,
) -> None:
    assert set(data) == {"id", "uid", "username", "is_active", "is_superuser"}
    assert data["uid"] == uid
    assert data["username"] == username
    assert data["is_active"] is is_active
    assert data["is_superuser"] is is_superuser
    assert "password" not in data
    assert "hashed_password" not in data


def _token(username: str, *, expires_delta: timedelta | None = None) -> str:
    return security.create_access_token(data={"sub": username}, expires_delta=expires_delta)


async def _login(client: httpx.AsyncClient, username: str, password: str) -> str:
    response = await client.post(AUTH_LOGIN, json={"username": username, "password": password})
    payload = _assert_standard(response, 200)
    assert set(payload["data"]) == {"access_token", "token_type"}
    assert payload["data"]["token_type"] == "bearer"
    return payload["data"]["access_token"]


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json: Any = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = None if token is None else {"Authorization": f"Bearer {token}"}
    return await client.request(method, path, headers=headers, json=json, params=params)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("uid", "username", "is_superuser", "current_password", "new_password"),
    [
        ("normal-user", "normal-user", False, OLD_PASSWORD, "normal-password-updated"),
        ("super-user", "super-admin", True, OLD_PASSWORD, "super-password-updated"),
        ("normal-user", "normal-user", False, OLD_PASSWORD, "a" * 8),
        ("normal-user", "normal-user", False, OLD_PASSWORD, "a" * 72),
        ("normal-user", "normal-user", False, OLD_PASSWORD, "中" * 24),
        ("normal-user", "normal-user", False, OLD_PASSWORD, "\U0001f600" * 18),
        ("legacy-user", "legacy-user", False, "x", "legacy-password-updated"),
    ],
    ids=["normal-user", "superuser", "ascii-8", "ascii-72", "utf8-72", "emoji-72", "legacy-user"],
)
async def test_user_can_change_only_own_password_and_keep_jwt_identity(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    uid: str,
    username: str,
    is_superuser: bool,
    current_password: str,
    new_password: str,
) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        before = await _snapshot(setup_session_factory)
        token = await _login(client, username, current_password)
        response = await _request(
            client,
            "POST",
            AUTH_CHANGE_PASSWORD,
            token=token,
            params={"uid": "peer-user", "username": "peer-user"},
            json={"current_password": current_password, "new_password": new_password},
        )
        payload = _assert_standard(response, 200)
        assert payload["data"] is None
        _assert_no_secrets(
            response,
            secrets=tuple(secret for secret in (current_password, new_password) if len(secret) > 1),
            hashes=tuple(row[4] for row in before if row[4]),
        )

        after = await _snapshot(setup_session_factory)
        assert tuple(row for row in after if row[1] != uid) == tuple(row for row in before if row[1] != uid)
        old_row = _row(before, uid)
        new_row = _row(after, uid)
        assert new_row[:4] == old_row[:4]
        assert new_row[4] != old_row[4]
        assert new_row[4] is not None
        assert security.verify_password(new_password, new_row[4])

        old_login = await client.post(AUTH_LOGIN, json={"username": username, "password": current_password})
        assert _assert_standard(old_login, 401)["data"] is None
        new_login = await client.post(AUTH_LOGIN, json={"username": username, "password": new_password})
        assert _assert_standard(new_login, 200)["data"]["token_type"] == "bearer"

        me = await _request(client, "GET", AUTH_ME, token=token)
        me_payload = _assert_standard(me, 200)
        _assert_public_user(me_payload["data"], uid=uid, username=username, is_active=True, is_superuser=is_superuser)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("locale", "expected_message"),
    [("zh", "当前密码错误"), ("en", "Current password is incorrect")],
)
async def test_wrong_current_password_is_localized_and_persistent_state_is_unchanged(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    locale: str,
    expected_message: str,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=api_app),
        base_url="http://test",
        headers={"Accept-Language": locale},
    ) as client:
        before = await _snapshot(setup_session_factory)
        token = await _login(client, "normal-user", OLD_PASSWORD)
        response = await _request(
            client,
            "POST",
            AUTH_CHANGE_PASSWORD,
            token=token,
            json={"current_password": "wrong-current-password", "new_password": "would-not-persist"},
        )
        payload = _assert_standard(response, 400)
        assert payload["message"] == expected_message
        assert payload["data"] is None
        _assert_no_secrets(response, secrets=("wrong-current-password", "would-not-persist"), hashes=tuple(row[4] for row in before if row[4]))
        assert await _snapshot(setup_session_factory) == before

        me = await _request(client, "GET", AUTH_ME, token=token)
        me_payload = _assert_standard(me, 200)
        _assert_public_user(me_payload["data"], uid="normal-user", username="normal-user", is_active=True, is_superuser=False)


@pytest.mark.asyncio
async def test_user_without_password_cannot_set_one_with_an_arbitrary_old_password(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        before = await _snapshot(setup_session_factory)
        response = await _request(
            client,
            "POST",
            AUTH_CHANGE_PASSWORD,
            token=_token("no-password-user"),
            json={"current_password": "arbitrary-old-password", "new_password": VALID_NEW_PASSWORD},
        )
        assert _assert_standard(response, 400)["data"] is None
        assert await _snapshot(setup_session_factory) == before


UNAUTHORIZED_CREDENTIALS = ("missing", "invalid", "expired", "disabled", "unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", UNAUTHORIZED_CREDENTIALS)
async def test_change_password_rejects_unauthorized_credentials_without_mutation(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    credential: str,
) -> None:
    if credential == "missing":
        token = None
    elif credential == "invalid":
        token = "not-a-valid-jwt"
    elif credential == "expired":
        token = _token("normal-user", expires_delta=timedelta(seconds=-1))
    elif credential == "disabled":
        token = _token("disabled-user")
    else:
        token = _token("does-not-exist")

    before = await _snapshot(setup_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            AUTH_CHANGE_PASSWORD,
            token=token,
            json={"current_password": OLD_PASSWORD, "new_password": VALID_NEW_PASSWORD},
        )
    assert _assert_standard(response, 401)["data"] is None
    assert await _snapshot(setup_session_factory) == before


VALIDATION_CASES = (
    {"id": "missing-body"},
    {"id": "missing-current", "body": {"new_password": VALID_NEW_PASSWORD}},
    {"id": "missing-new", "body": {"current_password": OLD_PASSWORD}},
    {"id": "empty-current", "body": {"current_password": "", "new_password": VALID_NEW_PASSWORD}},
    {"id": "empty-new", "body": {"current_password": OLD_PASSWORD, "new_password": ""}},
    {"id": "null-current", "body": {"current_password": None, "new_password": VALID_NEW_PASSWORD}},
    {"id": "null-new", "body": {"current_password": OLD_PASSWORD, "new_password": None}},
    {"id": "non-string-current", "body": {"current_password": 123456789, "new_password": VALID_NEW_PASSWORD}},
    {"id": "non-string-new", "body": {"current_password": OLD_PASSWORD, "new_password": 123456789}},
    {"id": "short-new", "body": {"current_password": OLD_PASSWORD, "new_password": "1234567"}},
    {"id": "current-ascii-too-long", "body": {"current_password": "c" * 73, "new_password": VALID_NEW_PASSWORD}},
    {"id": "new-ascii-too-long", "body": {"current_password": OLD_PASSWORD, "new_password": "n" * 73}},
    {"id": "current-utf8-too-long", "body": {"current_password": "中" * 25, "new_password": VALID_NEW_PASSWORD}},
    {"id": "new-utf8-too-long", "body": {"current_password": OLD_PASSWORD, "new_password": "中" * 25}},
    {"id": "forged-uid", "body": {"current_password": OLD_PASSWORD, "new_password": VALID_NEW_PASSWORD, "uid": "peer-user"}},
    {"id": "forged-username", "body": {"current_password": OLD_PASSWORD, "new_password": VALID_NEW_PASSWORD, "username": "peer-user"}},
    {"id": "forged-is-superuser", "body": {"current_password": OLD_PASSWORD, "new_password": VALID_NEW_PASSWORD, "is_superuser": True}},
)


def _password_inputs(case: dict[str, Any]) -> tuple[str, ...]:
    body = case.get("body")
    if not isinstance(body, dict):
        return ()
    return tuple(value for key, value in body.items() if key in {"current_password", "new_password"} and isinstance(value, str) and len(value) > 1)


@pytest.mark.asyncio
@pytest.mark.parametrize("case", VALIDATION_CASES, ids=lambda case: case["id"])
async def test_change_password_validation_rejects_invalid_bodies_without_leaking_or_mutating(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    case: dict[str, Any],
) -> None:
    before = await _snapshot(setup_session_factory)
    token = _token("normal-user")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        if "body" not in case:
            response = await client.post(AUTH_CHANGE_PASSWORD, headers={"Authorization": f"Bearer {token}"})
        else:
            response = await client.post(
                AUTH_CHANGE_PASSWORD,
                headers={"Authorization": f"Bearer {token}"},
                json=case["body"],
            )
    payload = _assert_standard(response, 422)
    assert payload["data"] is None
    _assert_no_secrets(
        response,
        secrets=_password_inputs(case),
        hashes=tuple(row[4] for row in before if row[4]),
    )
    assert await _snapshot(setup_session_factory) == before


@pytest.mark.asyncio
async def test_concurrent_changes_with_same_old_password_have_one_winner(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_new_password = "concurrent-password-one"
    second_new_password = "concurrent-password-two"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        before = await _snapshot(setup_session_factory)
        token = await _login(client, "normal-user", OLD_PASSWORD)
        responses = await asyncio.gather(
            _request(
                client,
                "POST",
                AUTH_CHANGE_PASSWORD,
                token=token,
                json={"current_password": OLD_PASSWORD, "new_password": first_new_password},
            ),
            _request(
                client,
                "POST",
                AUTH_CHANGE_PASSWORD,
                token=token,
                json={"current_password": OLD_PASSWORD, "new_password": second_new_password},
            ),
        )

        assert sorted(response.status_code for response in responses) == [200, 400]
        for response in responses:
            payload = _assert_standard(response, response.status_code)
            assert payload["data"] is None

        winner_index = 0 if responses[0].status_code == 200 else 1
        winner_password = (first_new_password, second_new_password)[winner_index]
        loser_password = (second_new_password, first_new_password)[winner_index]
        after = await _snapshot(setup_session_factory)
        assert tuple(row for row in after if row[1] != "normal-user") == tuple(row for row in before if row[1] != "normal-user")
        old_row = _row(before, "normal-user")
        new_row = _row(after, "normal-user")
        assert new_row[:4] == old_row[:4]
        assert new_row[4] != old_row[4]
        assert new_row[4] is not None
        assert security.verify_password(winner_password, new_row[4])
        assert not security.verify_password(OLD_PASSWORD, new_row[4])
        assert not security.verify_password(loser_password, new_row[4])

        winner_login = await client.post(AUTH_LOGIN, json={"username": "normal-user", "password": winner_password})
        assert _assert_standard(winner_login, 200)["data"]["token_type"] == "bearer"
        old_login = await client.post(AUTH_LOGIN, json={"username": "normal-user", "password": OLD_PASSWORD})
        assert _assert_standard(old_login, 401)["data"] is None
        loser_login = await client.post(AUTH_LOGIN, json={"username": "normal-user", "password": loser_password})
        assert _assert_standard(loser_login, 401)["data"] is None
