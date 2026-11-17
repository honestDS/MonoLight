import os
from collections.abc import AsyncGenerator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import app.core.security as security
from app.api.v1.profile import router as profile_router
from app.api.v1.prompts import router
from app.core.constants import (
    ERR_DB_OPERATION_FAILED,
    ERR_PROMPT_NAME_EXISTS,
    ERR_PROMPT_OWNER_REQUIRED,
)
from app.core.crud.profile.prompt import prompt_crud
from app.core.i18n import t
from app.core.utils.dispatcher.inject_system_prompt import build_system_prompt
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseProfileBinding
from app.models.memory import LongTermMemoryStore
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.user import User
from app.providers.database import get_db

TEST_PASSWORD = "prompt-permissions-integration-password"
TEST_JWT_SECRET = "prompt-permissions-integration-jwt-secret"
PromptIds = dict[str | None, tuple[int, ...]]


@pytest_asyncio.fixture(
    params=("sqlite", "mysql"),
    ids=("sqlite", "mysql"),
)
async def prompt_profile_lock_session_factory(
    request: pytest.FixtureRequest,
    database_factory,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    if request.param == "sqlite":
        async with database_factory(foreign_keys=True) as session_factory:
            yield session_factory
        return

    mysql_url_value = os.getenv("MONOLIGH_TEST_MYSQL_URL")
    if not mysql_url_value:
        pytest.skip("MONOLIGH_TEST_MYSQL_URL is not configured")
    try:
        mysql_url = make_url(mysql_url_value)
    except Exception:
        pytest.fail("invalid MONOLIGH_TEST_MYSQL_URL")
    if mysql_url.get_backend_name() != "mysql" or mysql_url.get_driver_name() not in {"aiomysql", "asyncmy"}:
        pytest.fail("MONOLIGH_TEST_MYSQL_URL must use mysql+aiomysql or mysql+asyncmy")

    database_name = mysql_url.database or ""
    allowed_database = os.getenv("MONOLIGH_TEST_MYSQL_ALLOWED_DATABASE", "").strip()
    if not database_name or ("test" not in database_name.lower() and allowed_database != database_name):
        pytest.fail("MySQL test database must contain 'test' or match MONOLIGH_TEST_MYSQL_ALLOWED_DATABASE")

    engine = create_async_engine(
        mysql_url,
        isolation_level="REPEATABLE READ",
        pool_pre_ping=True,
    )
    tables = (
        User.__table__,
        PromptLibrary.__table__,
        Profile.__table__,
        ModelChannel.__table__,
        LongTermMemoryStore.__table__,
        KnowledgeBase.__table__,
        KnowledgeBaseProfileBinding.__table__,
    )
    try:
        async with engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: SQLModel.metadata.create_all(
                    sync_connection,
                    tables=tables,
                )
            )
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


