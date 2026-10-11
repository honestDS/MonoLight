from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.security as security
from app.api.v1 import scheduled_tasks
from app.core.constants import ERR_BACKGROUND_TASK_NOT_FOUND, ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND, ERR_SESSION_NOT_FOUND, ERR_UNAUTHORIZED
from app.core.i18n import t
from app.core.utils.time import get_local_time
from app.handler import register_handlers
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.scheduled_task import ScheduledTask, ScheduledTaskStatus
from app.models.session import ChatSession
from app.models.user import User
from app.providers.database import get_db

TEST_JWT_SECRET = "scheduled-task-permissions-integration-jwt-secret"
LIST_PATH = "/api/v1/scheduled-tasks/list"
CREATE_PATH = "/api/v1/scheduled-tasks/create"
UPDATE_PATH = "/api/v1/scheduled-tasks/update"
DELETE_PATH = "/api/v1/scheduled-tasks/delete"


def _headers(username: str, **claims: Any) -> dict[str, str]:
    token = security.create_access_token(data={"sub": username, **claims})
    return {"Authorization": f"Bearer {token}"}


async def _task_snapshot(factory: async_sessionmaker[AsyncSession]) -> tuple[dict[str, Any], ...]:
    async with factory() as session:
        result = await session.execute(select(ScheduledTask).order_by(ScheduledTask.id))
        return tuple(task.model_dump(mode="json") for task in result.scalars().all())


@pytest_asyncio.fixture
async def scheduled_task_session_factory(database_factory) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    tables = [
        User.__table__,
        PromptLibrary.__table__,
        Profile.__table__,
        ChatSession.__table__,
        ScheduledTask.__table__,
    ]
    async with database_factory(
        tables=tables,
        foreign_keys=True,
        name="scheduled-task-permissions.sqlite3",
    ) as factory:
        fixed_created_at = datetime(2026, 1, 1, tzinfo=UTC)
        async with factory() as session:
            session.add_all(
                [
                    User(uid="admin", username="super-admin", is_active=True, is_superuser=True),
                    User(uid="owner", username="task-owner", is_active=True, is_superuser=False),
                    User(uid="peer", username="task-peer", is_active=True, is_superuser=False),
                    User(uid="disabled", username="disabled-user", is_active=False, is_superuser=False),
                    PromptLibrary(id=1, uid="admin", name="scheduled-task-test-prompt", content="test prompt"),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    Profile(id=1, uid="owner", name="owner-main-profile", prompt_id=1, configs={}),
                    Profile(id=2, uid="owner", name="owner-alternate-profile", prompt_id=1, configs={}),
                    Profile(id=3, uid="peer", name="peer-profile", prompt_id=1, configs={}),
                    Profile(id=4, uid="admin", name="admin-profile", prompt_id=1, configs={}),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ChatSession(session_id="owner-main", uid="owner", profile_id=1),
                    ChatSession(session_id="owner-alternate", uid="owner", profile_id=2),
                    ChatSession(session_id="peer-main", uid="peer", profile_id=3),
                    ChatSession(session_id="admin-main", uid="admin", profile_id=4),
                    ChatSession(session_id="owner-no-profile", uid="owner", profile_id=None),
                    ChatSession(session_id="owner-wrong-profile", uid="owner", profile_id=3),
                ]
            )
            await session.flush()
            session.add_all(
                [
                    ScheduledTask(
                        id=1,
                        name="owner-enabled",
                        uid="owner",
                        session_id="owner-main",
                        profile_id=1,
                        message="owner enabled message",
                        interval_seconds=60,
                        status=ScheduledTaskStatus.ENABLED,
                        next_run_at=fixed_created_at + timedelta(minutes=1),
                        created_at=fixed_created_at,
                        updated_at=fixed_created_at,
                    ),
                    ScheduledTask(
                        id=2,
                        name="owner-disabled",
                        uid="owner",
                        session_id="owner-alternate",
                        profile_id=2,
                        message="owner disabled message",
                        interval_seconds=120,
                        status=ScheduledTaskStatus.DISABLED,
                        next_run_at=fixed_created_at + timedelta(minutes=2),
                        created_at=fixed_created_at + timedelta(minutes=1),
                        updated_at=fixed_created_at + timedelta(minutes=1),
                    ),
                    ScheduledTask(
                        id=3,
                        name="peer-enabled",
                        uid="peer",
                        session_id="peer-main",
                        profile_id=3,
                        message="peer enabled message",
                        interval_seconds=180,
                        status=ScheduledTaskStatus.ENABLED,
                        next_run_at=fixed_created_at + timedelta(minutes=3),
                        created_at=fixed_created_at + timedelta(minutes=2),
                        updated_at=fixed_created_at + timedelta(minutes=2),
                    ),
                    ScheduledTask(
                        id=4,
                        name="admin-enabled",
                        uid="admin",
                        session_id="admin-main",
                        profile_id=4,
                        message="admin enabled message",
                        interval_seconds=240,
                        status=ScheduledTaskStatus.ENABLED,
                        next_run_at=fixed_created_at + timedelta(minutes=4),
                        created_at=fixed_created_at + timedelta(minutes=3),
                        updated_at=fixed_created_at + timedelta(minutes=3),
                    ),
                ]
            )
            await session.commit()
        yield factory


