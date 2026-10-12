import asyncio
import subprocess
import sys
import threading
from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import func
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.security as security
import app.core.user_file_cleanup as file_cleanup
from app.api.v1 import auth, users
from app.core.audit.startup import recover_and_cleanup_audit_data
from app.core.audit.storage import write_audit_json
from app.core.constants import (
    ERR_ONLY_ADMIN_ALLOWED,
    ERR_UNAUTHORIZED,
    ERR_USER_SUPER_DELETE_FORBIDDEN,
    ERR_USER_SUPER_PROTECTED,
)
from app.core.crud.account.user import user_crud
from app.core.crud.knowledge.job import knowledge_job_crud
from app.core.crud.memory.job import memory_job_crud
from app.core.crud.task.background import background_task_crud
from app.core.i18n import t
from app.core.knowledge_jobs.executor import KnowledgeJobExecutionContext, KnowledgeJobLeaseLostError
from app.core.memory_jobs.executor import MemoryJobExecutionContext, MemoryJobLeaseLostError
from app.core.user_file_cleanup import cleanup_user_files, recover_user_file_cleanup
from app.handler import register_handlers
from app.models.audit import AuditRecord, AuditRecordStatus
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.knowledge_base import (
    KnowledgeBase,
    KnowledgeBaseCollectionOwner,
    KnowledgeBaseType,
    KnowledgeJob,
    KnowledgeJobOperation,
    KnowledgeJobStatus,
)
from app.models.memory import (
    LongTermMemoryEmbeddingDelta,
    LongTermMemoryEmbeddingRevision,
    LongTermMemoryMutationJob,
    LongTermMemoryMutationOperation,
    LongTermMemoryMutationStatus,
    LongTermMemoryRecord,
    LongTermMemoryRevision,
    LongTermMemoryStore,
)
from app.models.message import Message
from app.models.message_platform import MessagePlatform
from app.models.message_platform_outbox import MessagePlatformOutbox
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.scheduled_task import ScheduledTask
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.session_todo import SessionTodoPlan
from app.models.user import User, UserFileCleanup
from app.providers.database import get_db
from app.providers.database.time import get_database_time, get_database_timestamp

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
async def setup_session_factory(database_factory):
    async with database_factory(foreign_keys=True) as session_factory:
        yield session_factory


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
    tmp_path: Path,
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(security, "AsyncSessionLocal", setup_session_factory)
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)
    monkeypatch.setattr(file_cleanup, "TEMP_DIR", tmp_path / "temp")
    monkeypatch.setattr(file_cleanup, "AUDIT_DIR", tmp_path / "audit")

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


def _create_directory_link(link: Path, target: Path) -> None:
    target.mkdir(parents=True, exist_ok=True)
    link.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        if not callable(getattr(link, "is_junction", None)):
            pytest.skip("junction detection unavailable")
        try:
            result = subprocess.run(
                ["cmd", "/c", "mklink", "/J", str(link), str(target)],
                capture_output=True,
                check=False,
            )
        except OSError as exc:
            pytest.skip(f"directory junction unavailable: {exc}")
        if result.returncode != 0 or not link.exists():
            pytest.skip("directory junction unavailable")
        return

    try:
        link.symlink_to(target, target_is_directory=True)
    except (NotImplementedError, OSError) as exc:
        pytest.skip(f"directory symlink unavailable: {exc}")


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
        assert prompts[0].uid == managed_uid
        assert profiles[0].prompt_id == prompts[0].id

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
        state_after_delete = await _state_snapshot(setup_session_factory)
        assert all(row[1] != managed_uid for row in state_after_delete[0])
        assert all(row[1] != managed_uid for row in state_after_delete[1])
        assert all(row[1] != managed_uid for row in state_after_delete[2])
        assert tuple(row for row in state_after_delete[1] if row[1] != managed_uid) == initial_state[1]
        assert tuple(row for row in state_after_delete[2] if row[1] != managed_uid) == initial_state[2]

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


