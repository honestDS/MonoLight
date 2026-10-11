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
from app.api.v1 import auth, users
from app.core.constants import (
    ERR_ONLY_ADMIN_ALLOWED,
    ERR_UNAUTHORIZED,
    ERR_USER_SUPER_DELETE_FORBIDDEN,
    ERR_USER_SUPER_PROTECTED,
)
from app.core.i18n import t
from app.handler import register_handlers
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.user import User
from app.providers.database import get_db

TEST_PASSWORD = "integration-password"
TEST_JWT_SECRET = "user-management-integration-jwt-secret"

AUTH_LOGIN = "/api/v1/auth/login"
AUTH_ME = "/api/v1/auth/me"
ADMIN_LIST = "/api/v1/admin/user/list"
ADMIN_ADD = "/api/v1/admin/user/add"
ADMIN_UPDATE = "/api/v1/admin/user/update"
ADMIN_DELETE = "/api/v1/admin/user/delete"


@pytest.fixture(scope="module")
def password_hash() -> str:
    return security.get_password_hash(TEST_PASSWORD)


@pytest_asyncio.fixture
async def db_session(
    setup_session_factory: async_sessionmaker[AsyncSession],
    password_hash: str,
) -> AsyncIterator[AsyncSession]:
    async with setup_session_factory() as session:
        session.add_all(
            [
                User(uid="super-user", username="super-admin", is_active=True, is_superuser=True, hashed_password=password_hash),
                User(uid="normal-user", username="normal-user", is_active=True, is_superuser=False, hashed_password=password_hash),
                User(uid="peer-user", username="peer-user", is_active=True, is_superuser=False, hashed_password=password_hash),
                User(uid="disabled-user", username="disabled-user", is_active=False, is_superuser=False, hashed_password=password_hash),
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
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

    app = FastAPI()
    register_handlers(app)
    app.include_router(auth.router, prefix="/api/v1/auth")
    app.include_router(users.router, prefix="/api/v1/admin")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield app


async def _state_snapshot(setup_session_factory: async_sessionmaker[AsyncSession]) -> tuple[tuple[Any, ...], ...]:
    async with setup_session_factory() as session:
        user_result = await session.execute(select(User).order_by(User.id))
        profile_result = await session.execute(select(Profile).order_by(Profile.id))
        prompt_result = await session.execute(select(PromptLibrary).order_by(PromptLibrary.id))
        return (
            tuple((user.id, user.uid, user.username, user.is_active, user.is_superuser, user.hashed_password) for user in user_result.scalars().all()),
            tuple((profile.id, profile.uid, profile.name, profile.prompt_id, profile.configs, profile.is_default) for profile in profile_result.scalars().all()),
            tuple((prompt.id, prompt.uid, prompt.name, prompt.content, prompt.created_at) for prompt in prompt_result.scalars().all()),
        )


async def _get_user(setup_session_factory: async_sessionmaker[AsyncSession], uid: str) -> User | None:
    async with setup_session_factory() as session:
        result = await session.execute(select(User).where(User.uid == uid))
        return result.scalars().first()


def _token(username: str, **claims: Any) -> str:
    return security.create_access_token(data={"sub": username, **claims})


async def _request(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    token: str | None = None,
    json: Any = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return await client.request(method, path, headers=headers, json=json, params=params)


def _assert_error(response: httpx.Response, code: int, message: str | None = None) -> None:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert payload["data"] is None
    if message is not None:
        assert payload["message"] == message


def _assert_public_user(data: dict[str, Any], *, uid: str, username: str, is_active: bool, is_superuser: bool) -> None:
    assert set(data) == {"id", "uid", "username", "is_active", "is_superuser"}
    assert data["uid"] == uid
    assert data["username"] == username
    assert data["is_active"] is is_active
    assert data["is_superuser"] is is_superuser
    assert "password" not in data
    assert "hashed_password" not in data


FORBIDDEN_CASES = (
    {"id": "list", "method": "GET", "path": ADMIN_LIST},
    {
        "id": "add-invalid-fields-and-superuser-claim",
        "method": "POST",
        "path": ADMIN_ADD,
        "json": {"username": "x", "password": "short", "is_superuser": True},
    },
    {
        "id": "update-self",
        "method": "POST",
        "path": ADMIN_UPDATE,
        "json": {"uid": "normal-user", "username": "normal-renamed", "password": "new-password", "is_active": False},
    },
    {
        "id": "update-peer",
        "method": "POST",
        "path": ADMIN_UPDATE,
        "json": {"uid": "peer-user", "username": "peer-renamed", "password": "new-password", "is_active": False},
    },
    {
        "id": "update-superuser",
        "method": "POST",
        "path": ADMIN_UPDATE,
        "json": {"uid": "super-user", "username": "super-renamed", "password": "new-password", "is_active": False},
    },
    {"id": "delete-self", "method": "POST", "path": ADMIN_DELETE, "params": {"uid": "normal-user"}},
    {"id": "delete-peer", "method": "POST", "path": ADMIN_DELETE, "params": {"uid": "peer-user"}},
    {"id": "delete-superuser", "method": "POST", "path": ADMIN_DELETE, "params": {"uid": "super-user"}},
    {"id": "signed-jwt-admin-claim", "method": "GET", "path": ADMIN_LIST, "claims": {"is_superuser": True}},
)


MANAGEMENT_REQUESTS = (
    {"method": "GET", "path": ADMIN_LIST},
    {"method": "POST", "path": ADMIN_ADD, "json": {"username": "request-user", "password": TEST_PASSWORD}},
    {"method": "POST", "path": ADMIN_UPDATE, "json": {"uid": "peer-user", "password": "new-password"}},
    {"method": "POST", "path": ADMIN_DELETE, "params": {"uid": "peer-user"}},
)


UNAUTHORIZED_CREDENTIALS = ("missing", "invalid", "expired", "disabled", "unknown")


@pytest.mark.asyncio
@pytest.mark.parametrize("case", FORBIDDEN_CASES, ids=lambda case: case["id"])
async def test_normal_user_cannot_manage_users_or_change_persistence(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    case: dict[str, Any],
) -> None:
    claims = case.get("claims", {})
    token = _token("normal-user", **claims)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        before = await _state_snapshot(setup_session_factory)
        response = await _request(
            client,
            case["method"],
            case["path"],
            token=token,
            json=case.get("json"),
            params=case.get("params"),
        )
        _assert_error(response, 403, t(ERR_ONLY_ADMIN_ALLOWED))
        assert await _state_snapshot(setup_session_factory) == before


UNAUTHORIZED_REQUESTS = (
    {"id": "list", "method": "GET", "path": ADMIN_LIST},
    {"id": "add", "method": "POST", "path": ADMIN_ADD, "json": {"username": "request-user", "password": TEST_PASSWORD}},
    {"id": "update", "method": "POST", "path": ADMIN_UPDATE, "json": {"uid": "peer-user", "password": "new-password"}},
    {"id": "delete", "method": "POST", "path": ADMIN_DELETE, "params": {"uid": "peer-user"}},
    {"id": "me", "method": "GET", "path": AUTH_ME},
)


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", UNAUTHORIZED_CREDENTIALS)
@pytest.mark.parametrize("case", UNAUTHORIZED_REQUESTS, ids=lambda case: case["id"])
async def test_user_endpoints_reject_missing_invalid_expired_disabled_and_unknown_tokens(
    api_app: FastAPI,
    credential: str,
    case: dict[str, Any],
) -> None:
    if credential == "missing":
        token = None
    elif credential == "invalid":
        token = "not-a-valid-jwt"
    elif credential == "expired":
        token = security.create_access_token(data={"sub": "normal-user"}, expires_delta=timedelta(seconds=-1))
    elif credential == "disabled":
        token = _token("disabled-user")
    else:
        token = _token("does-not-exist")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            case["method"],
            case["path"],
            token=token,
            json=case.get("json"),
            params=case.get("params"),
        )
    _assert_error(response, 401, t(ERR_UNAUTHORIZED))


@pytest.mark.asyncio
async def test_login_and_me_return_only_current_public_database_identity(api_app: FastAPI) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        super_login = await client.post(AUTH_LOGIN, json={"username": "super-admin", "password": TEST_PASSWORD})
        assert super_login.status_code == 200
        super_payload = super_login.json()
        assert super_payload["code"] == 200
        assert set(super_payload["data"]) == {"access_token", "token_type"}
        super_token = super_payload["data"]["access_token"]

        super_me = await client.get(AUTH_ME, headers={"Authorization": f"Bearer {super_token}"})
        assert super_me.status_code == 200
        _assert_public_user(super_me.json()["data"], uid="super-user", username="super-admin", is_active=True, is_superuser=True)

        normal_login = await client.post(AUTH_LOGIN, json={"username": "normal-user", "password": TEST_PASSWORD})
        assert normal_login.status_code == 200
        normal_token = normal_login.json()["data"]["access_token"]
        for token in (normal_token, _token("normal-user", is_superuser=True)):
            for params in ({}, {"uid": "super-user"}, {"username": "super-admin"}, {"is_superuser": "true"}):
                normal_me = await client.get(
                    AUTH_ME,
                    params=params,
                    headers={"Authorization": f"Bearer {token}"},
                )
                assert normal_me.status_code == 200
                _assert_public_user(normal_me.json()["data"], uid="normal-user", username="normal-user", is_active=True, is_superuser=False)


@pytest.mark.asyncio
async def test_superuser_can_manage_accounts_and_protect_own_identity(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        login = await client.post(AUTH_LOGIN, json={"username": "super-admin", "password": TEST_PASSWORD})
        assert login.status_code == 200
        token = login.json()["data"]["access_token"]
        headers = {"Authorization": f"Bearer {token}"}

        listed = await client.get(ADMIN_LIST, headers=headers)
        assert listed.status_code == 200
        listed_data = listed.json()["data"]
        assert listed_data["total"] == 4
        assert {item["uid"] for item in listed_data["items"]} == {"super-user", "normal-user", "peer-user", "disabled-user"}
        initial_state = await _state_snapshot(setup_session_factory)

        created = await client.post(
            ADMIN_ADD,
            headers=headers,
            json={"username": "managed-user", "password": "managed-password", "is_superuser": True},
        )
        assert created.status_code == 200
        created_payload = created.json()
        assert created_payload["code"] == 200
        managed = created_payload["data"]
        managed_uid = managed["uid"]
        _assert_public_user(managed, uid=managed_uid, username="managed-user", is_active=True, is_superuser=False)

        managed_user = await _get_user(setup_session_factory, managed_uid)
        assert managed_user is not None
        assert managed_user.is_superuser is False
        assert managed_user.hashed_password is not None
        assert security.verify_password("managed-password", managed_user.hashed_password)
        async with setup_session_factory() as session:
            profile_result = await session.execute(select(Profile).where(Profile.uid == managed_uid))
            profiles = profile_result.scalars().all()
            prompt_result = await session.execute(select(PromptLibrary).where(PromptLibrary.name == "default"))
            prompts = prompt_result.scalars().all()
        assert len(profiles) == 1
        assert profiles[0].name == "default"
        assert profiles[0].is_default is True
        assert len(prompts) == 1

        updated = await client.post(
            ADMIN_UPDATE,
            headers=headers,
            json={
                "uid": managed_uid,
                "username": "managed-renamed",
                "password": "managed-password-updated",
                "is_active": False,
            },
        )
        assert updated.status_code == 200
        _assert_public_user(updated.json()["data"], uid=managed_uid, username="managed-renamed", is_active=False, is_superuser=False)
        managed_user = await _get_user(setup_session_factory, managed_uid)
        assert managed_user is not None
        assert managed_user.username == "managed-renamed"
        assert managed_user.is_active is False
        assert managed_user.hashed_password is not None
        assert security.verify_password("managed-password-updated", managed_user.hashed_password)

        enabled = await client.post(ADMIN_UPDATE, headers=headers, json={"uid": managed_uid, "is_active": True})
        assert enabled.status_code == 200
        _assert_public_user(enabled.json()["data"], uid=managed_uid, username="managed-renamed", is_active=True, is_superuser=False)

        state_after_updates = await _state_snapshot(setup_session_factory)
        assert tuple(row for row in state_after_updates[0] if row[1] != managed_uid) == initial_state[0]

        deleted = await client.post(ADMIN_DELETE, headers=headers, params={"uid": managed_uid})
        assert deleted.status_code == 200
        assert deleted.json()["code"] == 200
        assert deleted.json()["data"] is None
        assert await _get_user(setup_session_factory, managed_uid) is None

        reset = await client.post(
            ADMIN_UPDATE,
            headers=headers,
            json={"uid": "super-user", "password": "super-password-updated"},
        )
        assert reset.status_code == 200
        _assert_public_user(reset.json()["data"], uid="super-user", username="super-admin", is_active=True, is_superuser=True)

        protected_rename = await client.post(
            ADMIN_UPDATE,
            headers=headers,
            json={"uid": "super-user", "username": "renamed-super"},
        )
        _assert_error(protected_rename, 400, t(ERR_USER_SUPER_PROTECTED))

        protected_disable = await client.post(
            ADMIN_UPDATE,
            headers=headers,
            json={"uid": "super-user", "is_active": False},
        )
        _assert_error(protected_disable, 400, t(ERR_USER_SUPER_PROTECTED))

        protected_delete = await client.post(ADMIN_DELETE, headers=headers, params={"uid": "super-user"})
        _assert_error(protected_delete, 400, t(ERR_USER_SUPER_DELETE_FORBIDDEN))

        super_user = await _get_user(setup_session_factory, "super-user")
        assert super_user is not None
        assert super_user.username == "super-admin"
        assert super_user.is_active is True
        assert super_user.is_superuser is True
        assert super_user.hashed_password is not None
        assert security.verify_password("super-password-updated", super_user.hashed_password)
        final_state = await _state_snapshot(setup_session_factory)
        assert tuple(row for row in final_state[0] if row[1] in {"normal-user", "peer-user", "disabled-user"}) == tuple(row for row in initial_state[0] if row[1] in {"normal-user", "peer-user", "disabled-user"})


@pytest.mark.asyncio
async def test_same_token_observes_database_role_promotion_revocation_and_disable(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    token = _token("normal-user", is_superuser=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        before_me = await _request(client, "GET", AUTH_ME, token=token)
        assert before_me.status_code == 200
        _assert_public_user(before_me.json()["data"], uid="normal-user", username="normal-user", is_active=True, is_superuser=False)
        before_list = await _request(client, "GET", ADMIN_LIST, token=token)
        _assert_error(before_list, 403, t(ERR_ONLY_ADMIN_ALLOWED))

        async with setup_session_factory() as session:
            result = await session.execute(select(User).where(User.uid == "normal-user"))
            user = result.scalars().one()
            user.is_superuser = True
            session.add(user)
            await session.commit()

        promoted_me = await _request(client, "GET", AUTH_ME, token=token)
        assert promoted_me.status_code == 200
        _assert_public_user(promoted_me.json()["data"], uid="normal-user", username="normal-user", is_active=True, is_superuser=True)
        promoted_list = await _request(client, "GET", ADMIN_LIST, token=token)
        assert promoted_list.status_code == 200
        assert {item["uid"] for item in promoted_list.json()["data"]["items"]} == {"super-user", "normal-user", "peer-user", "disabled-user"}

        async with setup_session_factory() as session:
            result = await session.execute(select(User).where(User.uid == "normal-user"))
            user = result.scalars().one()
            user.is_superuser = False
            session.add(user)
            await session.commit()

        for request in MANAGEMENT_REQUESTS:
            response = await _request(
                client,
                request["method"],
                request["path"],
                token=token,
                json=request.get("json"),
                params=request.get("params"),
            )
            _assert_error(response, 403, t(ERR_ONLY_ADMIN_ALLOWED))

        async with setup_session_factory() as session:
            result = await session.execute(select(User).where(User.uid == "normal-user"))
            user = result.scalars().one()
            user.is_active = False
            session.add(user)
            await session.commit()

        disabled_me = await _request(client, "GET", AUTH_ME, token=token)
        _assert_error(disabled_me, 401, t(ERR_UNAUTHORIZED))
        for request in MANAGEMENT_REQUESTS:
            response = await _request(
                client,
                request["method"],
                request["path"],
                token=token,
                json=request.get("json"),
                params=request.get("params"),
            )
            _assert_error(response, 401, t(ERR_UNAUTHORIZED))