@pytest_asyncio.fixture
async def db_session(
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with scheduled_task_session_factory() as session:
        yield session


@pytest_asyncio.fixture
async def api_app(
    db_session: AsyncSession,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(security, "AsyncSessionLocal", scheduled_task_session_factory)
    monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

    app = FastAPI()
    register_handlers(app)
    app.include_router(scheduled_tasks.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        yield db_session

    app.dependency_overrides[get_db] = override_get_db
    yield app


def _success_data(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200
    assert payload["data"] is not None
    return payload["data"]


LIST_IDENTITIES = (
    pytest.param("task-owner", {1, 2}, id="owner-only"),
    pytest.param("task-peer", {3}, id="peer-only"),
    pytest.param("super-admin", {1, 2, 3, 4}, id="superuser-all"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("username,expected_ids", LIST_IDENTITIES)
async def test_list_scopes_tasks_by_database_identity_and_returns_owner_username(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    username: str,
    expected_ids: set[int],
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get(LIST_PATH, headers=_headers(username))
    data = _success_data(response)
    items = data["items"]
    assert data["total"] == len(expected_ids)
    assert {item["id"] for item in items} == expected_ids
    expected_usernames = {
        "owner": "task-owner",
        "peer": "task-peer",
        "admin": "super-admin",
    }
    assert {item["username"] for item in items} == {expected_usernames[item["uid"]] for item in items}
    for item in items:
        assert item["username"] == expected_usernames[item["uid"]]
    assert await _task_snapshot(scheduled_task_session_factory) == before


@pytest.mark.asyncio
async def test_owner_pagination_filters_total_and_keeps_pages_disjoint(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        responses = [await client.get(LIST_PATH, headers=_headers("task-owner"), params={"page": page, "size": 1}) for page in (1, 2, 3)]
    pages = [_success_data(response) for response in responses]
    assert [page["page"] for page in pages] == [1, 2, 3]
    assert all(page["size"] == 1 and page["total"] == 2 for page in pages)
    page_ids = [{item["id"] for item in page["items"]} for page in pages]
    assert page_ids[0] == {2}
    assert page_ids[1] == {1}
    assert page_ids[0].isdisjoint(page_ids[1])
    assert page_ids[2] == set()
    assert await _task_snapshot(scheduled_task_session_factory) == before


CREATE_SUCCESS_CASES = (
    pytest.param("task-owner", "owner-main", "owner", 1, "task-owner", id="owner-creates-owner-task"),
    pytest.param("super-admin", "admin-main", "admin", 4, "super-admin", id="superuser-creates-admin-task"),
    pytest.param("super-admin", "owner-main", "owner", 1, "task-owner", id="superuser-creates-owner-task"),
    pytest.param("super-admin", "peer-main", "peer", 3, "task-peer", id="superuser-creates-peer-task"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator,session_id,expected_uid,expected_profile_id,owner_username", CREATE_SUCCESS_CASES)
async def test_create_persists_target_session_owner_and_profile(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    operator: str,
    session_id: str,
    expected_uid: str,
    expected_profile_id: int,
    owner_username: str,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    request_body = {
        "name": f"created-{session_id}",
        "session_id": session_id,
        "message": f"created message for {session_id}",
        "interval_seconds": 60,
        "uid": "forged-request-owner",
        "profile_id": 9999,
        "is_superuser": True,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(CREATE_PATH, headers=_headers(operator), json=request_body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["code"] == 200
        assert payload["data"] is not None
        task_id = payload["data"]["id"]
        assert payload["data"]["uid"] == expected_uid
        assert payload["data"]["session_id"] == session_id
        assert payload["data"]["profile_id"] == expected_profile_id

        list_response = await client.get(LIST_PATH, headers=_headers(owner_username))
    list_data = _success_data(list_response)
    assert task_id in {item["id"] for item in list_data["items"]}

    after = await _task_snapshot(scheduled_task_session_factory)
    before_by_id = {task["id"]: task for task in before}
    after_by_id = {task["id"]: task for task in after}
    created_ids = set(after_by_id) - set(before_by_id)
    assert len(created_ids) == 1
    assert task_id in created_ids
    assert len(after) == len(before) + 1
    for task_id_before, task_before in before_by_id.items():
        assert after_by_id[task_id_before] == task_before

    created_task = after_by_id[task_id]
    assert created_task["uid"] == expected_uid
    assert created_task["session_id"] == session_id
    assert created_task["profile_id"] == expected_profile_id
    async with scheduled_task_session_factory() as session:
        result = await session.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
        persisted_task = result.scalar_one()
        assert persisted_task.uid == expected_uid
        assert persisted_task.session_id == session_id
        assert persisted_task.profile_id == expected_profile_id


CREATE_REJECTION_CASES = (
    pytest.param("task-owner", "peer-main", 404, 404, ERR_SESSION_NOT_FOUND, id="owner-cannot-create-peer-task"),
    pytest.param("task-owner", "missing-session", 404, 404, ERR_SESSION_NOT_FOUND, id="owner-cannot-create-missing-session-task"),
    pytest.param("super-admin", "missing-session", 404, 404, ERR_SESSION_NOT_FOUND, id="superuser-cannot-create-missing-session-task"),
    pytest.param("task-owner", "owner-no-profile", 200, 400, ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND, id="owner-no-profile"),
    pytest.param("task-owner", "owner-wrong-profile", 200, 400, ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND, id="owner-wrong-profile"),
    pytest.param("super-admin", "owner-no-profile", 200, 400, ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND, id="superuser-owner-no-profile"),
    pytest.param("super-admin", "owner-wrong-profile", 200, 400, ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND, id="superuser-owner-wrong-profile"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator,session_id,http_status,expected_code,error_key", CREATE_REJECTION_CASES)
async def test_create_rejects_invalid_target_without_persisting_task(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    operator: str,
    session_id: str,
    http_status: int,
    expected_code: int,
    error_key: str,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    request_body = {
        "name": f"rejected-{session_id}",
        "session_id": session_id,
        "message": f"rejected message for {session_id}",
        "interval_seconds": 60,
        "uid": "forged-request-owner",
        "profile_id": 9999,
        "is_superuser": True,
    }
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(CREATE_PATH, headers=_headers(operator), json=request_body)
    assert response.status_code == http_status
    payload = response.json()
    assert payload["code"] == expected_code
    assert payload["message"] == t(error_key)
    assert await _task_snapshot(scheduled_task_session_factory) == before


STATUS_CASES = (
    pytest.param("task-owner", "enabled", {1}, id="owner-enabled"),
    pytest.param("task-owner", "disabled", {2}, id="owner-disabled"),
    pytest.param("super-admin", "enabled", {1, 3, 4}, id="superuser-enabled-cross-user"),
    pytest.param("super-admin", "disabled", {2}, id="superuser-disabled-cross-user"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("username,status,expected_ids", STATUS_CASES)
async def test_list_status_filter_matches_scope_and_total(
    api_app: FastAPI,
    username: str,
    status: str,
    expected_ids: set[int],
) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get(LIST_PATH, headers=_headers(username), params={"status": status})
    data = _success_data(response)
    assert data["total"] == len(expected_ids)
    assert {item["id"] for item in data["items"]} == expected_ids
    assert all(item["status"] == status for item in data["items"])


QUERY_ESCALATION_CASES = (
    pytest.param({}, id="no-query"),
    pytest.param({"uid": "peer"}, id="forged-uid"),
    pytest.param({"is_admin": "true"}, id="forged-admin-flag"),
    pytest.param({"uid": "peer", "is_admin": "true"}, id="forged-uid-and-admin-flag"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("params", QUERY_ESCALATION_CASES)
@pytest.mark.parametrize(
    "claims",
    ({}, {"is_superuser": True}),
    ids=("database-role", "forged-jwt-superuser-claim"),
)
async def test_owner_cannot_expand_list_with_forged_query_or_jwt_role(
    api_app: FastAPI,
    params: dict[str, str],
    claims: dict[str, Any],
) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get(LIST_PATH, headers=_headers("task-owner", **claims), params=params)
    data = _success_data(response)
    assert data["total"] == 2
    assert {item["id"] for item in data["items"]} == {1, 2}
    assert {item["uid"] for item in data["items"]} == {"owner"}


@pytest.mark.asyncio
async def test_list_reflects_renamed_user_without_changing_persisted_task_uid(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with scheduled_task_session_factory() as session:
        result = await session.execute(select(User).where(User.uid == "owner"))
        owner = result.scalars().one()
        owner.username = "task-owner-renamed"
        session.add(owner)
        await session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.get(LIST_PATH, headers=_headers("task-owner-renamed"))
    data = _success_data(response)
    assert data["total"] == 2
    assert {item["id"] for item in data["items"]} == {1, 2}
    assert {item["username"] for item in data["items"]} == {"task-owner-renamed"}
    assert {item["uid"] for item in data["items"]} == {"owner"}

    async with scheduled_task_session_factory() as session:
        result = await session.execute(select(User).where(User.uid == "owner"))
        persisted_owner = result.scalars().one()
        assert persisted_owner.uid == "owner"
        assert persisted_owner.username == "task-owner-renamed"
    assert await _task_snapshot(scheduled_task_session_factory) == before


def _assert_other_tasks_unchanged(
    before: tuple[dict[str, Any], ...],
    after: tuple[dict[str, Any], ...],
    task_id: int,
) -> None:
    before_by_id = {task["id"]: task for task in before}
    after_by_id = {task["id"]: task for task in after}
    assert set(after_by_id) == set(before_by_id)
    for other_id, before_task in before_by_id.items():
        if other_id != task_id:
            assert after_by_id[other_id] == before_task


def _assert_task_unchanged_except(
    before: dict[str, Any],
    after: dict[str, Any],
    *ignored_fields: str,
) -> None:
    ignored = set(ignored_fields)
    assert {key: value for key, value in before.items() if key not in ignored} == {key: value for key, value in after.items() if key not in ignored}


UPDATE_SUCCESS_CASES = (
    pytest.param("task-owner", 1, "owner", "owner-main", 1, id="owner-updates-own-task"),
    pytest.param("super-admin", 1, "owner", "owner-main", 1, id="superuser-updates-owner-task"),
    pytest.param("super-admin", 3, "peer", "peer-main", 3, id="superuser-updates-peer-task"),
    pytest.param("super-admin", 4, "admin", "admin-main", 4, id="superuser-updates-own-task"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator,task_id,expected_uid,expected_session_id,expected_profile_id", UPDATE_SUCCESS_CASES)
async def test_update_changes_task_fields_and_preserves_database_owner(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    operator: str,
    task_id: int,
    expected_uid: str,
    expected_session_id: str,
    expected_profile_id: int,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    before_by_id = {task["id"]: task for task in before}
    updated_name = f"updated-task-{task_id}"
    updated_message = f"updated message for task {task_id}"
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            UPDATE_PATH,
            headers=_headers(operator),
            params={"task_id": task_id},
            json={
                "name": updated_name,
                "message": updated_message,
                "interval_seconds": 600,
                "status": ScheduledTaskStatus.DISABLED.value,
            },
        )
        disabled_data = _success_data(response)
        assert disabled_data["name"] == updated_name
        assert disabled_data["message"] == updated_message
        assert disabled_data["interval_seconds"] == 600
        assert disabled_data["status"] == ScheduledTaskStatus.DISABLED.value

        response = await client.post(
            UPDATE_PATH,
            headers=_headers(operator),
            params={"task_id": task_id},
            json={"status": ScheduledTaskStatus.ENABLED.value},
        )
        enabled_data = _success_data(response)
        assert enabled_data["name"] == updated_name
        assert enabled_data["message"] == updated_message
        assert enabled_data["interval_seconds"] == 600
        assert enabled_data["status"] == ScheduledTaskStatus.ENABLED.value

    after = await _task_snapshot(scheduled_task_session_factory)
    _assert_other_tasks_unchanged(before, after, task_id)
    updated_task = {task["id"]: task for task in after}[task_id]
    before_task = before_by_id[task_id]
    assert updated_task["name"] == updated_name
    assert updated_task["message"] == updated_message
    assert updated_task["interval_seconds"] == 600
    assert updated_task["status"] == ScheduledTaskStatus.ENABLED.value
    assert updated_task["uid"] == expected_uid
    assert updated_task["session_id"] == expected_session_id
    assert updated_task["profile_id"] == expected_profile_id
    assert updated_task["next_run_at"] != before_task["next_run_at"]

    now = get_local_time()
    next_run_at = datetime.fromisoformat(updated_task["next_run_at"])
    if next_run_at.tzinfo is None:
        next_run_at = next_run_at.replace(tzinfo=now.tzinfo)
    assert now + timedelta(seconds=590) <= next_run_at <= now + timedelta(seconds=610)


UPDATE_SESSION_SUCCESS_OPERATORS = ("task-owner", "super-admin")


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", UPDATE_SESSION_SUCCESS_OPERATORS)
async def test_update_switches_task_session_only_with_same_owner_profile(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    operator: str,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    before_task = {task["id"]: task for task in before}[1]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            UPDATE_PATH,
            headers=_headers(operator),
            params={"task_id": 1},
            json={"session_id": "owner-alternate"},
        )
    data = _success_data(response)
    assert data["uid"] == "owner"
    assert data["session_id"] == "owner-alternate"
    assert data["profile_id"] == 2

    after = await _task_snapshot(scheduled_task_session_factory)
    _assert_other_tasks_unchanged(before, after, 1)
    updated_task = {task["id"]: task for task in after}[1]
    assert updated_task["uid"] == "owner"
    assert updated_task["session_id"] == "owner-alternate"
    assert updated_task["profile_id"] == 2
    _assert_task_unchanged_except(before_task, updated_task, "session_id", "profile_id", "updated_at")


UPDATE_FORBIDDEN_CASES = (
    pytest.param(3, "peer", id="owner-cannot-update-peer-task"),
    pytest.param(4, "admin", id="owner-cannot-update-admin-task"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id,target_uid", UPDATE_FORBIDDEN_CASES)
async def test_owner_cannot_update_other_users_tasks_with_forged_authorization_inputs(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    task_id: int,
    target_uid: str,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            UPDATE_PATH,
            headers=_headers("task-owner", is_superuser=True),
            params={"task_id": task_id, "uid": target_uid, "is_admin": "true"},
            json={
                "name": "forged update",
                "message": "forged message",
                "uid": "owner",
                "profile_id": 1,
                "is_superuser": True,
            },
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 404
    assert payload["message"] == t(ERR_BACKGROUND_TASK_NOT_FOUND)
    assert await _task_snapshot(scheduled_task_session_factory) == before


UPDATE_INVALID_SESSION_CASES = (
    pytest.param("task-owner", "peer-main", id="owner-cannot-switch-to-peer-session"),
    pytest.param("task-owner", "missing-session", id="owner-cannot-switch-to-missing-session"),
    pytest.param("super-admin", "peer-main", id="superuser-cannot-switch-to-peer-session"),
    pytest.param("super-admin", "missing-session", id="superuser-cannot-switch-to-missing-session"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator,session_id", UPDATE_INVALID_SESSION_CASES)
async def test_update_rejects_cross_user_or_missing_session_without_persisting(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    operator: str,
    session_id: str,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            UPDATE_PATH,
            headers=_headers(operator),
            params={"task_id": 1},
            json={"name": "must not persist", "session_id": session_id},
        )
    assert response.status_code == 404
    payload = response.json()
    assert payload["code"] == 404
    assert payload["message"] == t(ERR_SESSION_NOT_FOUND)
    assert await _task_snapshot(scheduled_task_session_factory) == before


UPDATE_INVALID_PROFILE_CASES = (
    pytest.param("task-owner", "owner-no-profile", id="owner-no-profile"),
    pytest.param("task-owner", "owner-wrong-profile", id="owner-wrong-profile"),
    pytest.param("super-admin", "owner-no-profile", id="superuser-owner-no-profile"),
    pytest.param("super-admin", "owner-wrong-profile", id="superuser-owner-wrong-profile"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator,session_id", UPDATE_INVALID_PROFILE_CASES)
async def test_update_rejects_invalid_target_profile_without_persisting(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    operator: str,
    session_id: str,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            UPDATE_PATH,
            headers=_headers(operator),
            params={"task_id": 1},
            json={"name": "must not persist", "session_id": session_id},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 400
    assert payload["message"] == t(ERR_SCHEDULED_TASK_PROFILE_NOT_FOUND)
    assert await _task_snapshot(scheduled_task_session_factory) == before


DELETE_SUCCESS_CASES = (
    pytest.param("task-owner", 1, id="owner-deletes-own-task"),
    pytest.param("task-peer", 3, id="peer-deletes-own-task"),
    pytest.param("super-admin", 1, id="superuser-deletes-owner-task"),
    pytest.param("super-admin", 3, id="superuser-deletes-peer-task"),
    pytest.param("super-admin", 4, id="superuser-deletes-admin-task"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator,task_id", DELETE_SUCCESS_CASES)
async def test_delete_removes_only_authorized_task(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    operator: str,
    task_id: int,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(DELETE_PATH, headers=_headers(operator), params={"task_id": task_id})
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 200

    async with scheduled_task_session_factory() as session:
        result = await session.execute(select(ScheduledTask).where(ScheduledTask.id == task_id))
        assert result.scalar_one_or_none() is None

    after = await _task_snapshot(scheduled_task_session_factory)
    assert after == tuple(task for task in before if task["id"] != task_id)


DELETE_FORBIDDEN_CASES = (
    pytest.param(3, "peer", id="owner-cannot-delete-peer-task"),
    pytest.param(4, "admin", id="owner-cannot-delete-admin-task"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("task_id,target_uid", DELETE_FORBIDDEN_CASES)
async def test_owner_cannot_delete_other_users_tasks_with_forged_authorization_inputs(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    task_id: int,
    target_uid: str,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            DELETE_PATH,
            headers=_headers("task-owner", is_superuser=True),
            params={"task_id": task_id, "uid": target_uid, "is_admin": "true"},
        )
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 404
    assert payload["message"] == t(ERR_BACKGROUND_TASK_NOT_FOUND)
    assert await _task_snapshot(scheduled_task_session_factory) == before


DELETE_MISSING_OPERATORS = (
    pytest.param("task-owner", id="owner-cannot-delete-missing-task"),
    pytest.param("super-admin", id="superuser-cannot-delete-missing-task"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", DELETE_MISSING_OPERATORS)
async def test_delete_rejects_missing_task_without_persisting(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    operator: str,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(DELETE_PATH, headers=_headers(operator), params={"task_id": 999})
    assert response.status_code == 200
    payload = response.json()
    assert payload["code"] == 404
    assert payload["message"] == t(ERR_BACKGROUND_TASK_NOT_FOUND)
    assert await _task_snapshot(scheduled_task_session_factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("operator", UPDATE_SESSION_SUCCESS_OPERATORS)
async def test_update_ignores_explicit_null_fields_without_clearing_task_data(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    operator: str,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    before_task = {task["id"]: task for task in before}[1]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.post(
            UPDATE_PATH,
            headers=_headers(operator),
            params={"task_id": 1},
            json={
                "name": None,
                "session_id": None,
                "message": None,
                "interval_seconds": None,
                "status": None,
            },
        )
    data = _success_data(response)
    assert data["uid"] == before_task["uid"]
    assert data["session_id"] == before_task["session_id"]
    assert data["profile_id"] == before_task["profile_id"]
    assert data["name"] == before_task["name"]
    assert data["message"] == before_task["message"]
    assert data["interval_seconds"] == before_task["interval_seconds"]
    assert data["status"] == before_task["status"]
    after = await _task_snapshot(scheduled_task_session_factory)
    _assert_other_tasks_unchanged(before, after, 1)
    updated_task = {task["id"]: task for task in after}[1]
    _assert_task_unchanged_except(before_task, updated_task, "updated_at")


AUTH_FAILURE_CASES = (
    pytest.param("missing-token", id="missing-token"),
    pytest.param("invalid-token", id="invalid-token"),
    pytest.param("expired-token", id="expired-token"),
    pytest.param("disabled-user-token", id="disabled-user-token"),
    pytest.param("missing-user-token", id="missing-user-token"),
)


def _auth_failure_headers(case: str) -> dict[str, str]:
    if case == "missing-token":
        return {}
    if case == "invalid-token":
        return {"Authorization": "Bearer invalid-token"}
    if case == "expired-token":
        token = security.create_access_token(data={"sub": "task-owner"}, expires_delta=timedelta(seconds=-60))
        return {"Authorization": f"Bearer {token}"}
    if case == "disabled-user-token":
        return _headers("disabled-user")
    if case == "missing-user-token":
        return _headers("missing-user")
    raise AssertionError(f"unknown authentication failure case: {case}")


def _assert_unauthorized(response: httpx.Response) -> None:
    assert response.status_code == 401
    payload = response.json()
    assert payload["code"] == 401
    assert payload["message"] == t(ERR_UNAUTHORIZED)
    assert payload["data"] is None


AUTH_ENDPOINT_CASES = (
    pytest.param("GET", LIST_PATH, None, None, id="list"),
    pytest.param(
        "POST",
        CREATE_PATH,
        {
            "name": "authentication-failure-create",
            "session_id": "owner-main",
            "message": "authentication-failure message",
            "interval_seconds": 60,
        },
        None,
        id="create",
    ),
    pytest.param("POST", UPDATE_PATH, {"name": "authentication-failure-update"}, {"task_id": 1}, id="update"),
    pytest.param("POST", DELETE_PATH, None, {"task_id": 1}, id="delete"),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("auth_case", AUTH_FAILURE_CASES)
@pytest.mark.parametrize("method,path,json,params", AUTH_ENDPOINT_CASES)
async def test_scheduled_task_endpoints_reject_authentication_failures_without_persisting(
    api_app: FastAPI,
    scheduled_task_session_factory: async_sessionmaker[AsyncSession],
    auth_case: str,
    method: str,
    path: str,
    json: dict[str, Any] | None,
    params: dict[str, Any] | None,
) -> None:
    before = await _task_snapshot(scheduled_task_session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api_app), base_url="http://test") as client:
        response = await client.request(
            method,
            path,
            headers=_auth_failure_headers(auth_case),
            json=json,
            params=params,
        )
    _assert_unauthorized(response)
    assert await _task_snapshot(scheduled_task_session_factory) == before