DELETE_DEPENDENCY_CASES = (
    "own-session",
    "foreign-session",
    "foreign-override",
    "platform",
    "scheduled",
    "user-kb",
    "managed-kb",
    "foreign-profile",
    "ownerless-profile",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("dependency", DELETE_DEPENDENCY_CASES)
async def test_superuser_deletes_user_data_and_unlinks_foreign_dependencies(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    dependency: str,
) -> None:
    prompt = PromptLibrary(uid="normal-user", name="default", content="default prompt")
    db_session.add(prompt)
    await db_session.flush()
    prompt_id = prompt.id

    profile = Profile(
        uid="normal-user",
        name="default",
        prompt_id=prompt_id,
        configs={},
        is_default=True,
    )
    db_session.add(profile)
    await db_session.flush()
    profile_id = profile.id

    session_id = f"delete-dependency-{dependency}"
    platform_name = f"delete-dependency-{dependency}"
    task_name = f"delete-dependency-{dependency}"
    knowledge_base_name = f"delete-dependency-{dependency}"
    knowledge_base_collection = f"delete-dependency-{dependency}"

    if dependency == "own-session":
        db_session.add(ChatSession(session_id=session_id, uid="normal-user"))
    elif dependency == "foreign-session":
        db_session.add(ChatSession(session_id=session_id, uid="peer-user", profile_id=profile_id))
    elif dependency == "foreign-override":
        db_session.add(ChatSession(session_id=session_id, uid="peer-user", profile_override_id=profile_id))
    elif dependency == "platform":
        db_session.add(
            MessagePlatform(
                name=platform_name,
                platform_type="WEIXIN_OPENCLAW",
                uid="normal-user",
                profile_id=profile_id,
            )
        )
    elif dependency == "scheduled":
        chat_session = ChatSession(session_id=session_id, uid="peer-user")
        db_session.add(chat_session)
        await db_session.flush()
        db_session.add(
            ScheduledTask(
                name=task_name,
                uid="peer-user",
                session_id=session_id,
                profile_id=profile_id,
                message="scheduled dependency",
                interval_seconds=60,
            )
        )
    elif dependency in {"user-kb", "managed-kb"}:
        channel = ModelChannel(name=f"{dependency}-channel", api_key="test-api-key")
        db_session.add(channel)
        await db_session.flush()
        db_session.add(
            KnowledgeBase(
                uid="normal-user",
                name=knowledge_base_name,
                embedding_channel_id=channel.id,
                embedding_model_id="test-embedding-model",
                collection_name=knowledge_base_collection,
                knowledge_base_type=KnowledgeBaseType.USER if dependency == "user-kb" else KnowledgeBaseType.LLM_MANAGED,
                managed_profile_id=profile_id if dependency == "managed-kb" else None,
            )
        )
    elif dependency == "foreign-profile":
        db_session.add(Profile(uid="peer-user", name="foreign", prompt_id=prompt_id, configs={}))
    else:
        db_session.add(Profile(uid=None, name="ownerless", prompt_id=prompt_id, configs={}))

    await db_session.commit()
    before = await _state_snapshot(setup_session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    assert payload["data"] is None

    after = await _state_snapshot(setup_session_factory)
    assert all(row[1] != "normal-user" for rows in after for row in rows)
    assert tuple(row for row in after[0] if row[1] != "normal-user") == tuple(row for row in before[0] if row[1] != "normal-user")
    assert tuple(row for row in after[2] if row[1] != "normal-user") == tuple(row for row in before[2] if row[1] != "normal-user")
    expected_profiles = tuple((*row[:3], None if dependency in {"foreign-profile", "ownerless-profile"} else row[3], *row[4:]) for row in before[1] if row[1] != "normal-user")
    assert tuple(row for row in after[1] if row[1] != "normal-user") == expected_profiles

    async with setup_session_factory() as session:
        if dependency == "own-session":
            result = await session.execute(select(ChatSession).where(ChatSession.session_id == session_id))
            assert result.scalars().first() is None
        elif dependency in {"foreign-session", "foreign-override"}:
            result = await session.execute(select(ChatSession).where(ChatSession.session_id == session_id))
            persisted_session = result.scalars().one()
            assert persisted_session.uid == "peer-user"
            assert persisted_session.profile_id is None
            assert persisted_session.profile_override_id is None
        elif dependency == "platform":
            result = await session.execute(select(MessagePlatform).where(MessagePlatform.name == platform_name))
            assert result.scalars().first() is None
        elif dependency == "scheduled":
            result = await session.execute(select(ScheduledTask).where(ScheduledTask.name == task_name))
            persisted_task = result.scalars().one()
            assert persisted_task.uid == "peer-user"
            assert persisted_task.session_id == session_id
            assert persisted_task.message == "scheduled dependency"
            assert persisted_task.profile_id is None
        elif dependency in {"user-kb", "managed-kb"}:
            result = await session.execute(select(KnowledgeBase).where(KnowledgeBase.name == knowledge_base_name))
            assert result.scalars().first() is None
            result = await session.execute(select(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name == knowledge_base_collection))
            persisted_collection_owner = result.scalars().one()
            assert persisted_collection_owner.knowledge_base_id is None
        elif dependency == "foreign-profile":
            result = await session.execute(select(Profile).where(Profile.uid == "peer-user", Profile.name == "foreign"))
            assert result.scalars().one().prompt_id is None
        else:
            result = await session.execute(select(Profile).where(Profile.uid.is_(None), Profile.name == "ownerless"))
            assert result.scalars().one().prompt_id is None


@pytest.mark.asyncio
async def test_user_delete_rolls_back_configuration_cleanup_when_user_delete_fails(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    prompt = PromptLibrary(uid="normal-user", name="default", content="default prompt")
    db_session.add(prompt)
    await db_session.flush()
    profile = Profile(
        uid="normal-user",
        name="default",
        prompt_id=prompt.id,
        configs={},
        is_default=True,
    )
    db_session.add(profile)
    shared_channel_name = "rollback-user-shared-channel"
    knowledge_base_collection = "rollback-user-kb"
    memory_collection = "rollback-user-memory"
    embedding_model_id = "rollback-embedding-model"
    shared_channel = ModelChannel(name=shared_channel_name, api_key="test-api-key")
    db_session.add(shared_channel)
    await db_session.flush()
    assert shared_channel.id is not None
    shared_channel_id = shared_channel.id
    db_session.add_all(
        [
            KnowledgeBase(
                uid="normal-user",
                name="rollback-user-kb",
                embedding_channel_id=shared_channel_id,
                embedding_model_id=embedding_model_id,
                collection_name=knowledge_base_collection,
                knowledge_base_type=KnowledgeBaseType.USER,
            ),
            LongTermMemoryStore(
                uid="normal-user",
                active_embedding_channel_id=shared_channel_id,
                active_embedding_model_id=embedding_model_id,
                active_embedding_revision=1,
                active_collection_name=memory_collection,
            ),
        ]
    )
    await db_session.commit()

    normal_user = await _get_user(setup_session_factory, "normal-user")
    assert normal_user is not None
    assert normal_user.id is not None
    normal_user_id = normal_user.id
    before = await _state_snapshot(setup_session_factory)
    async with setup_session_factory() as session:
        owner_result = await session.execute(select(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name.in_((knowledge_base_collection, memory_collection))).order_by(KnowledgeBaseCollectionOwner.collection_name))
        owner_baseline = tuple(
            (
                owner.collection_name,
                owner.knowledge_base_id,
                owner.cleanup_revision,
                owner.cleanup_attempt_count,
                owner.cleanup_error,
            )
            for owner in owner_result.scalars().all()
        )
    original_remove = user_crud.remove
    cleanup_observed = False

    async def failing_remove(db: AsyncSession, *, id: int) -> Any:
        nonlocal cleanup_observed
        if id == normal_user_id:
            profiles = (await db.execute(select(Profile).where(Profile.uid == "normal-user"))).scalars().all()
            prompts = (await db.execute(select(PromptLibrary).where(PromptLibrary.uid == "normal-user"))).scalars().all()
            knowledge_bases = (await db.execute(select(KnowledgeBase).where(KnowledgeBase.uid == "normal-user"))).scalars().all()
            memory_stores = (await db.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == "normal-user"))).scalars().all()
            cleanup_observed = not profiles and not prompts and not knowledge_bases and not memory_stores
            assert cleanup_observed
            raise RuntimeError("injected user delete failure")
        return await original_remove(db, id=id)

    monkeypatch.setattr(user_crud, "remove", failing_remove)
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        failed = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
        assert failed.status_code == 500
        assert cleanup_observed
        assert await _state_snapshot(setup_session_factory) == before

        async with setup_session_factory() as session:
            knowledge_base_result = await session.execute(select(KnowledgeBase).where(KnowledgeBase.uid == "normal-user", KnowledgeBase.collection_name == knowledge_base_collection))
            persisted_knowledge_base = knowledge_base_result.scalars().one()
            assert (
                persisted_knowledge_base.collection_name,
                persisted_knowledge_base.embedding_channel_id,
                persisted_knowledge_base.embedding_model_id,
            ) == (knowledge_base_collection, shared_channel_id, embedding_model_id)

            memory_store_result = await session.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == "normal-user", LongTermMemoryStore.active_collection_name == memory_collection))
            persisted_memory_store = memory_store_result.scalars().one()
            assert (
                persisted_memory_store.active_collection_name,
                persisted_memory_store.active_embedding_channel_id,
                persisted_memory_store.active_embedding_model_id,
            ) == (memory_collection, shared_channel_id, embedding_model_id)

            owner_result = await session.execute(select(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name.in_((knowledge_base_collection, memory_collection))).order_by(KnowledgeBaseCollectionOwner.collection_name))
            owner_snapshot = tuple(
                (
                    owner.collection_name,
                    owner.knowledge_base_id,
                    owner.cleanup_revision,
                    owner.cleanup_attempt_count,
                    owner.cleanup_error,
                )
                for owner in owner_result.scalars().all()
            )
            assert owner_snapshot == owner_baseline

        monkeypatch.setattr(user_crud, "remove", original_remove)
        deleted = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
        assert deleted.status_code == 200
        assert deleted.json()["code"] == 200
        assert deleted.json()["data"] is None

    after = await _state_snapshot(setup_session_factory)
    assert all(row[1] != "normal-user" for rows in after for row in rows)
    for after_rows, before_rows in zip(after, before):
        assert tuple(row for row in after_rows if row[1] != "normal-user") == tuple(row for row in before_rows if row[1] != "normal-user")

    async with setup_session_factory() as session:
        knowledge_base_result = await session.execute(select(KnowledgeBase).where(KnowledgeBase.collection_name == knowledge_base_collection))
        assert knowledge_base_result.scalars().first() is None
        memory_store_result = await session.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.active_collection_name == memory_collection))
        assert memory_store_result.scalars().first() is None

        owner_result = await session.execute(select(KnowledgeBaseCollectionOwner).where(KnowledgeBaseCollectionOwner.collection_name.in_((knowledge_base_collection, memory_collection))).order_by(KnowledgeBaseCollectionOwner.collection_name))
        owners = owner_result.scalars().all()
        assert {owner.collection_name for owner in owners} == {knowledge_base_collection, memory_collection}
        assert all(owner.knowledge_base_id is None for owner in owners)

        shared_channel_result = await session.execute(select(ModelChannel).where(ModelChannel.name == shared_channel_name))
        assert shared_channel_result.scalars().one().name == shared_channel_name