@pytest_asyncio.fixture
async def api_context(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds]]:
    async with database_factory(foreign_keys=True) as session_factory:
        password_hash = security.get_password_hash(TEST_PASSWORD)
        async with session_factory() as session:
            session.add_all(
                [
                    User(
                        uid="admin",
                        username="super-admin",
                        is_active=True,
                        is_superuser=True,
                        hashed_password=password_hash,
                    ),
                    User(
                        uid="user-a",
                        username="alice",
                        is_active=True,
                        is_superuser=False,
                        hashed_password=password_hash,
                    ),
                    User(
                        uid="user-b",
                        username="bob",
                        is_active=True,
                        is_superuser=False,
                        hashed_password=password_hash,
                    ),
                    User(
                        uid="user-empty",
                        username="user-empty",
                        is_active=True,
                        is_superuser=False,
                        hashed_password=password_hash,
                    ),
                    User(
                        uid="disabled",
                        username="disabled",
                        is_active=False,
                        is_superuser=False,
                        hashed_password=password_hash,
                    ),
                ]
            )
            await session.flush()

            prompt_ids: PromptIds = {}
            prompt_names: dict[str | None, tuple[str, ...]] = {
                "admin": ("admin-one", "admin-two"),
                "user-a": ("alice-one", "alice-two"),
                "user-b": ("bob-one", "bob-two"),
                None: ("orphan",),
                "deleted-user": ("legacy",),
            }
            for uid, names in prompt_names.items():
                ids: list[int] = []
                for name in names:
                    prompt = PromptLibrary(
                        uid=uid,
                        name=name,
                        content=f"content-{name}",
                        created_at=datetime.now(UTC),
                    )
                    session.add(prompt)
                    await session.flush()
                    assert prompt.id is not None
                    ids.append(int(prompt.id))
                prompt_ids[uid] = tuple(ids)
            await session.commit()

        monkeypatch.setattr(security, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

        app = FastAPI()
        register_handlers(app)
        app.include_router(router, prefix="/api/v1")

        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        yield app, session_factory, prompt_ids


def _token(username: str, **claims: Any) -> str:
    return security.create_access_token(data={"sub": username, **claims})


async def _prompt_snapshot(session_factory: async_sessionmaker[AsyncSession]) -> tuple[tuple[Any, ...], ...]:
    async with session_factory() as session:
        prompts = (await session.execute(select(PromptLibrary).order_by(PromptLibrary.id))).scalars().all()
        return tuple((prompt.id, prompt.uid, prompt.name, prompt.content, prompt.created_at) for prompt in prompts)


def _assert_standard(response: httpx.Response, code: int, *, data_is_none: bool = False) -> dict[str, Any]:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert isinstance(payload["message"], str)
    assert payload["message"]
    if data_is_none:
        assert payload["data"] is None
    return payload


def _assert_page(response: httpx.Response, *, page: int, size: int, current_uid: str) -> dict[str, Any]:
    payload = _assert_standard(response, 200)
    data = payload["data"]
    assert set(data) == {"items", "total", "page", "size", "meta"}
    assert isinstance(data["items"], list)
    assert data["page"] == page
    assert data["size"] == size
    assert data["meta"]["current_uid"] == current_uid
    return data


@pytest.mark.asyncio
async def test_prompt_list_is_scoped_to_regular_user_across_pages_and_forged_identity(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, _session_factory, prompt_ids = api_context
    own_ids = set(prompt_ids["user-a"])
    headers = {"Authorization": f"Bearer {_token('alice', role='admin', uid='user-b', is_superuser=True)}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/prompts/list",
            headers=headers,
            params={"page": 1, "size": 10, "uid": "user-b", "is_superuser": "true"},
        )
        data = _assert_page(response, page=1, size=10, current_uid="user-a")
        assert {item["id"] for item in data["items"]} == own_ids
        assert {item["uid"] for item in data["items"]} == {"user-a"}
        assert {item["username"] for item in data["items"]} == {"alice"}
        assert data["total"] == 2

        page_ids: list[set[int]] = []
        for page in (1, 2):
            page_response = await client.get(
                "/api/v1/prompts/list",
                headers=headers,
                params={"page": page, "size": 1, "uid": "user-b", "is_superuser": "true"},
            )
            page_data = _assert_page(page_response, page=page, size=1, current_uid="user-a")
            assert len(page_data["items"]) == 1
            assert page_data["total"] == 2
            assert page_data["items"][0]["uid"] == "user-a"
            assert page_data["items"][0]["username"] == "alice"
            page_ids.append({page_data["items"][0]["id"]})

    assert page_ids[0].isdisjoint(page_ids[1])
    assert page_ids[0] | page_ids[1] == own_ids


@pytest.mark.asyncio
async def test_prompt_list_as_superuser_shows_all_records_and_real_owner_names(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, _session_factory, prompt_ids = api_context
    all_ids = {prompt_id for ids in prompt_ids.values() for prompt_id in ids}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/prompts/list",
            headers={"Authorization": f"Bearer {_token('super-admin')}"},
            params={"page": 1, "size": 100},
        )

    data = _assert_page(response, page=1, size=100, current_uid="admin")
    assert {item["id"] for item in data["items"]} == all_ids
    assert {item["uid"] for item in data["items"]} == {"admin", "user-a", "user-b", None, "deleted-user"}
    assert data["total"] == 8
    expected_usernames = {"admin": "super-admin", "user-a": "alice", "user-b": "bob"}
    for item in data["items"]:
        assert item["username"] == expected_usernames.get(item["uid"])


@pytest.mark.asyncio
async def test_prompt_list_uses_current_username_after_user_rename(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, _prompt_ids = api_context
    async with session_factory() as session:
        user = (await session.execute(select(User).where(User.uid == "user-a"))).scalars().one()
        user.username = "alice-renamed"
        await session.commit()

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/prompts/list",
            headers={"Authorization": f"Bearer {_token('super-admin')}"},
            params={"page": 1, "size": 100},
        )

    data = _assert_page(response, page=1, size=100, current_uid="admin")
    user_a_items = [item for item in data["items"] if item["uid"] == "user-a"]
    assert len(user_a_items) == 2
    assert {item["username"] for item in user_a_items} == {"alice-renamed"}


@pytest.mark.asyncio
async def test_prompt_list_for_user_without_prompts_is_empty(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, _session_factory, _prompt_ids = api_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/prompts/list",
            headers={"Authorization": f"Bearer {_token('user-empty')}"},
        )

    data = _assert_page(response, page=1, size=10, current_uid="user-empty")
    assert data["items"] == []
    assert data["total"] == 0


@pytest.mark.asyncio
async def test_prompt_list_rejects_missing_invalid_disabled_and_unknown_tokens_without_mutation(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, _prompt_ids = api_context
    requests = [
        {},
        {"Authorization": "Bearer not-a-valid-jwt"},
        {"Authorization": f"Bearer {_token('disabled')}"},
        {"Authorization": f"Bearer {_token('unknown-user')}"},
    ]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for headers in requests:
            before = await _prompt_snapshot(session_factory)
            response = await client.get("/api/v1/prompts/list", headers=headers)
            _assert_standard(response, 401, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_prompt_list_rejects_invalid_pagination_parameters(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, _session_factory, _prompt_ids = api_context
    invalid_params = (
        {"page": 0, "size": 10},
        {"page": 1, "size": 0},
        {"page": 1, "size": 1001},
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for params in invalid_params:
            response = await client.get(
                "/api/v1/prompts/list",
                headers={"Authorization": f"Bearer {_token('alice')}"},
                params=params,
            )
            _assert_standard(response, 422, data_is_none=True)


async def _create_prompt(client: httpx.AsyncClient, token: str | None, body: dict[str, Any]) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return await client.post("/api/v1/prompts/create", headers=headers, json=body)


def _assert_created_prompt(
    response: httpx.Response,
    *,
    uid: str,
    username: str,
    name: str,
    content: str,
) -> dict[str, Any]:
    payload = _assert_standard(response, 200)
    data = payload["data"]
    assert set(data) == {"uid", "name", "content", "id", "created_at", "username"}
    assert data["uid"] == uid
    assert data["username"] == username
    assert data["name"] == name
    assert data["content"] == content
    assert isinstance(data["id"], int)
    assert data["id"] > 0
    assert isinstance(data["created_at"], str)
    assert data["created_at"]
    return data


@pytest.mark.asyncio
async def test_prompt_create_regular_user_persists_own_uid_for_omitted_null_and_explicit_uid(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, _prompt_ids = api_context
    requests = (
        ("alice-created-omitted", "content-omitted", {}),
        ("alice-created-null", "content-null", {"uid": None}),
        ("alice-created-explicit", "content-explicit", {"uid": "user-a"}),
    )
    before = await _prompt_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created_ids: list[int] = []
        for name, content, extra in requests:
            body = {"name": name, "content": content, **extra}
            response = await _create_prompt(client, _token("alice"), body)
            data = _assert_created_prompt(
                response,
                uid="user-a",
                username="alice",
                name=name,
                content=content,
            )
            created_ids.append(data["id"])

    after = await _prompt_snapshot(session_factory)
    before_by_id = {row[0]: row for row in before}
    after_by_id = {row[0]: row for row in after}
    assert set(before_by_id).issubset(after_by_id)
    assert all(after_by_id[prompt_id] == before_row for prompt_id, before_row in before_by_id.items())
    created_rows = [after_by_id[prompt_id] for prompt_id in created_ids]
    assert [(row[1], row[2], row[3]) for row in created_rows] == [
        ("user-a", "alice-created-omitted", "content-omitted"),
        ("user-a", "alice-created-null", "content-null"),
        ("user-a", "alice-created-explicit", "content-explicit"),
    ]


@pytest.mark.asyncio
async def test_prompt_create_regular_user_cannot_forge_owner_in_jwt_or_body(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, _prompt_ids = api_context
    forged_token = _token("alice", is_superuser=True, uid="user-b")
    bodies = (
        {"name": "forbidden-user-b", "content": "forbidden", "uid": "user-b"},
        {"name": "forbidden-admin", "content": "forbidden", "uid": "admin"},
        {"name": "forbidden-deleted", "content": "forbidden", "uid": "deleted-user"},
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for body in bodies:
            before = await _prompt_snapshot(session_factory)
            response = await _create_prompt(client, forged_token, body)
            _assert_standard(response, 403, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_prompt_create_name_is_scoped_to_owner_and_duplicate_names_do_not_mutate(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, _prompt_ids = api_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await _create_prompt(
            client,
            _token("alice"),
            {"name": "bob-one", "content": "alice-owned-bob-name"},
        )
        _assert_created_prompt(
            create_response,
            uid="user-a",
            username="alice",
            name="bob-one",
            content="alice-owned-bob-name",
        )

        for body in (
            {"name": "alice-one", "content": "duplicate-existing-owner-name"},
            {"name": "bob-one", "content": "duplicate-new-owner-name"},
        ):
            before = await _prompt_snapshot(session_factory)
            duplicate_response = await _create_prompt(client, _token("alice"), body)
            _assert_standard(duplicate_response, 400, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_prompt_create_superuser_defaults_to_admin_or_uses_requested_owner(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, _prompt_ids = api_context
    requests = (
        ("super-created-omitted", "super-content-omitted", {}),
        ("super-created-null", "super-content-null", {"uid": None}),
        ("super-created-user-b", "super-content-user-b", {"uid": "user-b"}),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        created_ids: list[int] = []
        for name, content, extra in requests:
            response = await _create_prompt(
                client,
                _token("super-admin"),
                {"name": name, "content": content, **extra},
            )
            expected_uid, expected_username = ("user-b", "bob") if extra.get("uid") == "user-b" else ("admin", "super-admin")
            data = _assert_created_prompt(
                response,
                uid=expected_uid,
                username=expected_username,
                name=name,
                content=content,
            )
            created_ids.append(data["id"])

    snapshot = await _prompt_snapshot(session_factory)
    rows_by_id = {row[0]: row for row in snapshot}
    assert [(rows_by_id[prompt_id][1], rows_by_id[prompt_id][2]) for prompt_id in created_ids] == [
        ("admin", "super-created-omitted"),
        ("admin", "super-created-null"),
        ("user-b", "super-created-user-b"),
    ]


@pytest.mark.asyncio
async def test_prompt_create_superuser_rejects_missing_owner_without_mutation(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, _prompt_ids = api_context
    before = await _prompt_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _create_prompt(
            client,
            _token("super-admin"),
            {"name": "missing-owner-prompt", "content": "not persisted", "uid": "deleted-user"},
        )
    _assert_standard(response, 404, data_is_none=True)
    assert await _prompt_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_prompt_create_rejects_missing_invalid_and_disabled_tokens_without_mutation(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, _prompt_ids = api_context
    requests = (
        (None, {"name": "unauthorized-missing", "content": "not persisted"}),
        ("not-a-valid-jwt", {"name": "unauthorized-invalid", "content": "not persisted"}),
        (_token("disabled"), {"name": "unauthorized-disabled", "content": "not persisted"}),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for token, body in requests:
            before = await _prompt_snapshot(session_factory)
            response = await _create_prompt(client, token, body)
            _assert_standard(response, 401, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before


async def _update_prompt(
    client: httpx.AsyncClient,
    token: str | None,
    prompt_id: int,
    body: dict[str, Any],
    *,
    extra_params: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    params: dict[str, Any] = {"prompt_id": prompt_id}
    if extra_params:
        params.update(extra_params)
    return await client.post("/api/v1/prompts/update", headers=headers, params=params, json=body)


def _assert_updated_prompt(
    response: httpx.Response,
    *,
    uid: str | None,
    name: str,
    content: str,
) -> dict[str, Any]:
    payload = _assert_standard(response, 200)
    data = payload["data"]
    assert set(data) == {"uid", "name", "content", "id", "created_at", "username"}
    assert data["uid"] == uid
    assert data["name"] == name
    assert data["content"] == content
    assert isinstance(data["id"], int)
    assert isinstance(data["created_at"], str)
    assert data["created_at"]
    return data


def _assert_only_prompt_changed(
    before: tuple[tuple[Any, ...], ...],
    after: tuple[tuple[Any, ...], ...],
    prompt_id: int,
    *,
    uid: str | None,
    name: str,
    content: str,
) -> None:
    before_by_id = {row[0]: row for row in before}
    after_by_id = {row[0]: row for row in after}
    assert set(after_by_id) == set(before_by_id)
    for row_id, before_row in before_by_id.items():
        after_row = after_by_id[row_id]
        if row_id != prompt_id:
            assert after_row == before_row
            continue
        assert after_row[1] == uid
        assert after_row[2] == name
        assert after_row[3] == content
        assert after_row[4] == before_row[4]


@pytest.mark.asyncio
async def test_prompt_update_regular_user_changes_own_fields_without_changing_owner_or_created_at(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    name = "alice-updated"
    content = "alice-updated-content"
    before = await _prompt_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _update_prompt(
            client,
            _token("alice"),
            prompt_id,
            {"name": name, "content": content, "uid": "user-a"},
            extra_params={"uid": "user-b"},
        )
        data = _assert_updated_prompt(response, uid="user-a", name=name, content=content)
        assert data["id"] == prompt_id
        assert data["username"] == "alice"

    after = await _prompt_snapshot(session_factory)
    _assert_only_prompt_changed(before, after, prompt_id, uid="user-a", name=name, content=content)


@pytest.mark.asyncio
async def test_prompt_update_regular_user_cannot_edit_other_owner_records_with_forged_identity(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    forged_token = _token("alice", is_superuser=True, uid="user-b")
    targets = (
        ("user-b", prompt_ids["user-b"][0]),
        ("admin", prompt_ids["admin"][0]),
        ("no-owner", prompt_ids[None][0]),
        ("deleted-user", prompt_ids["deleted-user"][0]),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for label, prompt_id in targets:
            before = await _prompt_snapshot(session_factory)
            response = await _update_prompt(
                client,
                forged_token,
                prompt_id,
                {"name": f"forbidden-{label}", "content": "not persisted"},
                extra_params={"uid": "user-b"},
            )
            _assert_standard(response, 403, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_prompt_update_superuser_can_edit_each_owner_scope_without_other_mutations(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    targets = (
        ("admin", prompt_ids["admin"][0], "admin-updated"),
        ("user-a", prompt_ids["user-a"][0], "alice-by-admin"),
        ("user-b", prompt_ids["user-b"][0], "bob-by-admin"),
        ("deleted-user", prompt_ids["deleted-user"][0], "legacy-updated"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for uid, prompt_id, name in targets:
            content = f"content-{name}"
            before = await _prompt_snapshot(session_factory)
            response = await _update_prompt(
                client,
                _token("super-admin"),
                prompt_id,
                {"name": name, "content": content},
            )
            data = _assert_updated_prompt(response, uid=uid, name=name, content=content)
            assert data["id"] == prompt_id
            after = await _prompt_snapshot(session_factory)
            _assert_only_prompt_changed(before, after, prompt_id, uid=uid, name=name, content=content)


@pytest.mark.parametrize(
    "owner_payload",
    (
        pytest.param({}, id="omitted"),
        pytest.param({"uid": None}, id="null"),
    ),
)
@pytest.mark.asyncio
async def test_prompt_update_referenced_orphan_requires_owner_for_admin_and_rejects_regular_user(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    owner_payload: dict[str, Any],
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids[None][0]
    await _seed_profiles(session_factory, ((None, prompt_id, "orphan-reference"),))
    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)
    body = {"name": "orphan-updated", "content": "orphan-updated-content", **owner_payload}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        admin_response = await _update_prompt(client, _token("super-admin"), prompt_id, body)
        admin_payload = _assert_standard(admin_response, 400, data_is_none=True)
        assert admin_payload["message"] == t(ERR_PROMPT_OWNER_REQUIRED)
        assert await _prompt_snapshot(session_factory) == before_prompts
        assert await _profile_snapshot(session_factory) == before_profiles

        regular_response = await _update_prompt(client, _token("alice"), prompt_id, body)
        _assert_standard(regular_response, 403, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_prompts
        assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
async def test_prompt_update_legacy_empty_owner_requires_owner(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, _prompt_ids = api_context
    async with session_factory() as session:
        prompt = PromptLibrary(
            uid="",
            name="legacy-empty-owner",
            content="legacy-empty-owner-content",
        )
        session.add(prompt)
        await session.flush()
        assert prompt.id is not None
        prompt_id = int(prompt.id)
        await session.commit()

    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _update_prompt(
            client,
            _token("super-admin"),
            prompt_id,
            {"name": "legacy-empty-owner-updated", "content": "legacy-empty-owner-updated-content"},
        )

    payload = _assert_standard(response, 400, data_is_none=True)
    assert payload["message"] == t(ERR_PROMPT_OWNER_REQUIRED)
    assert await _prompt_snapshot(session_factory) == before_prompts
    assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
async def test_prompt_update_name_scope_allows_cross_owner_name_but_rejects_same_owner_duplicate_and_missing_id(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    own_prompt_id = prompt_ids["user-a"][0]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before_duplicate = await _prompt_snapshot(session_factory)
        duplicate_response = await _update_prompt(
            client,
            _token("alice"),
            own_prompt_id,
            {"name": "alice-two", "content": "duplicate-not-persisted"},
        )
        _assert_standard(duplicate_response, 400, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_duplicate

        cross_owner_id = prompt_ids["user-b"][0]
        before_cross_owner = await _prompt_snapshot(session_factory)
        cross_owner_response = await _update_prompt(
            client,
            _token("super-admin"),
            cross_owner_id,
            {"name": "alice-one", "content": "same-name-different-owner"},
        )
        _assert_updated_prompt(
            cross_owner_response,
            uid="user-b",
            name="alice-one",
            content="same-name-different-owner",
        )
        after_cross_owner = await _prompt_snapshot(session_factory)
        _assert_only_prompt_changed(
            before_cross_owner,
            after_cross_owner,
            cross_owner_id,
            uid="user-b",
            name="alice-one",
            content="same-name-different-owner",
        )

        before_missing = await _prompt_snapshot(session_factory)
        missing_response = await _update_prompt(
            client,
            _token("super-admin"),
            999999,
            {"name": "missing-id", "content": "not persisted"},
        )
        _assert_standard(missing_response, 404, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_missing


@pytest.mark.parametrize("operation", ("create", "rename", "transfer"))
@pytest.mark.asyncio
async def test_prompt_database_name_conflict_rolls_back(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, prompt_ids = api_context
    before = await _prompt_snapshot(session_factory)

    original_get_by_name = prompt_crud.get_by_name

    async def get_by_name_without_conflict(*args: Any, **kwargs: Any) -> None:
        await original_get_by_name(*args, **kwargs)
        return None

    monkeypatch.setattr(prompt_crud, "get_by_name", get_by_name_without_conflict)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        if operation == "create":
            response = await _create_prompt(
                client,
                _token("alice"),
                {"name": "alice-one", "content": "database-conflict-create"},
            )
        else:
            prompt_id = prompt_ids["user-a"][0]
            if operation == "rename":
                token = _token("alice")
                body = {"name": "alice-two", "content": "database-conflict-rename"}
            else:
                token = _token("super-admin")
                body = {"uid": "user-b", "name": "bob-one", "content": "database-conflict-transfer"}
            response = await _update_prompt(client, token, prompt_id, body)

    payload = _assert_standard(response, 400, data_is_none=True)
    assert payload["message"] == t(ERR_PROMPT_NAME_EXISTS)
    assert await _prompt_snapshot(session_factory) == before


@pytest.mark.parametrize("operation", ("create", "update"))
@pytest.mark.asyncio
async def test_prompt_unrelated_integrity_error_is_not_name_conflict(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    before = await _prompt_snapshot(session_factory)

    if operation == "create":
        original_create = prompt_crud.create

        async def create_with_duplicate_id(*args: Any, **kwargs: Any) -> Any:
            kwargs["update_dict"] = {"id": prompt_id}
            return await original_create(*args, **kwargs)

        monkeypatch.setattr(prompt_crud, "create", create_with_duplicate_id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        if operation == "create":
            response = await _create_prompt(
                client,
                _token("alice"),
                {"name": "database-conflict-unrelated", "content": "valid-content"},
            )
        else:
            response = await _update_prompt(client, _token("alice"), prompt_id, {"content": None})

    payload = _assert_standard(response, 500, data_is_none=True)
    assert payload["message"] == t(ERR_DB_OPERATION_FAILED)
    assert await _prompt_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_prompt_update_rejects_missing_invalid_and_disabled_tokens_without_mutation(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][1]
    requests = (
        (None, "unauthorized-update-missing"),
        ("not-a-valid-jwt", "unauthorized-update-invalid"),
        (_token("disabled"), "unauthorized-update-disabled"),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for token, name in requests:
            before = await _prompt_snapshot(session_factory)
            response = await _update_prompt(
                client,
                token,
                prompt_id,
                {"name": name, "content": "not persisted"},
            )
            _assert_standard(response, 401, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before


async def _seed_profiles(
    session_factory: async_sessionmaker[AsyncSession],
    specs: tuple[tuple[str | None, int, str], ...],
) -> tuple[int, ...]:
    async with session_factory() as session:
        profile_ids: list[int] = []
        for uid, prompt_id, name in specs:
            profile = Profile(uid=uid, name=name, prompt_id=prompt_id, configs={}, is_default=False)
            session.add(profile)
            await session.flush()
            assert profile.id is not None
            profile_ids.append(int(profile.id))
        await session.commit()
    return tuple(profile_ids)


async def _profile_snapshot(session_factory: async_sessionmaker[AsyncSession]) -> tuple[tuple[Any, ...], ...]:
    async with session_factory() as session:
        profiles = (await session.execute(select(Profile).order_by(Profile.id))).scalars().all()
        return tuple(
            (
                profile.id,
                profile.uid,
                profile.name,
                profile.prompt_id,
                deepcopy(profile.configs or {}),
                profile.is_default,
            )
            for profile in profiles
        )


async def _delete_prompt(
    client: httpx.AsyncClient,
    token: str | None,
    prompt_id: int,
    *,
    extra_params: dict[str, Any] | None = None,
) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    params: dict[str, Any] = {"prompt_id": prompt_id}
    if extra_params:
        params.update(extra_params)
    return await client.post("/api/v1/prompts/delete", headers=headers, params=params)


def _assert_only_prompt_deleted(
    before: tuple[tuple[Any, ...], ...],
    after: tuple[tuple[Any, ...], ...],
    prompt_id: int,
) -> None:
    before_by_id = {row[0]: row for row in before}
    after_by_id = {row[0]: row for row in after}
    assert prompt_id in before_by_id
    assert prompt_id not in after_by_id
    assert set(after_by_id) == set(before_by_id) - {prompt_id}
    for row_id, before_row in before_by_id.items():
        if row_id != prompt_id:
            assert after_by_id[row_id] == before_row


@pytest.mark.asyncio
async def test_prompt_delete_regular_user_removes_only_unreferenced_own_prompt(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _delete_prompt(client, _token("alice"), prompt_id)

    _assert_standard(response, 200, data_is_none=True)
    _assert_only_prompt_deleted(before_prompts, await _prompt_snapshot(session_factory), prompt_id)
    assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
async def test_prompt_delete_regular_user_cannot_forge_owner_for_other_prompt_scopes(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    forged_token = _token("alice", role="admin", uid="user-b", is_superuser=True)
    targets = (
        prompt_ids["user-b"][0],
        prompt_ids["admin"][0],
        prompt_ids[None][0],
        prompt_ids["deleted-user"][0],
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for prompt_id in targets:
            before_prompts = await _prompt_snapshot(session_factory)
            before_profiles = await _profile_snapshot(session_factory)
            response = await _delete_prompt(
                client,
                forged_token,
                prompt_id,
                extra_params={"uid": "user-b"},
            )
            _assert_standard(response, 403, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before_prompts
            assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
async def test_prompt_delete_superuser_removes_one_unreferenced_prompt_from_each_owner_scope(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    targets = (
        prompt_ids["admin"][0],
        prompt_ids["user-a"][0],
        prompt_ids["user-b"][0],
        prompt_ids[None][0],
        prompt_ids["deleted-user"][0],
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for prompt_id in targets:
            before_prompts = await _prompt_snapshot(session_factory)
            before_profiles = await _profile_snapshot(session_factory)
            response = await _delete_prompt(client, _token("super-admin"), prompt_id)
            _assert_standard(response, 200, data_is_none=True)
            _assert_only_prompt_deleted(before_prompts, await _prompt_snapshot(session_factory), prompt_id)
            assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
async def test_prompt_delete_with_own_profile_reference_returns_conflict_for_regular_and_superuser(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    await _seed_profiles(session_factory, (("user-a", prompt_id, "alice-reference"),))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for token in (_token("alice"), _token("super-admin")):
            before_prompts = await _prompt_snapshot(session_factory)
            before_profiles = await _profile_snapshot(session_factory)
            response = await _delete_prompt(client, token, prompt_id)
            payload = _assert_standard(response, 409)
            assert payload["data"] == {"referenced_count": 1}
            assert await _prompt_snapshot(session_factory) == before_prompts
            assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
async def test_prompt_delete_confirmed_same_owner_replacement_reassigns_all_profiles_and_removes_source(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    source_id, replacement_id = prompt_ids["user-a"]
    await _seed_profiles(
        session_factory,
        (
            ("user-a", source_id, "alice-source-one"),
            ("user-a", source_id, "alice-source-two"),
            ("user-a", replacement_id, "alice-replacement"),
            ("user-b", prompt_ids["user-b"][0], "bob-other"),
        ),
    )
    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _delete_prompt(
            client,
            _token("alice"),
            source_id,
            extra_params={"replacement_prompt_id": replacement_id, "confirm_reassign": True},
        )

    _assert_standard(response, 200, data_is_none=True)
    _assert_only_prompt_deleted(before_prompts, await _prompt_snapshot(session_factory), source_id)
    after_profiles = await _profile_snapshot(session_factory)
    before_by_id = {row[0]: row for row in before_profiles}
    after_by_id = {row[0]: row for row in after_profiles}
    source_profile_ids = {row[0] for row in before_profiles if row[3] == source_id}
    assert len(source_profile_ids) == 2
    assert {before_by_id[profile_id][1] for profile_id in source_profile_ids} == {"user-a"}
    for profile_id, before_row in before_by_id.items():
        after_row = after_by_id[profile_id]
        if profile_id in source_profile_ids:
            assert after_row[:3] == before_row[:3]
            assert after_row[3] == replacement_id
            assert after_row[4:] == before_row[4:]
        else:
            assert after_row == before_row


@pytest.mark.asyncio
async def test_prompt_delete_rejects_invalid_replacements_without_prompt_or_profile_changes(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    source_id = prompt_ids["user-a"][0]
    invalid_replacements = (
        source_id,
        999999,
        prompt_ids["user-b"][0],
        prompt_ids[None][0],
    )
    await _seed_profiles(session_factory, (("user-a", source_id, "alice-reference"),))

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for replacement_id in invalid_replacements:
            before_prompts = await _prompt_snapshot(session_factory)
            before_profiles = await _profile_snapshot(session_factory)
            response = await _delete_prompt(
                client,
                _token("alice"),
                source_id,
                extra_params={"replacement_prompt_id": replacement_id, "confirm_reassign": True},
            )
            _assert_standard(response, 400, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before_prompts
            assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
async def test_prompt_delete_rejects_cross_owner_profile_reference_for_regular_and_superuser(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    source_id, replacement_id = prompt_ids["user-a"]
    await _seed_profiles(
        session_factory,
        (
            ("user-a", source_id, "alice-reference"),
            ("user-b", source_id, "historical-cross-owner-reference"),
        ),
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before_regular_prompts = await _prompt_snapshot(session_factory)
        before_regular_profiles = await _profile_snapshot(session_factory)
        regular_response = await _delete_prompt(client, _token("alice"), source_id)
        _assert_standard(regular_response, 403, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_regular_prompts
        assert await _profile_snapshot(session_factory) == before_regular_profiles

        before_admin_prompts = await _prompt_snapshot(session_factory)
        before_admin_profiles = await _profile_snapshot(session_factory)
        admin_response = await _delete_prompt(
            client,
            _token("super-admin"),
            source_id,
            extra_params={"replacement_prompt_id": replacement_id, "confirm_reassign": True},
        )
        _assert_standard(admin_response, 400, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_admin_prompts
        assert await _profile_snapshot(session_factory) == before_admin_profiles


@pytest.mark.asyncio
async def test_prompt_delete_missing_target_and_unauthorized_tokens_do_not_mutate(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][1]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before_missing_prompts = await _prompt_snapshot(session_factory)
        before_missing_profiles = await _profile_snapshot(session_factory)
        missing_response = await _delete_prompt(client, _token("alice"), 999999)
        _assert_standard(missing_response, 404, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_missing_prompts
        assert await _profile_snapshot(session_factory) == before_missing_profiles

        requests = (
            (None, "missing-token"),
            ("not-a-valid-jwt", "invalid-token"),
            (_token("disabled"), "disabled-token"),
        )
        for token, _label in requests:
            before_prompts = await _prompt_snapshot(session_factory)
            before_profiles = await _profile_snapshot(session_factory)
            response = await _delete_prompt(client, token, prompt_id)
            _assert_standard(response, 401, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before_prompts
            assert await _profile_snapshot(session_factory) == before_profiles


async def _create_profile(client: httpx.AsyncClient, token: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(
        "/api/v1/profiles/create",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


def _assert_created_profile(
    response: httpx.Response,
    *,
    uid: str,
    name: str,
    prompt_id: int,
) -> dict[str, Any]:
    payload = _assert_standard(response, 200)
    data = payload["data"]
    assert data["uid"] == uid
    assert data["name"] == name
    assert data["prompt_id"] == prompt_id
    assert isinstance(data["id"], int)
    assert data["id"] > 0
    return data


def _assert_only_profile_created(
    before: tuple[tuple[Any, ...], ...],
    after: tuple[tuple[Any, ...], ...],
    *,
    profile_id: int,
    uid: str,
    prompt_id: int,
    name: str,
) -> None:
    before_by_id = {row[0]: row for row in before}
    after_by_id = {row[0]: row for row in after}
    assert set(after_by_id) == set(before_by_id) | {profile_id}
    for row_id, before_row in before_by_id.items():
        assert after_by_id[row_id] == before_row
    created_row = after_by_id[profile_id]
    assert created_row[1] == uid
    assert created_row[2] == name
    assert created_row[3] == prompt_id


@pytest.mark.asyncio
async def test_profile_create_allows_only_regular_users_own_prompt_scope(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    app.include_router(profile_router, prefix="/api/v1")
    own_prompt_id = prompt_ids["user-a"][0]
    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        own_response = await _create_profile(
            client,
            _token("alice"),
            {"name": "alice-own-profile", "prompt_id": own_prompt_id},
        )
        own_data = _assert_created_profile(
            own_response,
            uid="user-a",
            name="alice-own-profile",
            prompt_id=own_prompt_id,
        )

        after_own_profiles = await _profile_snapshot(session_factory)
        _assert_only_profile_created(
            before_profiles,
            after_own_profiles,
            profile_id=own_data["id"],
            uid="user-a",
            prompt_id=own_prompt_id,
            name="alice-own-profile",
        )
        assert await _prompt_snapshot(session_factory) == before_prompts

        foreign_targets = (
            ("user-b", prompt_ids["user-b"][0]),
            ("admin", prompt_ids["admin"][0]),
            ("no-owner", prompt_ids[None][0]),
            ("deleted-user", prompt_ids["deleted-user"][0]),
        )
        for label, prompt_id in foreign_targets:
            before_invalid_prompts = await _prompt_snapshot(session_factory)
            before_invalid_profiles = await _profile_snapshot(session_factory)
            response = await _create_profile(
                client,
                _token("alice"),
                {"name": f"alice-invalid-{label}", "prompt_id": prompt_id},
            )
            _assert_standard(response, 400, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before_invalid_prompts
            assert await _profile_snapshot(session_factory) == before_invalid_profiles


@pytest.mark.asyncio
async def test_profile_create_superuser_uid_user_a_cannot_bind_foreign_prompt(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    app, session_factory, prompt_ids = api_context
    app.include_router(profile_router, prefix="/api/v1")
    own_prompt_id = prompt_ids["user-a"][0]
    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        own_response = await _create_profile(
            client,
            _token("super-admin"),
            {"uid": "user-a", "name": "admin-created-user-a-profile", "prompt_id": own_prompt_id},
        )
        own_data = _assert_created_profile(
            own_response,
            uid="user-a",
            name="admin-created-user-a-profile",
            prompt_id=own_prompt_id,
        )
        after_own_profiles = await _profile_snapshot(session_factory)
        _assert_only_profile_created(
            before_profiles,
            after_own_profiles,
            profile_id=own_data["id"],
            uid="user-a",
            prompt_id=own_prompt_id,
            name="admin-created-user-a-profile",
        )
        assert await _prompt_snapshot(session_factory) == before_prompts

        foreign_targets = (
            ("user-b", prompt_ids["user-b"][0]),
            ("admin", prompt_ids["admin"][0]),
            ("no-owner", prompt_ids[None][0]),
            ("deleted-user", prompt_ids["deleted-user"][0]),
        )
        for label, prompt_id in foreign_targets:
            before_invalid_prompts = await _prompt_snapshot(session_factory)
            before_invalid_profiles = await _profile_snapshot(session_factory)
            response = await _create_profile(
                client,
                _token("super-admin"),
                {"uid": "user-a", "name": f"admin-invalid-user-a-{label}", "prompt_id": prompt_id},
            )
            _assert_standard(response, 400, data_is_none=True)
            assert await _prompt_snapshot(session_factory) == before_invalid_prompts
            assert await _profile_snapshot(session_factory) == before_invalid_profiles


@pytest.mark.asyncio
async def test_build_system_prompt_scopes_owned_and_orphan_content_by_profile_owner(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
) -> None:
    _app, session_factory, prompt_ids = api_context
    own_prompt_id = prompt_ids["user-a"][0]
    foreign_prompt_id = prompt_ids["user-b"][0]
    orphan_prompt_id = prompt_ids[None][0]
    await _seed_profiles(
        session_factory,
        (
            ("user-a", own_prompt_id, "build-own-profile"),
            ("user-a", foreign_prompt_id, "build-foreign-profile"),
            ("user-a", orphan_prompt_id, "build-orphan-profile"),
            (None, orphan_prompt_id, "build-unowned-profile"),
            (None, own_prompt_id, "build-unowned-owned-profile"),
        ),
    )

    async with session_factory() as session:
        profiles = (
            (
                await session.execute(
                    select(Profile).where(
                        Profile.name.in_(
                            (
                                "build-own-profile",
                                "build-foreign-profile",
                                "build-orphan-profile",
                                "build-unowned-profile",
                                "build-unowned-owned-profile",
                            )
                        )
                    )
                )
            )
            .scalars()
            .all()
        )
        prompt_texts = {profile.name: await build_system_prompt(session, profile) for profile in profiles}

    own_content = "content-alice-one"
    foreign_content = "content-bob-one"
    orphan_content = "content-orphan"
    assert own_content in prompt_texts["build-own-profile"]
    assert foreign_content not in prompt_texts["build-foreign-profile"]
    assert orphan_content not in prompt_texts["build-orphan-profile"]
    assert orphan_content in prompt_texts["build-unowned-profile"]
    assert own_content not in prompt_texts["build-unowned-profile"]
    assert foreign_content not in prompt_texts["build-unowned-profile"]
    assert own_content not in prompt_texts["build-unowned-owned-profile"]
    for profile_name in (
        "build-own-profile",
        "build-foreign-profile",
        "build-orphan-profile",
        "build-unowned-owned-profile",
    ):
        prompt_text = prompt_texts[profile_name]
        assert foreign_content not in prompt_text
        assert orphan_content not in prompt_text
