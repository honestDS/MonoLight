import asyncio
import os
import uuid
from collections.abc import AsyncGenerator
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import delete
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import app.core.security as security
from app.api.v1.profile import router as profile_router
from app.api.v1.prompts import router
from app.api.v1.users import router as user_router
from app.core.crud.account.user import user_crud
from app.core.crud.profile.profile import profile_crud
from app.core.crud.profile.prompt import prompt_crud
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.utils.dispatcher.inject_system_prompt import build_system_prompt
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseProfileBinding
from app.models.memory import LongTermMemoryStore
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.user import User
from app.providers.database import get_db
from app.providers.database.bootstrap import ensure_default_profile_for_user

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
        (None, prompt_ids[None][0], "orphan-updated"),
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
async def test_build_system_prompt_keeps_owned_content_and_excludes_foreign_or_orphan_content(
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
        ),
    )

    async with session_factory() as session:
        profiles = (await session.execute(select(Profile).where(Profile.name.in_(("build-own-profile", "build-foreign-profile", "build-orphan-profile"))))).scalars().all()
        prompt_texts = {profile.name: await build_system_prompt(session, profile) for profile in profiles}

    own_content = "content-alice-one"
    foreign_content = "content-bob-one"
    orphan_content = "content-orphan"
    assert own_content in prompt_texts["build-own-profile"]
    assert foreign_content not in prompt_texts["build-foreign-profile"]
    assert orphan_content not in prompt_texts["build-orphan-profile"]
    for prompt_text in prompt_texts.values():
        assert foreign_content not in prompt_text
        assert orphan_content not in prompt_text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "prompt_owner",
    (
        pytest.param("user-a", id="user-a"),
        pytest.param(None, id="orphan"),
        pytest.param("deleted-user", id="legacy"),
    ),
)
async def test_prompt_update_superuser_transfers_unreferenced_prompt_to_user_b_without_changing_content(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    prompt_owner: str | None,
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids[prompt_owner][0]
    before = await _prompt_snapshot(session_factory)
    before_row = {row[0]: row for row in before}[prompt_id]
    original_name = before_row[2]
    original_content = before_row[3]
    original_created_at = before_row[4]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        transfer_response = await _update_prompt(
            client,
            _token("super-admin"),
            prompt_id,
            {"uid": "user-b"},
        )
        transfer_data = _assert_updated_prompt(
            transfer_response,
            uid="user-b",
            name=original_name,
            content=original_content,
        )
        assert transfer_data["id"] == prompt_id
        assert transfer_data["created_at"] == original_created_at.isoformat()
        assert transfer_data["username"] == "bob"

        after_transfer = await _prompt_snapshot(session_factory)
        _assert_only_prompt_changed(
            before,
            after_transfer,
            prompt_id,
            uid="user-b",
            name=original_name,
            content=original_content,
        )

        bob_list_response = await client.get(
            "/api/v1/prompts/list",
            headers={"Authorization": f"Bearer {_token('bob')}"},
            params={"page": 1, "size": 100},
        )
        bob_list = _assert_page(bob_list_response, page=1, size=100, current_uid="user-b")
        bob_items = [item for item in bob_list["items"] if item["id"] == prompt_id]
        assert len(bob_items) == 1
        assert bob_items[0]["uid"] == "user-b"
        assert bob_items[0]["username"] == "bob"

        alice_list_response = await client.get(
            "/api/v1/prompts/list",
            headers={"Authorization": f"Bearer {_token('alice')}"},
            params={"page": 1, "size": 100},
        )
        alice_list = _assert_page(alice_list_response, page=1, size=100, current_uid="user-a")
        assert prompt_id not in {item["id"] for item in alice_list["items"]}

        before_alice_edit = await _prompt_snapshot(session_factory)
        alice_edit_response = await _update_prompt(
            client,
            _token("alice"),
            prompt_id,
            {"name": "alice-forbidden-edit", "content": "not persisted"},
        )
        _assert_standard(alice_edit_response, 403, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_alice_edit

        bob_name = f"{original_name}-bob-edit"
        bob_content = f"{original_content}-bob-edit"
        before_bob_edit = await _prompt_snapshot(session_factory)
        bob_edit_response = await _update_prompt(
            client,
            _token("bob"),
            prompt_id,
            {"name": bob_name, "content": bob_content},
        )
        bob_edit_data = _assert_updated_prompt(
            bob_edit_response,
            uid="user-b",
            name=bob_name,
            content=bob_content,
        )
        assert bob_edit_data["id"] == prompt_id
        assert bob_edit_data["username"] == "bob"

    _assert_only_prompt_changed(
        before_bob_edit,
        await _prompt_snapshot(session_factory),
        prompt_id,
        uid="user-b",
        name=bob_name,
        content=bob_content,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("requested_uid", ("user-b", "admin", "deleted-user"))
async def test_prompt_update_regular_user_cannot_change_own_prompt_owner_in_json(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    requested_uid: str,
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    forged_token = _token("alice", is_superuser=True, uid="user-b")

    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _update_prompt(
            client,
            forged_token,
            prompt_id,
            {
                "uid": requested_uid,
                "name": f"forbidden-{requested_uid}",
                "content": "not persisted",
            },
        )

    _assert_standard(response, 403, data_is_none=True)
    assert await _prompt_snapshot(session_factory) == before_prompts
    assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "requested_uid, expected_status",
    (
        pytest.param(None, 404, id="null"),
        pytest.param("deleted-user", 404, id="missing-user"),
        pytest.param("", 422, id="empty"),
        pytest.param("x" * 51, 422, id="too-long"),
    ),
)
async def test_prompt_update_superuser_rejects_invalid_owner_uid_without_prompt_or_profile_changes(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    requested_uid: str | None,
    expected_status: int,
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _update_prompt(
            client,
            _token("super-admin"),
            prompt_id,
            {
                "uid": requested_uid,
                "name": "invalid-owner-update",
                "content": "not persisted",
            },
        )

    _assert_standard(response, expected_status, data_is_none=True)
    assert await _prompt_snapshot(session_factory) == before_prompts
    assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize("include_name", (False, True), ids=("actual-name", "explicit-name"))
async def test_prompt_update_superuser_checks_transferred_name_against_target_owner_scope(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    include_name: bool,
) -> None:
    app, session_factory, prompt_ids = api_context
    prompt_id = prompt_ids["user-a"][0]
    async with session_factory() as session:
        prompt = (await session.execute(select(PromptLibrary).where(PromptLibrary.id == prompt_id))).scalars().one()
        original_content = prompt.content
        prompt.name = "bob-one"
        await session.commit()
    renamed_row = {row[0]: row for row in await _prompt_snapshot(session_factory)}[prompt_id]
    assert renamed_row[3] == original_content

    conflict_body: dict[str, Any] = {"uid": "user-b", "content": "not persisted"}
    if include_name:
        conflict_body["name"] = "bob-two"

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before_conflict_prompts = await _prompt_snapshot(session_factory)
        before_conflict_profiles = await _profile_snapshot(session_factory)
        conflict_response = await _update_prompt(
            client,
            _token("super-admin"),
            prompt_id,
            conflict_body,
        )
        _assert_standard(conflict_response, 400, data_is_none=True)
        assert await _prompt_snapshot(session_factory) == before_conflict_prompts
        assert await _profile_snapshot(session_factory) == before_conflict_profiles

        transfer_name = "bob-transferred-unique"
        transfer_content = "bob-transferred-content"
        before_transfer_prompts = await _prompt_snapshot(session_factory)
        before_transfer_profiles = await _profile_snapshot(session_factory)
        transfer_response = await _update_prompt(
            client,
            _token("super-admin"),
            prompt_id,
            {"uid": "user-b", "name": transfer_name, "content": transfer_content},
        )
        transfer_data = _assert_updated_prompt(
            transfer_response,
            uid="user-b",
            name=transfer_name,
            content=transfer_content,
        )
        assert transfer_data["id"] == prompt_id
        assert transfer_data["username"] == "bob"

    _assert_only_prompt_changed(
        before_transfer_prompts,
        await _prompt_snapshot(session_factory),
        prompt_id,
        uid="user-b",
        name=transfer_name,
        content=transfer_content,
    )
    assert await _profile_snapshot(session_factory) == before_transfer_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ("zh", "en"))
async def test_prompt_update_protects_referenced_prompt_until_profile_repoints(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    locale: str,
) -> None:
    app, session_factory, prompt_ids = api_context
    app.include_router(profile_router, prefix="/api/v1")
    source_id, replacement_id = prompt_ids["user-a"]
    (profile_id,) = await _seed_profiles(session_factory, (("user-a", source_id, "alice-reference"),))

    same_owner_content = "content-alice-one-updated"
    before_same_owner_prompts = await _prompt_snapshot(session_factory)
    before_same_owner_profiles = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        same_owner_response = await _update_prompt(
            client,
            _token("super-admin"),
            source_id,
            {"uid": "user-a", "content": same_owner_content},
        )
        same_owner_data = _assert_updated_prompt(
            same_owner_response,
            uid="user-a",
            name="alice-one",
            content=same_owner_content,
        )
        assert same_owner_data["id"] == source_id
        assert same_owner_data["username"] == "alice"

        after_same_owner_prompts = await _prompt_snapshot(session_factory)
        _assert_only_prompt_changed(
            before_same_owner_prompts,
            after_same_owner_prompts,
            source_id,
            uid="user-a",
            name="alice-one",
            content=same_owner_content,
        )
        assert await _profile_snapshot(session_factory) == before_same_owner_profiles

        transfer_name = "bob-transferred-reference"
        transfer_content = "content-bob-transferred-reference"
        before_conflict_prompts = await _prompt_snapshot(session_factory)
        before_conflict_profiles = await _profile_snapshot(session_factory)
        locale_token = set_current_locale(locale)
        try:
            conflict_response = await _update_prompt(
                client,
                _token("super-admin"),
                source_id,
                {"uid": "user-b", "name": transfer_name, "content": transfer_content},
            )
        finally:
            reset_current_locale(locale_token)

        conflict_payload = _assert_standard(conflict_response, 409, data_is_none=True)
        assert (
            conflict_payload["message"]
            == {
                "zh": "提示词正在被配置引用，请先为相关配置更换同一所属用户的提示词，再修改所属用户",
                "en": "This prompt is referenced by profiles. Select another prompt belonging to the same owner for those profiles before changing its owner.",
            }[locale]
        )
        assert await _prompt_snapshot(session_factory) == before_conflict_prompts
        assert await _profile_snapshot(session_factory) == before_conflict_profiles

        profile_response = await client.post(
            "/api/v1/profiles/update",
            headers={"Authorization": f"Bearer {_token('super-admin')}"},
            params={"profile_id": profile_id},
            json={"prompt_id": replacement_id},
        )
        profile_payload = _assert_standard(profile_response, 200)
        assert profile_payload["data"]["id"] == profile_id
        assert profile_payload["data"]["uid"] == "user-a"
        assert profile_payload["data"]["prompt_id"] == replacement_id

        before_retry_prompts = await _prompt_snapshot(session_factory)
        before_retry_profiles = await _profile_snapshot(session_factory)
        retry_response = await _update_prompt(
            client,
            _token("super-admin"),
            source_id,
            {"uid": "user-b", "name": transfer_name, "content": transfer_content},
        )
        retry_data = _assert_updated_prompt(
            retry_response,
            uid="user-b",
            name=transfer_name,
            content=transfer_content,
        )
        assert retry_data["id"] == source_id
        assert retry_data["username"] == "bob"

    _assert_only_prompt_changed(
        before_retry_prompts,
        await _prompt_snapshot(session_factory),
        source_id,
        uid="user-b",
        name=transfer_name,
        content=transfer_content,
    )
    after_retry_profiles = await _profile_snapshot(session_factory)
    assert after_retry_profiles == before_retry_profiles
    profile_row = next(row for row in after_retry_profiles if row[0] == profile_id)
    assert profile_row[1] == "user-a"
    assert profile_row[3] == replacement_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "old_operation, expected_status",
    (
        pytest.param("profile-create", 400, id="profile-create"),
        pytest.param("profile-update", 404, id="profile-update"),
        pytest.param("prompt-update", 403, id="prompt-update"),
        pytest.param("prompt-delete", 403, id="prompt-delete"),
    ),
)
async def test_prompt_transfer_serializes_old_owner_operations_and_rejects_concurrent_access(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    old_operation: str,
    expected_status: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, prompt_ids = api_context
    app.include_router(profile_router, prefix="/api/v1")
    source_id = prompt_ids["user-a"][0]
    profile_id: int | None = None
    if old_operation == "profile-update":
        (profile_id,) = await _seed_profiles(
            session_factory,
            (("user-a", prompt_ids["user-a"][1], "alice-concurrent-profile"),),
        )

    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)
    source_row = next(row for row in before_prompts if row[0] == source_id)
    source_name = source_row[2]
    source_content = source_row[3]
    source_created_at = source_row[4]

    transfer_staged = asyncio.Event()
    release_transfer = asyncio.Event()
    waiter_started = asyncio.Event()
    original_prompt_update = prompt_crud.update
    original_prompt_get = prompt_crud.get
    original_profile_lock = profile_crud.lock_for_runtime_use
    original_user_get = user_crud.get_by_uid

    async def gated_prompt_update(
        db: AsyncSession,
        *,
        db_obj: PromptLibrary,
        obj_in: Any,
        commit: bool = True,
    ) -> PromptLibrary:
        result = await original_prompt_update(db, db_obj=db_obj, obj_in=obj_in, commit=commit)
        if commit is False and getattr(obj_in, "uid", None) == "user-b":
            transfer_staged.set()
            await release_transfer.wait()
        return result

    async def gated_prompt_get(
        db: AsyncSession,
        id: int,
        *,
        lock_for_reference_write: bool = False,
    ) -> PromptLibrary | None:
        if transfer_staged.is_set() and not release_transfer.is_set():
            waiter_started.set()
        return await original_prompt_get(db, id, lock_for_reference_write=lock_for_reference_write)

    async def gated_profile_lock(
        db: AsyncSession,
        *,
        profile_id: int,
        uid: str | None,
    ) -> Profile | None:
        if transfer_staged.is_set() and not release_transfer.is_set():
            waiter_started.set()
        return await original_profile_lock(db, profile_id=profile_id, uid=uid)

    async def gated_user_get(
        db: AsyncSession,
        uid: str,
        *,
        lock_for_reference_write: bool = False,
    ) -> User | None:
        if lock_for_reference_write and transfer_staged.is_set() and not release_transfer.is_set():
            waiter_started.set()
        return await original_user_get(db, uid, lock_for_reference_write=lock_for_reference_write)

    monkeypatch.setattr(prompt_crud, "update", gated_prompt_update)
    monkeypatch.setattr(prompt_crud, "get", gated_prompt_get)
    monkeypatch.setattr(profile_crud, "lock_for_runtime_use", gated_profile_lock)
    monkeypatch.setattr(user_crud, "get_by_uid", gated_user_get)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        async def run_old_operation() -> httpx.Response:
            if old_operation == "profile-create":
                return await _create_profile(
                    client,
                    _token("alice"),
                    {"name": "alice-concurrent-profile-create", "prompt_id": source_id},
                )
            if old_operation == "profile-update":
                assert profile_id is not None
                return await client.post(
                    "/api/v1/profiles/update",
                    headers={"Authorization": f"Bearer {_token('alice')}"},
                    params={"profile_id": profile_id},
                    json={"prompt_id": source_id},
                )
            if old_operation == "prompt-update":
                return await _update_prompt(
                    client,
                    _token("alice"),
                    source_id,
                    {"name": "alice-concurrent-update", "content": "alice-concurrent-content"},
                )
            if old_operation == "prompt-delete":
                return await _delete_prompt(client, _token("alice"), source_id)
            raise AssertionError(f"unsupported operation: {old_operation}")

        admin_task: asyncio.Task[httpx.Response] | None = None
        old_task: asyncio.Task[httpx.Response] | None = None
        try:
            admin_task = asyncio.create_task(_update_prompt(client, _token("super-admin"), source_id, {"uid": "user-b"}))
            await asyncio.wait_for(transfer_staged.wait(), 10)

            old_task = asyncio.create_task(run_old_operation())
            await asyncio.wait_for(waiter_started.wait(), 10)
            assert not old_task.done()

            release_transfer.set()
            admin_response, old_response = await asyncio.wait_for(
                asyncio.gather(admin_task, old_task),
                10,
            )
        finally:
            release_transfer.set()
            for task in (admin_task, old_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (admin_task, old_task) if task is not None),
                return_exceptions=True,
            )

    admin_data = _assert_updated_prompt(
        admin_response,
        uid="user-b",
        name=source_name,
        content=source_content,
    )
    assert admin_data["username"] == "bob"
    assert admin_data["created_at"] == source_created_at.isoformat()
    _assert_standard(old_response, expected_status, data_is_none=True)

    after_prompts = await _prompt_snapshot(session_factory)
    _assert_only_prompt_changed(
        before_prompts,
        after_prompts,
        source_id,
        uid="user-b",
        name=source_name,
        content=source_content,
    )
    after_source = next(row for row in after_prompts if row[0] == source_id)
    assert after_source[4] == source_created_at
    assert await _profile_snapshot(session_factory) == before_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "operation",
    (
        pytest.param("profile-create-no-prompt", id="profile-create-no-prompt"),
        pytest.param("profile-create-with-prompt", id="profile-create-with-prompt"),
        pytest.param("prompt-create", id="prompt-create"),
        pytest.param("prompt-transfer", id="prompt-transfer"),
        pytest.param("default-bootstrap", id="default-bootstrap"),
    ),
)
async def test_user_delete_blocks_concurrent_configuration_creation_and_prompt_writes(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], PromptIds],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, prompt_ids = api_context
    app.include_router(user_router, prefix="/api/v1/admin")
    app.include_router(profile_router, prefix="/api/v1")

    user_a_prompt_id = prompt_ids["user-a"][0]
    source_transfer = prompt_ids["user-b"][0]
    (default_profile_id,) = await _seed_profiles(
        session_factory,
        (("user-a", user_a_prompt_id, "defaultProfile"),),
    )
    async with session_factory() as session:
        default_profile = (await session.execute(select(Profile).where(Profile.id == default_profile_id))).scalars().one()
        default_profile.is_default = True
        user_a = (await session.execute(select(User).where(User.uid == "user-a"))).scalars().one()
        assert user_a.id is not None
        user_a_id = int(user_a.id)
        await session.commit()

    before_prompts = await _prompt_snapshot(session_factory)
    before_profiles = await _profile_snapshot(session_factory)
    source_before = next(row for row in before_prompts if row[0] == source_transfer)

    deletion_staged = asyncio.Event()
    release = asyncio.Event()
    waiter_started = asyncio.Event()
    original_remove = user_crud.remove
    original_get_by_uid = user_crud.get_by_uid

    async def gated_remove(db: AsyncSession, *, id: Any) -> User | None:
        if id == user_a_id:
            deletion_staged.set()
            await release.wait()
        return await original_remove(db, id=id)

    async def gated_get_by_uid(
        db: AsyncSession,
        uid: str,
        *,
        lock_for_reference_write: bool = False,
    ) -> User | None:
        if lock_for_reference_write and deletion_staged.is_set() and not release.is_set():
            waiter_started.set()
        return await original_get_by_uid(db, uid, lock_for_reference_write=lock_for_reference_write)

    monkeypatch.setattr(user_crud, "remove", gated_remove)
    monkeypatch.setattr(user_crud, "get_by_uid", gated_get_by_uid)

    admin_token = _token("super-admin")
    delete_task: asyncio.Task[httpx.Response] | None = None
    operation_task: asyncio.Task[Any] | None = None
    delete_response: httpx.Response | None = None
    operation_result: Any = None

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:

        async def run_operation() -> httpx.Response | None:
            if operation == "profile-create-no-prompt":
                return await _create_profile(
                    client,
                    admin_token,
                    {"uid": "user-a", "name": "concurrent-profile-no-prompt"},
                )
            if operation == "profile-create-with-prompt":
                return await _create_profile(
                    client,
                    admin_token,
                    {
                        "uid": "user-a",
                        "name": "concurrent-profile-with-prompt",
                        "prompt_id": user_a_prompt_id,
                    },
                )
            if operation == "prompt-create":
                return await _create_prompt(
                    client,
                    admin_token,
                    {"uid": "user-a", "name": "concurrent-prompt", "content": "not persisted"},
                )
            if operation == "prompt-transfer":
                return await _update_prompt(client, admin_token, source_transfer, {"uid": "user-a"})
            if operation == "default-bootstrap":
                async with session_factory() as session:
                    result = await ensure_default_profile_for_user(session, uid="user-a")
                    await session.commit()
                    return result
            raise AssertionError(f"unsupported operation: {operation}")

        try:
            delete_task = asyncio.create_task(
                client.post(
                    "/api/v1/admin/user/delete",
                    headers={"Authorization": f"Bearer {admin_token}"},
                    params={"uid": "user-a"},
                )
            )
            await asyncio.wait_for(deletion_staged.wait(), 10)

            operation_task = asyncio.create_task(run_operation())
            await asyncio.wait_for(waiter_started.wait(), 10)
            assert not operation_task.done()

            release.set()
            assert delete_task is not None
            assert operation_task is not None
            delete_response, operation_result = await asyncio.wait_for(
                asyncio.gather(delete_task, operation_task),
                10,
            )
        finally:
            release.set()
            for task in (delete_task, operation_task):
                if task is not None and not task.done():
                    task.cancel()
            await asyncio.gather(
                *(task for task in (delete_task, operation_task) if task is not None),
                return_exceptions=True,
            )

    assert delete_response is not None
    _assert_standard(delete_response, 200, data_is_none=True)
    if operation == "default-bootstrap":
        assert operation_result is None
    else:
        assert isinstance(operation_result, httpx.Response)
        _assert_standard(operation_result, 404, data_is_none=True)

    after_prompts = await _prompt_snapshot(session_factory)
    after_profiles = await _profile_snapshot(session_factory)
    assert all(row[1] != "user-a" for row in after_prompts)
    assert all(row[1] != "user-a" for row in after_profiles)
    assert tuple(row for row in after_prompts if row[1] != "user-a") == tuple(row for row in before_prompts if row[1] != "user-a")
    assert tuple(row for row in after_profiles if row[1] != "user-a") == tuple(row for row in before_profiles if row[1] != "user-a")

    source_after = next(row for row in after_prompts if row[0] == source_transfer)
    assert source_after[1] == "user-b"
    assert source_after[2:4] == source_before[2:4]
    async with session_factory() as session:
        assert (await session.execute(select(User).where(User.uid == "user-a"))).scalars().first() is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "create_a_first",
    (
        pytest.param(True, id="a-lower-id"),
        pytest.param(False, id="b-lower-id"),
    ),
)
async def test_profile_update_to_replacement_serializes_with_prompt_delete(
    prompt_profile_lock_session_factory: async_sessionmaker[AsyncSession],
    create_a_first: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_factory = prompt_profile_lock_session_factory
    test_uid = f"lock-test-{uuid.uuid4().hex[:12]}"
    profile_a_id: int | None = None
    profile_b_id: int | None = None
    prompt_a_id: int | None = None
    prompt_b_id: int | None = None
    prompt_b_name = f"{test_uid}-prompt-b"
    prompt_b_content = f"{test_uid}-content-b"
    profile_b_name = f"{test_uid}-profile-b"
    updated_profile_name = f"{test_uid}-profile-a-updated"

    try:
        async with session_factory() as session:
            test_user = User(
                uid=test_uid,
                username=f"{test_uid}-user",
                is_active=True,
                is_superuser=False,
            )
            prompt_a = PromptLibrary(
                uid=test_uid,
                name=f"{test_uid}-prompt-a",
                content=f"{test_uid}-content-a",
            )
            prompt_b = PromptLibrary(
                uid=test_uid,
                name=prompt_b_name,
                content=prompt_b_content,
            )
            session.add(test_user)
            if create_a_first:
                session.add(prompt_a)
                await session.flush()
                session.add(prompt_b)
            else:
                session.add(prompt_b)
                await session.flush()
                session.add(prompt_a)
            await session.flush()
            assert prompt_a.id is not None
            assert prompt_b.id is not None
            prompt_a_id = int(prompt_a.id)
            prompt_b_id = int(prompt_b.id)

            profile_a = Profile(
                uid=test_uid,
                name=f"{test_uid}-profile-a",
                prompt_id=prompt_a_id,
                configs={},
                is_default=False,
            )
            profile_b = Profile(
                uid=test_uid,
                name=profile_b_name,
                prompt_id=prompt_b_id,
                configs={},
                is_default=False,
            )
            session.add_all([profile_a, profile_b])
            await session.flush()
            assert profile_a.id is not None
            assert profile_b.id is not None
            profile_a_id = int(profile_a.id)
            profile_b_id = int(profile_b.id)
            await session.commit()

        app = FastAPI()
        register_handlers(app)
        app.include_router(profile_router, prefix="/api/v1")
        app.include_router(router, prefix="/api/v1")

        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with session_factory() as session:
                try:
                    yield session
                finally:
                    if session.in_transaction():
                        await session.rollback()

        async def override_current_user() -> User:
            return test_user

        app.dependency_overrides[get_db] = override_get_db
        app.dependency_overrides[security.get_current_user] = override_current_user

        assert prompt_a_id is not None
        assert prompt_b_id is not None
        assert profile_a_id is not None
        assert profile_b_id is not None
        update_at_prompt = asyncio.Event()
        delete_at_profiles = asyncio.Event()
        release = asyncio.Event()
        original_get_visible = prompt_crud.get_visible
        original_get_multi_by_prompt_id = profile_crud.get_multi_by_prompt_id

        async def gated_get_visible(
            db: AsyncSession,
            prompt_id: int,
            uid: str | None = None,
            *,
            lock_for_reference_write: bool = False,
        ) -> PromptLibrary | None:
            if prompt_id == prompt_b_id and uid == test_uid and lock_for_reference_write:
                update_at_prompt.set()
                await asyncio.wait_for(release.wait(), 10)
            return await original_get_visible(
                db,
                prompt_id,
                uid=uid,
                lock_for_reference_write=lock_for_reference_write,
            )

        async def gated_get_multi_by_prompt_id(
            db: AsyncSession,
            prompt_id: int,
            *,
            lock_for_reference_write: bool = False,
        ) -> list[Profile]:
            if prompt_id == prompt_a_id and lock_for_reference_write:
                delete_at_profiles.set()
                await asyncio.wait_for(release.wait(), 10)
            return await original_get_multi_by_prompt_id(
                db,
                prompt_id,
                lock_for_reference_write=lock_for_reference_write,
            )

        monkeypatch.setattr(prompt_crud, "get_visible", gated_get_visible)
        monkeypatch.setattr(profile_crud, "get_multi_by_prompt_id", gated_get_multi_by_prompt_id)

        update_response: httpx.Response | None = None
        delete_response: httpx.Response | None = None
        update_task: asyncio.Task[httpx.Response] | None = None
        delete_task: asyncio.Task[httpx.Response] | None = None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            try:
                update_task = asyncio.create_task(
                    client.post(
                        "/api/v1/profiles/update",
                        params={"profile_id": profile_a_id},
                        json={"name": updated_profile_name, "prompt_id": prompt_b_id},
                    )
                )
                await asyncio.wait_for(update_at_prompt.wait(), 10)

                delete_task = asyncio.create_task(
                    client.post(
                        "/api/v1/prompts/delete",
                        params={
                            "prompt_id": prompt_a_id,
                            "replacement_prompt_id": prompt_b_id,
                            "confirm_reassign": True,
                        },
                    )
                )
                await asyncio.wait_for(delete_at_profiles.wait(), 10)
                assert not update_task.done()
                assert not delete_task.done()

                release.set()
                update_response, delete_response = await asyncio.wait_for(
                    asyncio.gather(update_task, delete_task),
                    10,
                )
            finally:
                release.set()
                tasks = [task for task in (update_task, delete_task) if task is not None]
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.wait_for(
                        asyncio.gather(*tasks, return_exceptions=True),
                        10,
                    )

        assert update_response is not None
        assert delete_response is not None
        update_payload = _assert_standard(update_response, 200)
        assert update_payload["data"]["prompt_id"] == prompt_b_id
        assert update_payload["data"]["name"] == updated_profile_name
        _assert_standard(delete_response, 200, data_is_none=True)

        async with session_factory() as session:
            prompts = (await session.execute(select(PromptLibrary).where(PromptLibrary.uid == test_uid))).scalars().all()
            profiles = (await session.execute(select(Profile).where(Profile.uid == test_uid).order_by(Profile.id))).scalars().all()

        assert {prompt.id for prompt in prompts} == {prompt_b_id}
        replacement_prompt = prompts[0]
        assert replacement_prompt.uid == test_uid
        assert replacement_prompt.name == prompt_b_name
        assert replacement_prompt.content == prompt_b_content

        profiles_by_id = {profile.id: profile for profile in profiles}
        assert profile_a_id in profiles_by_id
        assert profile_b_id in profiles_by_id
        updated_profile = profiles_by_id[profile_a_id]
        assert updated_profile.uid == test_uid
        assert updated_profile.prompt_id == prompt_b_id
        assert updated_profile.name == updated_profile_name
        untouched_profile = profiles_by_id[profile_b_id]
        assert untouched_profile.uid == test_uid
        assert untouched_profile.prompt_id == prompt_b_id
        assert untouched_profile.name == profile_b_name
        assert untouched_profile.configs == {}
        assert untouched_profile.is_default is False
    finally:
        async with session_factory() as session:
            await session.execute(delete(Profile).where(Profile.uid == test_uid))
            await session.execute(delete(PromptLibrary).where(PromptLibrary.uid == test_uid))
            await session.execute(delete(User).where(User.uid == test_uid))
            await session.commit()