@pytest.mark.asyncio
async def test_superuser_delete_clears_long_term_memory_and_registers_collections_without_affecting_peer(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    shared_channel_name = "delete-memory-shared-channel"
    normal_active_collection = "delete-memory-normal-active"
    normal_target_collection = "delete-memory-normal-target"
    normal_old_collection = "delete-memory-normal-old"
    normal_history_collection = "delete-memory-normal-history"
    peer_active_collection = "delete-memory-peer-active"
    peer_target_collection = "delete-memory-peer-target"
    peer_old_collection = "delete-memory-peer-old"
    peer_history_collection = "delete-memory-peer-history"

    shared_channel = ModelChannel(name=shared_channel_name, api_key="test-api-key")
    db_session.add(shared_channel)
    await db_session.flush()
    assert shared_channel.id is not None

    normal_store = LongTermMemoryStore(
        uid="normal-user",
        active_embedding_channel_id=shared_channel.id,
        active_embedding_model_id="shared-embedding-model",
        active_embedding_revision=1,
        active_collection_name=normal_active_collection,
        target_embedding_channel_id=shared_channel.id,
        target_embedding_model_id="shared-embedding-model",
        target_collection_name=normal_target_collection,
        old_collection_name=normal_old_collection,
    )
    peer_store = LongTermMemoryStore(
        uid="peer-user",
        active_embedding_channel_id=shared_channel.id,
        active_embedding_model_id="shared-embedding-model",
        active_embedding_revision=1,
        active_collection_name=peer_active_collection,
        target_embedding_channel_id=shared_channel.id,
        target_embedding_model_id="shared-embedding-model",
        target_collection_name=peer_target_collection,
        old_collection_name=peer_old_collection,
    )
    normal_record = LongTermMemoryRecord(uid="normal-user", content="normal memory content", is_active=True, version=1)
    peer_record = LongTermMemoryRecord(uid="peer-user", content="peer memory content", is_active=True, version=1)
    db_session.add_all([normal_store, peer_store, normal_record, peer_record])
    await db_session.flush()
    assert normal_record.id is not None and peer_record.id is not None

    normal_job = LongTermMemoryMutationJob(uid="normal-user", operation="create", dedupe_key="normal-memory-create")
    peer_job = LongTermMemoryMutationJob(uid="peer-user", operation="create", dedupe_key="peer-memory-create")
    db_session.add_all(
        [
            LongTermMemoryEmbeddingRevision(
                uid="normal-user",
                revision=1,
                from_collection=normal_history_collection,
                to_collection=normal_active_collection,
            ),
            LongTermMemoryEmbeddingRevision(
                uid="peer-user",
                revision=1,
                from_collection=peer_history_collection,
                to_collection=peer_active_collection,
            ),
            LongTermMemoryRevision(
                uid="normal-user",
                memory_id=normal_record.id,
                version=1,
                content="normal memory history",
            ),
            LongTermMemoryRevision(
                uid="peer-user",
                memory_id=peer_record.id,
                version=1,
                content="peer memory history",
            ),
            normal_job,
            peer_job,
        ]
    )
    await db_session.flush()
    assert normal_job.id is not None and peer_job.id is not None
    db_session.add_all(
        [
            LongTermMemoryEmbeddingDelta(uid="normal-user", migration_job_id=normal_job.id, sequence=1),
            LongTermMemoryEmbeddingDelta(uid="peer-user", migration_job_id=peer_job.id, sequence=1),
        ]
    )
    await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert response.status_code == 200
    assert response.json()["data"] is None

    memory_models = (
        LongTermMemoryStore,
        LongTermMemoryEmbeddingRevision,
        LongTermMemoryRecord,
        LongTermMemoryRevision,
        LongTermMemoryMutationJob,
        LongTermMemoryEmbeddingDelta,
    )
    async with setup_session_factory() as session:
        for model in memory_models:
            normal_count = await session.scalar(select(func.count()).select_from(model).where(model.uid == "normal-user"))
            peer_count = await session.scalar(select(func.count()).select_from(model).where(model.uid == "peer-user"))
            assert normal_count == 0
            assert peer_count == 1

        peer_store_result = await session.execute(select(LongTermMemoryStore).where(LongTermMemoryStore.uid == "peer-user"))
        persisted_peer_store = peer_store_result.scalars().one()
        assert (
            persisted_peer_store.active_collection_name,
            persisted_peer_store.target_collection_name,
            persisted_peer_store.old_collection_name,
        ) == (peer_active_collection, peer_target_collection, peer_old_collection)

        peer_record_result = await session.execute(select(LongTermMemoryRecord).where(LongTermMemoryRecord.uid == "peer-user"))
        persisted_peer_record = peer_record_result.scalars().one()
        assert persisted_peer_record.content == "peer memory content"

        peer_revision_result = await session.execute(select(LongTermMemoryRevision).where(LongTermMemoryRevision.uid == "peer-user"))
        persisted_peer_revision = peer_revision_result.scalars().one()
        assert persisted_peer_revision.memory_id == persisted_peer_record.id
        assert persisted_peer_revision.version == 1
        assert persisted_peer_revision.content == "peer memory history"

        peer_embedding_result = await session.execute(select(LongTermMemoryEmbeddingRevision).where(LongTermMemoryEmbeddingRevision.uid == "peer-user"))
        persisted_peer_embedding = peer_embedding_result.scalars().one()
        assert persisted_peer_embedding.revision == 1
        assert persisted_peer_embedding.from_collection == peer_history_collection
        assert persisted_peer_embedding.to_collection == peer_active_collection

        peer_job_result = await session.execute(select(LongTermMemoryMutationJob).where(LongTermMemoryMutationJob.uid == "peer-user"))
        persisted_peer_job = peer_job_result.scalars().one()
        assert persisted_peer_job.operation == "create"
        assert persisted_peer_job.dedupe_key == "peer-memory-create"

        peer_delta_result = await session.execute(select(LongTermMemoryEmbeddingDelta).where(LongTermMemoryEmbeddingDelta.uid == "peer-user"))
        persisted_peer_delta = peer_delta_result.scalars().one()
        assert persisted_peer_delta.migration_job_id == persisted_peer_job.id
        assert persisted_peer_delta.sequence == 1

        owner_result = await session.execute(select(KnowledgeBaseCollectionOwner).order_by(KnowledgeBaseCollectionOwner.collection_name))
        owners = owner_result.scalars().all()
        assert {owner.collection_name for owner in owners} == {
            normal_active_collection,
            normal_target_collection,
            normal_old_collection,
            normal_history_collection,
        }
        assert all(owner.knowledge_base_id is None for owner in owners)
        assert all(owner.collection_name not in {peer_active_collection, peer_target_collection, peer_old_collection, peer_history_collection} for owner in owners)

        shared_channel_result = await session.execute(select(ModelChannel).where(ModelChannel.name == shared_channel_name))
        assert shared_channel_result.scalars().one().name == shared_channel_name


@pytest.mark.asyncio
async def test_superuser_delete_clears_complete_session_task_subtree_without_affecting_peer(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    seeded_records: dict[str, dict[str, Any]] = {}
    for uid in ("normal-user", "peer-user"):
        prompt = PromptLibrary(uid=uid, name="default", content=f"{uid} private prompt")
        db_session.add(prompt)
        await db_session.flush()

        profile = Profile(uid=uid, name="default", prompt_id=prompt.id, configs={}, is_default=True)
        db_session.add(profile)
        await db_session.flush()

        session_id = f"delete-subtree-{uid}"
        task_name = f"{uid} scheduled task"
        chat_session = ChatSession(session_id=session_id, uid=uid, profile_id=profile.id)
        db_session.add(chat_session)
        await db_session.flush()

        db_session.add_all(
            [
                Message(session_id=session_id, uid=uid, role="user", content=f"{uid} message", profile_id=profile.id),
                ScheduledTask(
                    name=task_name,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile.id,
                    message=f"{uid} scheduled message",
                    interval_seconds=60,
                ),
                MessagePlatform(
                    name=f"{uid} platform",
                    platform_type="WEIXIN_OPENCLAW",
                    uid=uid,
                    profile_id=profile.id,
                ),
                BackgroundTask(
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile.id,
                    tool_call_id=f"{uid}-tool-call",
                    tool_name=f"{uid}-tool",
                    status="pending",
                ),
                SessionEvent(
                    dedupe_key=f"{uid}-event",
                    uid=uid,
                    session_id=session_id,
                    event={"uid": uid},
                ),
                SessionTodoPlan(session_id=session_id, uid=uid, todos=[{"todo": f"{uid} todo"}]),
                MessagePlatformOutbox(
                    dedupe_key=f"{uid}-outbox",
                    uid=uid,
                    session_id=session_id,
                    source=f"{uid}-source",
                    event={"uid": uid},
                ),
            ]
        )
        seeded_records[uid] = {
            "profile_id": profile.id,
            "prompt_id": prompt.id,
            "session_id": session_id,
            "task_name": task_name,
        }

    for index in range(104):
        db_session.add(ChatSession(session_id=f"delete-subtree-normal-extra-{index}", uid="normal-user"))
    await db_session.flush()
    await db_session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert response.status_code == 200
    assert response.json()["data"] is None

    owned_models = (
        Message,
        ScheduledTask,
        MessagePlatform,
        BackgroundTask,
        SessionEvent,
        SessionTodoPlan,
        MessagePlatformOutbox,
        Profile,
        PromptLibrary,
    )
    async with setup_session_factory() as session:
        for model in owned_models:
            normal_count = await session.scalar(select(func.count()).select_from(model).where(model.uid == "normal-user"))
            peer_count = await session.scalar(select(func.count()).select_from(model).where(model.uid == "peer-user"))
            assert normal_count == 0
            assert peer_count == 1

        normal_session_count = await session.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.uid == "normal-user"))
        peer_session_count = await session.scalar(select(func.count()).select_from(ChatSession).where(ChatSession.uid == "peer-user"))
        assert normal_session_count == 0
        assert peer_session_count == 1

        peer_message_result = await session.execute(select(Message).where(Message.uid == "peer-user"))
        peer_message = peer_message_result.scalars().one()
        assert peer_message.content == "peer-user message"

        peer_task_result = await session.execute(select(ScheduledTask).where(ScheduledTask.uid == "peer-user"))
        peer_task = peer_task_result.scalars().one()
        assert peer_task.name == seeded_records["peer-user"]["task_name"]

        peer_user_result = await session.execute(select(User).where(User.uid == "peer-user"))
        assert peer_user_result.scalars().one().uid == "peer-user"


@pytest.mark.asyncio
async def test_user_delete_invalidates_old_background_memory_and_knowledge_job_leases(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    worker_id = "deletion-worker"
    uids = ("normal-user", "peer-user")

    channel = ModelChannel(name="delete-lease-channel", api_key="test-api-key")
    db_session.add(channel)
    await db_session.flush()
    assert channel.id is not None

    profiles = {uid: Profile(uid=uid, name="delete-lease", configs={}) for uid in uids}
    db_session.add_all(list(profiles.values()))
    await db_session.flush()

    chat_sessions = {uid: ChatSession(session_id=f"delete-lease-{uid}", uid=uid, profile_id=profiles[uid].id) for uid in uids}
    knowledge_bases = {
        uid: KnowledgeBase(
            uid=uid,
            name=f"delete-lease-{uid}",
            embedding_channel_id=channel.id,
            embedding_model_id="delete-lease-embedding-model",
            collection_name=f"delete-lease-{uid}-collection",
            knowledge_base_type=KnowledgeBaseType.USER,
        )
        for uid in uids
    }
    db_session.add_all([*chat_sessions.values(), *knowledge_bases.values()])
    await db_session.flush()
    for uid in uids:
        assert profiles[uid].id is not None
        assert knowledge_bases[uid].id is not None

    lease_until = await get_database_time(db_session) + timedelta(hours=1)
    background_lock_until = await get_database_timestamp(db_session) + 3600
    background_tasks = {
        uid: BackgroundTask(
            uid=uid,
            session_id=chat_sessions[uid].session_id,
            profile_id=profiles[uid].id,
            tool_call_id=f"{uid}-delete-lease-tool-call",
            tool_name="delete-lease-tool",
            status=BackgroundTaskStatus.RUNNING,
            locked_by=worker_id,
            lock_until=background_lock_until,
            attempt_count=1,
            started_at=lease_until - timedelta(hours=1),
        )
        for uid in uids
    }
    memory_jobs = {
        uid: LongTermMemoryMutationJob(
            uid=uid,
            operation=LongTermMemoryMutationOperation.CREATE,
            dedupe_key=f"{uid}-delete-lease-memory",
            status=LongTermMemoryMutationStatus.RUNNING,
            locked_by=worker_id,
            lock_until=lease_until,
            attempt_count=1,
            started_at=lease_until - timedelta(hours=1),
        )
        for uid in uids
    }
    knowledge_jobs = {
        uid: KnowledgeJob(
            uid=uid,
            operation=KnowledgeJobOperation.MANAGED_CREATE,
            dedupe_key=f"{uid}-delete-lease-knowledge",
            request_hash="a" * 64,
            status=KnowledgeJobStatus.RUNNING,
            knowledge_base_id=knowledge_bases[uid].id,
            locked_by=worker_id,
            lock_until=lease_until,
            attempt_count=1,
            started_at=lease_until - timedelta(hours=1),
        )
        for uid in uids
    }
    db_session.add_all([*background_tasks.values(), *memory_jobs.values(), *knowledge_jobs.values()])
    await db_session.commit()

    normal_background_task_id = background_tasks["normal-user"].id
    peer_background_task_id = background_tasks["peer-user"].id
    normal_memory_job = memory_jobs["normal-user"]
    peer_memory_job = memory_jobs["peer-user"]
    normal_knowledge_job = knowledge_jobs["normal-user"]
    peer_knowledge_job = knowledge_jobs["peer-user"]
    assert normal_background_task_id is not None
    assert peer_background_task_id is not None
    assert normal_memory_job.id is not None
    assert peer_memory_job.id is not None
    assert normal_knowledge_job.id is not None
    assert peer_knowledge_job.id is not None

    normal_memory_context = MemoryJobExecutionContext(
        job=normal_memory_job,
        worker_id=worker_id,
        session_factory=setup_session_factory,
    )
    peer_memory_context = MemoryJobExecutionContext(
        job=peer_memory_job,
        worker_id=worker_id,
        session_factory=setup_session_factory,
    )
    normal_knowledge_context = KnowledgeJobExecutionContext(
        job=normal_knowledge_job,
        worker_id=worker_id,
        session_factory=setup_session_factory,
    )
    peer_knowledge_context = KnowledgeJobExecutionContext(
        job=peer_knowledge_job,
        worker_id=worker_id,
        session_factory=setup_session_factory,
    )

    assert (await normal_memory_context.checkpoint()).id == normal_memory_job.id
    assert (await peer_memory_context.checkpoint()).id == peer_memory_job.id
    assert (await normal_knowledge_context.checkpoint()).id == normal_knowledge_job.id
    assert (await peer_knowledge_context.checkpoint()).id == peer_knowledge_job.id

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert response.status_code == 200
    assert response.json()["code"] == 200

    with pytest.raises(MemoryJobLeaseLostError):
        await normal_memory_context.checkpoint()
    with pytest.raises(KnowledgeJobLeaseLostError):
        await normal_knowledge_context.checkpoint()
    assert (await peer_memory_context.checkpoint()).id == peer_memory_job.id
    assert (await peer_knowledge_context.checkpoint()).id == peer_knowledge_job.id

    async with setup_session_factory() as session:
        assert await background_task_crud.renew_lease(session, task_id=normal_background_task_id, worker_id=worker_id) is False
        assert (
            await background_task_crud.mark_succeeded(
                session,
                task_id=normal_background_task_id,
                worker_id=worker_id,
                result={},
                auto_reply=True,
            )
            is False
        )
        assert await background_task_crud.renew_lease(session, task_id=peer_background_task_id, worker_id=worker_id) is True

    async with setup_session_factory() as session:
        assert (
            await memory_job_crud.mark_succeeded(
                session,
                uid="normal-user",
                job_id=normal_memory_job.id,
                worker_id=worker_id,
                result={},
            )
            is False
        )

    async with setup_session_factory() as session:
        assert (
            await knowledge_job_crud.mark_succeeded(
                session,
                uid="normal-user",
                job_id=normal_knowledge_job.id,
                owner=worker_id,
                result={},
            )
            is False
        )

    async with setup_session_factory() as session:
        for model in (BackgroundTask, LongTermMemoryMutationJob, KnowledgeJob):
            normal_count = await session.scalar(select(func.count()).select_from(model).where(model.uid == "normal-user"))
            peer_count = await session.scalar(select(func.count()).select_from(model).where(model.uid == "peer-user"))
            assert normal_count == 0
            assert peer_count == 1

        normal_user_result = await session.execute(select(User).where(User.uid == "normal-user"))
        peer_user_result = await session.execute(select(User).where(User.uid == "peer-user"))
        assert normal_user_result.scalars().first() is None
        assert peer_user_result.scalars().one().uid == "peer-user"


@pytest.mark.asyncio
@pytest.mark.parametrize("fail_delete", (False, True), ids=("delete-succeeds", "delete-rolls-back"))
async def test_user_delete_audit_records_and_startup_cleanup_boundary(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fail_delete: bool,
) -> None:
    audit_root = tmp_path / "audit"
    temp_paths: dict[str, Path] = {}
    for uid in ("normal-user", "peer-user"):
        temp_path = tmp_path / "temp" / f"temp_{uid}" / "nested" / "attachment.txt"
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path.write_text(uid, encoding="utf-8")
        temp_paths[uid] = temp_path

    context_paths: dict[str, Path] = {}
    for uid in ("normal-user", "peer-user"):
        audit_record = AuditRecord(
            uid=uid,
            operator_username=uid,
            session_id=f"audit-delete-{uid}",
            source="llm_tool_call",
            language="zh",
            status=AuditRecordStatus.SUCCEEDED,
            source_assistant_message_id=1,
            working_directory=str(tmp_path),
            round_arguments_hash="a" * 64,
            tool_count=1,
        )
        db_session.add(audit_record)
        await db_session.flush()
        assert audit_record.id is not None
        context_paths[uid] = await write_audit_json(
            uid=uid,
            audit_record_id=audit_record.id,
            payload={"uid": uid},
            audit_root=audit_root,
        )
        audit_record.context_file_path = str(context_paths[uid])
    await db_session.commit()

    normal_user = await _get_user(setup_session_factory, "normal-user")
    assert normal_user is not None
    assert normal_user.id is not None
    if fail_delete:
        original_remove = user_crud.remove
        normal_user_id = normal_user.id

        async def failing_remove(db: AsyncSession, *, id: int) -> Any:
            if id == normal_user_id:
                raise RuntimeError("injected user delete failure")
            return await original_remove(db, id=id)

        monkeypatch.setattr(user_crud, "remove", failing_remove)

    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert response.status_code == (500 if fail_delete else 200)
    if not fail_delete:
        assert response.json()["code"] == 200
        assert response.json()["data"] is None

    async with setup_session_factory() as session:
        normal_user = await session.scalar(select(User).where(User.uid == "normal-user"))
        peer_user = await session.scalar(select(User).where(User.uid == "peer-user"))
        normal_audit_record = await session.scalar(select(AuditRecord).where(AuditRecord.uid == "normal-user"))
        peer_audit_record = await session.scalar(select(AuditRecord).where(AuditRecord.uid == "peer-user"))

    if fail_delete:
        assert normal_user is not None
        assert normal_audit_record is not None
    else:
        assert normal_user is None
        assert normal_audit_record is None
    assert peer_user is not None
    assert peer_audit_record is not None

    for uid, audit_record in (("normal-user", normal_audit_record), ("peer-user", peer_audit_record)):
        if audit_record is None:
            continue
        assert audit_record.operator_username == uid
        assert audit_record.session_id == f"audit-delete-{uid}"
        assert audit_record.source == "llm_tool_call"
        assert audit_record.language == "zh"
        assert audit_record.status == AuditRecordStatus.SUCCEEDED
        assert audit_record.source_assistant_message_id == 1
        assert audit_record.working_directory == str(tmp_path)
        assert audit_record.round_arguments_hash == "a" * 64
        assert audit_record.tool_count == 1
        assert audit_record.context_file_path == str(context_paths[uid])

    assert context_paths["normal-user"].is_file() is fail_delete
    assert context_paths["peer-user"].is_file()
    assert temp_paths["normal-user"].is_file() is fail_delete
    assert temp_paths["peer-user"].is_file()

    async with setup_session_factory() as session:
        recovery = await recover_and_cleanup_audit_data(session, retention_days=90, audit_root=audit_root)
    expected_deleted_files = set()
    assert set(recovery.file_cleanup.deleted_files) == expected_deleted_files
    assert recovery.file_cleanup.failed_paths == {}
    assert recovery.file_cleanup.missing_referenced_files == []
    assert recovery.deleted_database_records == 0
    assert context_paths["normal-user"].is_file() is fail_delete
    assert context_paths["peer-user"].is_file()

    async with setup_session_factory() as session:
        second_recovery = await recover_and_cleanup_audit_data(session, retention_days=90, audit_root=audit_root)
    assert second_recovery.file_cleanup.deleted_files == []
    assert second_recovery.file_cleanup.failed_paths == {}
    assert second_recovery.file_cleanup.missing_referenced_files == []
    assert second_recovery.deleted_database_records == 0

    async with setup_session_factory() as session:
        peer_audit_record = await session.scalar(select(AuditRecord).where(AuditRecord.uid == "peer-user"))
    assert peer_audit_record is not None
    assert peer_audit_record.status == AuditRecordStatus.SUCCEEDED
    assert peer_audit_record.context_file_path == str(context_paths["peer-user"])


@pytest.mark.asyncio
async def test_user_delete_removes_owned_and_legacy_temp_files_without_cross_user_or_traversal(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    legacy_session_id = "legacy-owned"
    peer_named_session_id = "peer-user"
    posix_escape_session_id = "../outside-posix"
    windows_escape_session_id = r"..\outside-windows"
    db_session.add_all(
        [
            ChatSession(session_id=legacy_session_id, uid="normal-user"),
            ChatSession(session_id=peer_named_session_id, uid="normal-user"),
            ChatSession(session_id=posix_escape_session_id, uid="normal-user"),
            ChatSession(session_id=windows_escape_session_id, uid="normal-user"),
        ]
    )
    await db_session.commit()

    temp_root = tmp_path / "temp"
    normal_file = temp_root / "temp_normal-user" / "nested" / "normal.txt"
    legacy_file = temp_root / f"temp_{legacy_session_id}" / "nested" / "legacy.txt"
    peer_file = temp_root / f"temp_{peer_named_session_id}" / "nested" / "peer.txt"
    escape_files = (
        tmp_path / "outside-posix" / "nested" / "posix.txt",
        tmp_path / "outside-windows" / "nested" / "windows.txt",
    )
    for path in (normal_file, legacy_file, peer_file, *escape_files):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert response.status_code == 200
    assert response.json()["code"] == 200
    assert not normal_file.exists()
    assert not legacy_file.exists()
    assert not normal_file.parents[1].exists()
    assert not legacy_file.parents[1].exists()
    assert peer_file.is_file()
    assert peer_file.parents[1].is_dir()
    assert all(path.is_file() for path in escape_files)

    async with setup_session_factory() as session:
        assert await session.scalar(select(User).where(User.uid == "normal-user")) is None
        assert await session.scalar(select(User).where(User.uid == "peer-user")) is not None
        normal_sessions = await session.scalars(select(ChatSession).where(ChatSession.uid == "normal-user"))
        assert normal_sessions.all() == []
        assert await session.scalar(select(ChatSession).where(ChatSession.session_id == peer_named_session_id)) is None
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user")) is None


@pytest.mark.asyncio
async def test_user_delete_retries_pending_file_cleanup_after_disk_failure(
    api_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    normal_file = tmp_path / "temp" / "temp_normal-user" / "nested" / "normal.txt"
    peer_file = tmp_path / "temp" / "temp_peer-user" / "nested" / "peer.txt"
    for path in (normal_file, peer_file):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(path.name, encoding="utf-8")

    original_remove_paths = file_cleanup._remove_user_file_cleanup_paths

    def failing_remove_paths(directory_names: list[str]) -> None:
        raise PermissionError(f"injected failure for {directory_names[0]}")

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", failing_remove_paths)
    transport = httpx.ASGITransport(app=api_app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        failed = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert failed.status_code == 500
    assert normal_file.is_file()
    assert peer_file.is_file()

    async with setup_session_factory() as session:
        assert await session.scalar(select(User).where(User.uid == "normal-user")) is None
        assert await session.scalar(select(User).where(User.uid == "peer-user")) is not None
        pending_cleanup = await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user"))
        assert pending_cleanup is not None

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", original_remove_paths)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        retried = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )
    assert retried.status_code == 200
    assert retried.json()["code"] == 200
    assert not normal_file.exists()
    assert peer_file.is_file()

    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user")) is None
        assert await session.scalar(select(User).where(User.uid == "peer-user")) is not None


@pytest.mark.asyncio
async def test_recover_user_file_cleanup_continues_after_one_disk_failure(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_uid = "deleted-first"
    second_uid = "deleted-second"
    db_session.add_all(
        [
            UserFileCleanup(uid=first_uid, legacy_session_ids=[]),
            UserFileCleanup(uid=second_uid, legacy_session_ids=[]),
        ]
    )
    await db_session.commit()

    file_paths = {uid: tmp_path / "temp" / f"temp_{uid}" / "nested" / "attachment.txt" for uid in (first_uid, second_uid)}
    for uid, path in file_paths.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(uid, encoding="utf-8")

    original_remove_paths = file_cleanup._remove_user_file_cleanup_paths

    def fail_first_remove(directory_names: list[str]) -> None:
        if directory_names[0] == first_uid:
            raise PermissionError(f"injected failure for {first_uid}")
        original_remove_paths(directory_names)

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", fail_first_remove)
    assert await recover_user_file_cleanup(db_session) == 1
    assert file_paths[first_uid].is_file()
    assert not file_paths[second_uid].exists()

    async with setup_session_factory() as session:
        pending = await session.scalars(select(UserFileCleanup).order_by(UserFileCleanup.uid))
        assert [cleanup.uid for cleanup in pending.all()] == [first_uid]

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", original_remove_paths)
    assert await recover_user_file_cleanup(db_session) == 1
    assert not file_paths[first_uid].exists()
    assert await recover_user_file_cleanup(db_session) == 0

    async with setup_session_factory() as session:
        pending = await session.scalars(select(UserFileCleanup))
        assert pending.all() == []


@pytest.mark.asyncio
async def test_file_cleanup_boundary_keeps_pending_record_for_existing_user(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    file_path = tmp_path / "temp" / "temp_normal-user" / "nested" / "attachment.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"existing-user-content")
    db_session.add(UserFileCleanup(uid="normal-user", legacy_session_ids=[]))
    await db_session.commit()

    assert await recover_user_file_cleanup(db_session) == 0
    assert file_path.read_bytes() == b"existing-user-content"

    async with setup_session_factory() as session:
        pending = await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user"))
        assert pending is not None
        assert pending.legacy_session_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "session_id",
    (
        "deleted-file-owner",
        pytest.param(
            "DELETED-FILE-OWNER",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows path aliases only"),
        ),
    ),
    ids=("exact-session-reuse", "uppercase-session-alias"),
)
async def test_file_cleanup_boundary_preserves_main_directory_reused_by_peer_session(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    session_id: str,
) -> None:
    deleted_uid = "deleted-file-owner"
    db_session.add_all(
        [
            ChatSession(session_id=session_id, uid="peer-user"),
            UserFileCleanup(uid=deleted_uid, legacy_session_ids=[]),
        ]
    )
    await db_session.commit()

    reused_file = tmp_path / "temp" / f"temp_{deleted_uid}" / "nested" / "reused.txt"
    reused_file.parent.mkdir(parents=True, exist_ok=True)
    reused_file.write_bytes(b"reused-content")

    assert await recover_user_file_cleanup(db_session) == 0
    assert reused_file.read_bytes() == b"reused-content"

    async with setup_session_factory() as session:
        pending = await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == deleted_uid))
        assert pending is not None
        assert pending.legacy_session_ids == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("legacy_session_id", "current_session_id", "session_owner"),
    (
        ("reused-session", "reused-session", "peer-user"),
        ("peer-user", None, None),
        pytest.param(
            "REUSED-SESSION",
            "reused-session",
            "peer-user",
            marks=pytest.mark.skipif(sys.platform != "win32", reason="Windows path aliases only"),
        ),
    ),
    ids=("peer-session-reuse", "active-user-uid", "windows-case-alias"),
)
async def test_file_cleanup_boundary_preserves_reused_legacy_directory(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    legacy_session_id: str,
    current_session_id: str | None,
    session_owner: str | None,
) -> None:
    deleted_uid = "deleted-file-owner"
    if current_session_id is not None:
        assert session_owner is not None
        db_session.add(ChatSession(session_id=current_session_id, uid=session_owner))
    db_session.add(UserFileCleanup(uid=deleted_uid, legacy_session_ids=[legacy_session_id]))
    await db_session.commit()

    deleted_file = tmp_path / "temp" / f"temp_{deleted_uid}" / "nested" / "deleted.txt"
    reused_file = tmp_path / "temp" / f"temp_{legacy_session_id}" / "nested" / "reused.txt"
    for path, content in ((deleted_file, b"deleted-content"), (reused_file, b"reused-content")):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    assert await recover_user_file_cleanup(db_session) == 1
    assert not deleted_file.exists()
    assert reused_file.read_bytes() == b"reused-content"

    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == deleted_uid)) is None


@pytest.mark.asyncio
@pytest.mark.skipif(sys.platform != "win32", reason="Windows path aliases only")
@pytest.mark.parametrize("session_id", ("peer-user.", "peer-user ", "PEER-USER"))
async def test_file_cleanup_boundary_windows_alias_does_not_remove_peer_files(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    session_id: str,
) -> None:
    db_session.add(ChatSession(session_id=session_id, uid="normal-user"))
    await db_session.commit()

    normal_file = tmp_path / "temp" / "temp_normal-user" / "nested" / "normal.txt"
    peer_file = tmp_path / "temp" / "temp_peer-user" / "nested" / "peer.txt"
    normal_file.parent.mkdir(parents=True, exist_ok=True)
    peer_file.parent.mkdir(parents=True, exist_ok=True)
    normal_file.write_bytes(b"normal-content")
    peer_file.write_bytes(b"peer-content")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await _request(
            client,
            "POST",
            ADMIN_DELETE,
            token=_token("super-admin"),
            params={"uid": "normal-user"},
        )

    assert response.status_code == 200
    assert response.json()["code"] == 200
    assert not normal_file.exists()
    assert peer_file.read_bytes() == b"peer-content"

    async with setup_session_factory() as session:
        assert await session.scalar(select(User).where(User.uid == "normal-user")) is None
        assert await session.scalar(select(User).where(User.uid == "peer-user")) is not None
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == "normal-user")) is None


@pytest.mark.asyncio
async def test_file_cleanup_boundary_removes_user_directory_link_without_following_target(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    uid = "deleted-directory-link"
    target_file = tmp_path / "outside-user-directory" / "nested" / "target.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(b"target-content")
    user_link = tmp_path / "temp" / f"temp_{uid}"
    _create_directory_link(user_link, target_file.parents[1])

    db_session.add(UserFileCleanup(uid=uid, legacy_session_ids=[]))
    await db_session.commit()
    assert await recover_user_file_cleanup(db_session) == 1

    assert not user_link.exists()
    assert target_file.read_bytes() == b"target-content"
    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == uid)) is None


@pytest.mark.asyncio
async def test_file_cleanup_boundary_does_not_traverse_internal_directory_link(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    uid = "deleted-internal-link"
    user_directory = tmp_path / "temp" / f"temp_{uid}"
    owned_file = user_directory / "owned.txt"
    owned_file.parent.mkdir(parents=True, exist_ok=True)
    owned_file.write_bytes(b"owned-content")
    target_file = tmp_path / "outside-internal-directory" / "target.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(b"target-content")
    _create_directory_link(user_directory / "linked-directory", target_file.parent)

    db_session.add(UserFileCleanup(uid=uid, legacy_session_ids=[]))
    await db_session.commit()
    assert await recover_user_file_cleanup(db_session) == 1

    assert not owned_file.exists()
    assert target_file.read_bytes() == b"target-content"
    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == uid)) is None


@pytest.mark.asyncio
async def test_file_cleanup_boundary_rejects_linked_temp_root_and_keeps_pending_cleanup(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
) -> None:
    uid = "linked-temp-root-user"
    target_file = tmp_path / "outside-temp-root" / f"temp_{uid}" / "nested" / "target.txt"
    target_file.parent.mkdir(parents=True, exist_ok=True)
    target_file.write_bytes(b"target-content")
    temp_link = tmp_path / "temp"
    _create_directory_link(temp_link, target_file.parents[2])

    db_session.add(UserFileCleanup(uid=uid, legacy_session_ids=[]))
    await db_session.commit()
    assert await recover_user_file_cleanup(db_session) == 0

    assert temp_link.exists()
    assert target_file.read_bytes() == b"target-content"
    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == uid)) is not None


@pytest.mark.asyncio
async def test_file_cleanup_concurrent_cleanup_same_uid_is_idempotent(
    api_app: FastAPI,
    db_session: AsyncSession,
    setup_session_factory: async_sessionmaker[AsyncSession],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    uid = "concurrent-file-cleanup"
    file_path = tmp_path / "temp" / f"temp_{uid}" / "nested" / "attachment.txt"
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_bytes(b"concurrent-content")
    db_session.add(UserFileCleanup(uid=uid, legacy_session_ids=[]))
    await db_session.commit()

    original_remove_paths = file_cleanup._remove_user_file_cleanup_paths
    loop = asyncio.get_running_loop()
    first_entered = asyncio.Event()
    second_entered = asyncio.Event()
    release_first = threading.Event()
    entry_lock = threading.Lock()
    first_claimed = False

    def synchronized_remove(directory_names: list[str]) -> None:
        nonlocal first_claimed
        with entry_lock:
            is_first = not first_claimed
            if is_first:
                first_claimed = True
        loop.call_soon_threadsafe((first_entered if is_first else second_entered).set)
        if is_first and not release_first.wait(timeout=5):
            raise TimeoutError("timed out waiting to release first cleanup")
        original_remove_paths(directory_names)

    monkeypatch.setattr(file_cleanup, "_remove_user_file_cleanup_paths", synchronized_remove)
    async with setup_session_factory() as first_session, setup_session_factory() as second_session:
        first_task = asyncio.create_task(cleanup_user_files(first_session, uid=uid))
        second_task: asyncio.Task[bool] | None = None
        try:
            await asyncio.wait_for(first_entered.wait(), timeout=1)
            second_task = asyncio.create_task(cleanup_user_files(second_session, uid=uid))
            try:
                await asyncio.wait_for(second_entered.wait(), timeout=0.2)
            except TimeoutError:
                pass
        finally:
            release_first.set()

        tasks = [first_task] if second_task is None else [first_task, second_task]
        results = await asyncio.gather(*tasks, return_exceptions=True)

    assert all(not isinstance(result, BaseException) for result in results)
    assert sorted(results) == [False, True]
    assert not file_path.exists()

    async with setup_session_factory() as session:
        assert await session.scalar(select(UserFileCleanup).where(UserFileCleanup.uid == uid)) is None
