from collections.abc import AsyncGenerator
from copy import deepcopy
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.crypto as crypto_module
import app.core.profile.permissions as profile_permissions_module
import app.core.security as security
from app.api.v1.profile import router
from app.core.crud.channel.channel import channel_crud
from app.handler import register_handlers
from app.models.channel import ChannelCreate
from app.models.profile import Profile, ProfileConfig
from app.models.user import User
from app.providers.database import get_db

TEST_PASSWORD = "profile-permissions-integration-password"
TEST_JWT_SECRET = "profile-permissions-integration-jwt-secret"


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x00" * 32)


def _chat_model() -> dict[str, Any]:
    return {
        "model_id": "chat-model",
        "usage": "CHAT",
        "protocol": "OPENAI",
        "context_window_k": 32,
        "max_tokens": 2048,
        "temperature": 0.25,
        "top_p": 0.8,
        "is_enabled": True,
    }


def _profile_configs(channel_id: int) -> dict[str, Any]:
    return ProfileConfig.model_validate(
        {
            "channel": {
                "chat_channel": {
                    "rules": [
                        {
                            "channel_id": channel_id,
                            "model_id": "chat-model",
                            "priority": 1,
                            "weight": 100,
                        }
                    ]
                }
            }
        }
    ).model_dump()


@pytest_asyncio.fixture
async def api_context(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]]]:
    async with database_factory(foreign_keys=True) as session_factory:
        password_hash = security.get_password_hash(TEST_PASSWORD)
        async with session_factory() as session:
            channel = await channel_crud.create_with_plain_api_key(
                session,
                obj_in=ChannelCreate(
                    name="profile-permissions-channel",
                    api_key="profile-permissions-api-key",
                    base_url="https://llm.example/v1",
                    is_active=True,
                    model_ids=[_chat_model()],
                ),
                commit=False,
            )
            assert channel.id is not None
            channel_id = int(channel.id)

            session.add_all(
                [
                    User(
                        uid="admin",
                        username="admin",
                        is_active=True,
                        is_superuser=True,
                        hashed_password=password_hash,
                    ),
                    User(
                        uid="user-a",
                        username="user-a",
                        is_active=True,
                        is_superuser=False,
                        hashed_password=password_hash,
                    ),
                    User(
                        uid="user-b",
                        username="user-b",
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
                ]
            )

            profile_ids: dict[str | None, dict[str, int]] = {}
            for uid in ("admin", "user-a", "user-b", None):
                label = uid or "no-owner"
                default_profile = Profile(
                    uid=uid,
                    name=f"{label}-default",
                    configs=_profile_configs(channel_id),
                    is_default=True,
                )
                target_profile = Profile(
                    uid=uid,
                    name=f"{label}-target",
                    configs=_profile_configs(channel_id),
                    is_default=False,
                )
                session.add_all([default_profile, target_profile])
                await session.flush()
                assert default_profile.id is not None
                assert target_profile.id is not None
                profile_ids[uid] = {
                    "default": int(default_profile.id),
                    "target": int(target_profile.id),
                }
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
        yield app, session_factory, profile_ids


def _token(username: str, **claims: Any) -> str:
    return security.create_access_token(data={"sub": username, **claims})


async def _set_default(
    client: httpx.AsyncClient,
    token: str,
    profile_id: int,
    *,
    extra_params: dict[str, Any] | None = None,
) -> httpx.Response:
    params: dict[str, Any] = {"profile_id": profile_id}
    if extra_params:
        params.update(extra_params)
    return await client.post(
        "/api/v1/profiles/set-default",
        headers={"Authorization": f"Bearer {token}"},
        params=params,
    )


async def _create_profile(client: httpx.AsyncClient, token: str, body: dict[str, Any]) -> httpx.Response:
    return await client.post(
        "/api/v1/profiles/create",
        headers={"Authorization": f"Bearer {token}"},
        json=body,
    )


async def _update_profile(
    client: httpx.AsyncClient,
    token: str,
    profile_id: int,
    body: dict[str, Any],
) -> httpx.Response:
    return await client.post(
        "/api/v1/profiles/update",
        headers={"Authorization": f"Bearer {token}"},
        params={"profile_id": profile_id},
        json=body,
    )


async def _profile_snapshot(session_factory: async_sessionmaker[AsyncSession]) -> tuple[dict[str, Any], ...]:
    async with session_factory() as session:
        profiles = (await session.execute(select(Profile).order_by(Profile.id))).scalars().all()
        return tuple(
            {
                "id": profile.id,
                "uid": profile.uid,
                "name": profile.name,
                "prompt_id": profile.prompt_id,
                "configs": deepcopy(profile.configs or {}),
                "is_default": profile.is_default,
            }
            for profile in profiles
        )


def _assert_standard(response: httpx.Response, code: int) -> dict[str, Any]:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert isinstance(payload["message"], str)
    assert payload["message"]
    assert payload["data"] is None
    return payload


def _assert_profile_success(response: httpx.Response) -> dict[str, Any]:
    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 200
    assert isinstance(payload["message"], str)
    assert payload["message"]
    assert isinstance(payload["data"], dict)
    return payload["data"]


def _assert_only_default_scope_changed(
    before: tuple[dict[str, Any], ...],
    after: tuple[dict[str, Any], ...],
    uid: str | None,
    target_id: int,
) -> None:
    before_by_id = {row["id"]: row for row in before}
    after_by_id = {row["id"]: row for row in after}
    assert set(after_by_id) == set(before_by_id)

    for profile_id, before_row in before_by_id.items():
        after_row = after_by_id[profile_id]
        if before_row["uid"] != uid:
            assert after_row == before_row
            continue
        assert {key: value for key, value in after_row.items() if key != "is_default"} == {key: value for key, value in before_row.items() if key != "is_default"}

    owner_rows = [row for row in after if row["uid"] == uid]
    assert sum(1 for row in owner_rows if row["is_default"]) == 1
    assert next(row for row in owner_rows if row["id"] == target_id)["is_default"] is True


@pytest.mark.asyncio
async def test_regular_user_can_set_own_default_but_cannot_forge_owner_or_role(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    own_target_id = profile_ids["user-a"]["target"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before_own = await _profile_snapshot(session_factory)
        own_response = await _set_default(client, _token("user-a"), own_target_id)
        _assert_standard(own_response, 200)
        after_own = await _profile_snapshot(session_factory)
        _assert_only_default_scope_changed(before_own, after_own, "user-a", own_target_id)

        forged_token = _token("user-a", is_superuser=True, uid="user-b")
        for uid in ("user-b", None):
            before_forbidden = await _profile_snapshot(session_factory)
            forbidden_response = await _set_default(
                client,
                forged_token,
                profile_ids[uid]["target"],
                extra_params={"is_superuser": "true", "uid": "user-b"},
            )
            _assert_standard(forbidden_response, 403)
            assert await _profile_snapshot(session_factory) == before_forbidden


@pytest.mark.asyncio
async def test_superuser_can_set_defaults_for_every_owner_without_cross_owner_mutation(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for uid in ("admin", "user-b", None):
            target_id = profile_ids[uid]["target"]
            before = await _profile_snapshot(session_factory)
            response = await _set_default(client, _token("admin"), target_id)
            _assert_standard(response, 200)
            after = await _profile_snapshot(session_factory)
            _assert_only_default_scope_changed(before, after, uid, target_id)


@pytest.mark.asyncio
async def test_missing_profile_returns_404_without_mutation(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, _profile_ids = api_context
    before = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _set_default(client, _token("user-a"), 999999)
    _assert_standard(response, 404)
    assert await _profile_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_invalid_target_without_chat_rule_returns_400_without_changing_defaults(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-b"]["target"]
    async with session_factory() as session:
        target = await session.get(Profile, target_id)
        assert target is not None
        invalid_configs = ProfileConfig.model_validate(target.configs).model_dump()
        invalid_configs["channel"]["chat_channel"]["rules"] = []
        target.configs = invalid_configs
        await session.commit()

    before = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _set_default(client, _token("admin"), target_id)
    _assert_standard(response, 400)
    assert await _profile_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_superuser_clears_defaults_across_more_than_one_hundred_profiles(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    early_target_id = profile_ids["user-b"]["target"]
    async with session_factory() as session:
        early_target = await session.get(Profile, early_target_id)
        assert early_target is not None
        valid_configs = ProfileConfig.model_validate(early_target.configs).model_dump()
        session.add_all(
            [
                Profile(
                    uid="user-b",
                    name=f"user-b-filler-{index}",
                    configs=ProfileConfig.model_validate(valid_configs).model_dump(),
                    is_default=False,
                )
                for index in range(100)
            ]
        )
        await session.commit()
        late_target = Profile(
            uid="user-b",
            name="user-b-late-target",
            configs=ProfileConfig.model_validate(valid_configs).model_dump(),
            is_default=False,
        )
        session.add(late_target)
        await session.commit()
        assert late_target.id is not None
        late_target_id = int(late_target.id)

    before = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        late_response = await _set_default(client, _token("admin"), late_target_id)
        _assert_standard(late_response, 200)
        after_late = await _profile_snapshot(session_factory)
        _assert_only_default_scope_changed(before, after_late, "user-b", late_target_id)

        early_response = await _set_default(client, _token("admin"), early_target_id)
        _assert_standard(early_response, 200)
    after_early = await _profile_snapshot(session_factory)
    _assert_only_default_scope_changed(before, after_early, "user-b", early_target_id)


@pytest.mark.asyncio
async def test_repeated_set_default_succeeds_with_exactly_one_default(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = await _profile_snapshot(session_factory)
        first_response = await _set_default(client, _token("user-a"), target_id)
        _assert_standard(first_response, 200)
        after_first = await _profile_snapshot(session_factory)
        _assert_only_default_scope_changed(before, after_first, "user-a", target_id)

        second_response = await _set_default(client, _token("user-a"), target_id)
        _assert_standard(second_response, 200)
    after_second = await _profile_snapshot(session_factory)
    _assert_only_default_scope_changed(after_first, after_second, "user-a", target_id)


@pytest.mark.asyncio
async def test_profile_list_is_scoped_to_regular_user_across_pages_and_forged_identity(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    own_ids = {profile_ids["user-a"]["default"], profile_ids["user-a"]["target"]}
    forged_token = _token("user-a", is_superuser=True, uid="user-b")
    headers = {"Authorization": f"Bearer {forged_token}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/profiles/list",
            headers=headers,
            params={
                "page": 1,
                "size": 10,
                "uid": "user-b",
                "is_superuser": "true",
                "owner_username": "user-b",
            },
        )
        assert response.status_code == 200
        payload = response.json()
        assert set(payload) == {"code", "message", "data"}
        assert payload["code"] == 200
        assert isinstance(payload["message"], str)
        assert payload["message"]
        data = payload["data"]
        assert set(data) == {"items", "total", "page", "size", "meta"}
        assert {item["id"] for item in data["items"]} == own_ids
        assert all(item["uid"] == "user-a" for item in data["items"])
        assert data["total"] == 2
        assert data["page"] == 1
        assert data["size"] == 10
        assert data["meta"]["show_owner"] is False
        assert data["meta"]["current_uid"] == "user-a"
        assert data["meta"]["config_permissions"]["security"] is False

        page_responses = [
            await client.get(
                "/api/v1/profiles/list",
                headers=headers,
                params={
                    "page": page,
                    "size": 1,
                    "uid": "user-b",
                    "is_superuser": "true",
                    "owner_username": "user-b",
                },
            )
            for page in (1, 2)
        ]
        page_ids = []
        ordered_page_ids = []
        for page, page_response in zip((1, 2), page_responses, strict=True):
            assert page_response.status_code == 200
            page_data = page_response.json()["data"]
            assert len(page_data["items"]) == 1
            assert page_data["total"] == 2
            assert page_data["page"] == page
            assert page_data["size"] == 1
            assert page_data["meta"]["show_owner"] is False
            assert page_data["meta"]["current_uid"] == "user-a"
            item_id = page_data["items"][0]["id"]
            page_ids.append({item_id})
            ordered_page_ids.append(item_id)
        assert page_ids[0].isdisjoint(page_ids[1])
        assert page_ids[0] | page_ids[1] == own_ids
        assert ordered_page_ids == sorted(own_ids)

        large_page_response = await client.get(
            "/api/v1/profiles/list",
            headers=headers,
            params={
                "page": 1,
                "size": 100,
                "uid": "user-b",
                "is_superuser": "true",
                "owner_username": "user-b",
            },
        )
    assert large_page_response.status_code == 200
    large_page_data = large_page_response.json()["data"]
    assert {item["id"] for item in large_page_data["items"]} == own_ids
    assert {item["uid"] for item in large_page_data["items"]} == {"user-a"}
    assert large_page_data["total"] == 2


@pytest.mark.asyncio
async def test_profile_list_as_superuser_shows_all_profiles_and_real_owner_names(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, _session_factory, profile_ids = api_context
    all_ids = {profile_id for owner_profiles in profile_ids.values() for profile_id in owner_profiles.values()}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/profiles/list",
            headers={"Authorization": f"Bearer {_token('admin')}"},
            params={"page": 1, "size": 100},
        )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 200
    assert isinstance(payload["message"], str)
    assert payload["message"]
    data = payload["data"]
    assert set(data) == {"items", "total", "page", "size", "meta"}
    assert len(data["items"]) == 8
    assert {item["id"] for item in data["items"]} == all_ids
    assert {item["uid"] for item in data["items"]} == {"admin", "user-a", "user-b", None}
    assert data["total"] == 8
    assert data["page"] == 1
    assert data["size"] == 100
    assert data["meta"]["show_owner"] is True
    assert data["meta"]["current_uid"] == "admin"
    assert data["meta"]["config_permissions"]["security"] is True
    for item in data["items"]:
        expected_username = item["uid"] if item["uid"] in {"admin", "user-a", "user-b"} else None
        assert item["username"] == expected_username


@pytest.mark.asyncio
async def test_profile_list_as_superuser_filters_by_owner_username(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, _session_factory, profile_ids = api_context
    user_a_ids = {profile_ids["user-a"]["default"], profile_ids["user-a"]["target"]}
    all_ids = {profile_id for owner_profiles in profile_ids.values() for profile_id in owner_profiles.values()}
    headers = {"Authorization": f"Bearer {_token('admin')}"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        page_responses = [
            await client.get(
                "/api/v1/profiles/list",
                headers=headers,
                params={"page": page, "size": 1, "owner_username": "  SER-A  "},
            )
            for page in (1, 2)
        ]
        page_ids = []
        ordered_page_ids = []
        for page, page_response in zip((1, 2), page_responses, strict=True):
            assert page_response.status_code == 200
            payload = page_response.json()
            assert set(payload) == {"code", "message", "data"}
            assert payload["code"] == 200
            data = payload["data"]
            assert set(data) == {"items", "total", "page", "size", "meta"}
            assert len(data["items"]) == 1
            assert data["total"] == 2
            assert data["page"] == page
            assert data["size"] == 1
            assert data["meta"]["show_owner"] is True
            assert data["meta"]["current_uid"] == "admin"
            item = data["items"][0]
            assert item["uid"] == "user-a"
            assert item["username"] == "user-a"
            page_ids.append({item["id"]})
            ordered_page_ids.append(item["id"])

        assert page_ids[0].isdisjoint(page_ids[1])
        assert page_ids[0] | page_ids[1] == user_a_ids
        assert ordered_page_ids == sorted(user_a_ids)

        blank_response = await client.get(
            "/api/v1/profiles/list",
            headers=headers,
            params={"page": 1, "size": 100, "owner_username": "   "},
        )
        assert blank_response.status_code == 200
        blank_data = blank_response.json()["data"]
        assert len(blank_data["items"]) == 8
        assert {item["id"] for item in blank_data["items"]} == all_ids
        assert blank_data["total"] == 8

        no_match_response = await client.get(
            "/api/v1/profiles/list",
            headers=headers,
            params={"page": 1, "size": 1, "owner_username": "no-such-owner"},
        )
        assert no_match_response.status_code == 200
        no_match_data = no_match_response.json()["data"]
        assert no_match_data["items"] == []
        assert no_match_data["total"] == 0

        for wildcard in ("%", "_"):
            wildcard_response = await client.get(
                "/api/v1/profiles/list",
                headers=headers,
                params={"page": 1, "size": 1, "owner_username": wildcard},
            )
            assert wildcard_response.status_code == 200
            wildcard_data = wildcard_response.json()["data"]
            assert wildcard_data["items"] == []
            assert wildcard_data["total"] == 0


@pytest.mark.asyncio
async def test_profile_list_for_user_without_profiles_is_empty(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, _session_factory, _profile_ids = api_context
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/profiles/list",
            headers={"Authorization": f"Bearer {_token('user-empty')}"},
        )

    assert response.status_code == 200
    payload = response.json()
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 200
    assert isinstance(payload["message"], str)
    assert payload["message"]
    data = payload["data"]
    assert data["items"] == []
    assert data["total"] == 0
    assert data["page"] == 1
    assert data["size"] == 10
    assert data["meta"]["show_owner"] is False
    assert data["meta"]["current_uid"] == "user-empty"


@pytest.mark.asyncio
async def test_profile_list_rejects_missing_and_invalid_tokens_without_mutation(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, _profile_ids = api_context
    before = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        missing_token_response = await client.get("/api/v1/profiles/list")
        _assert_standard(missing_token_response, 401)
        assert await _profile_snapshot(session_factory) == before

        invalid_token_response = await client.get(
            "/api/v1/profiles/list",
            headers={"Authorization": "Bearer not-a-valid-jwt"},
        )
        _assert_standard(invalid_token_response, 401)
    assert await _profile_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_regular_user_cannot_change_security_config_on_create_or_update(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    initial_snapshot = await _profile_snapshot(session_factory)
    target = next(row for row in initial_snapshot if row["id"] == target_id)
    channel_id = target["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]
    security_values = [
        ("audit_channel_id", channel_id),
        ("audit_model_id", "chat-model"),
        ("audit_threshold", 0),
        ("audit_depth", 1),
        ("audit_confirmation_timeout_seconds", 60),
        ("audit_report_language", "en"),
        ("protect_system_directories", False),
    ]

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        forged_token = _token("user-a", is_superuser=True)
        for index, (field, value) in enumerate(security_values):
            nested = index % 2 == 0
            configs = {"security": {field: value}} if nested else {field: value}
            before_create = await _profile_snapshot(session_factory)
            create_response = await _create_profile(
                client,
                forged_token,
                {"name": f"forbidden-security-create-{index}", "configs": configs},
            )
            _assert_standard(create_response, 403)
            assert await _profile_snapshot(session_factory) == before_create == initial_snapshot

            nested = not nested
            configs = {"security": {field: value}} if nested else {field: value}
            before_update = await _profile_snapshot(session_factory)
            update_response = await _update_profile(
                client,
                forged_token,
                target_id,
                {"configs": configs},
            )
            _assert_standard(update_response, 403)
            assert await _profile_snapshot(session_factory) == before_update == initial_snapshot

        legacy_nested = {"security": {"audit_confirmation_timeout_minutes": 1}}
        legacy_flat = {"audit_confirmation_timeout_minutes": 1}
        for endpoint, configs in (("create", legacy_nested), ("update", legacy_flat)):
            before = await _profile_snapshot(session_factory)
            if endpoint == "create":
                response = await _create_profile(
                    client,
                    forged_token,
                    {"name": "forbidden-security-create-legacy", "configs": configs},
                )
            else:
                response = await _update_profile(client, forged_token, target_id, {"configs": configs})
            _assert_standard(response, 403)
            assert await _profile_snapshot(session_factory) == before == initial_snapshot


@pytest.mark.asyncio
async def test_security_defaults_and_superuser_security_create_are_persisted(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    default_security = ProfileConfig.model_validate({}).model_dump()["security"]
    starting_snapshot = await _profile_snapshot(session_factory)
    source_profile = next(row for row in starting_snapshot if row["id"] == profile_ids["user-a"]["target"])
    channel_id = source_profile["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]
    non_default_security = {
        "audit_channel_id": channel_id,
        "audit_model_id": "chat-model",
        "audit_threshold": 6,
        "audit_depth": 7,
        "audit_confirmation_timeout_seconds": 420,
        "audit_report_language": "en",
        "protect_system_directories": False,
    }
    created_ids = []

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        for token, body, expected_security in (
            (
                _token("user-a"),
                {"name": "security-default-omitted", "configs": {}},
                default_security,
            ),
            (
                _token("user-a"),
                {"name": "security-default-explicit", "configs": {"security": default_security}},
                default_security,
            ),
            (
                _token("admin"),
                {"uid": "user-b", "name": "security-admin-created", "configs": {"security": non_default_security}},
                non_default_security,
            ),
        ):
            response_data = _assert_profile_success(await _create_profile(client, token, body))
            created_ids.append(response_data["id"])
            assert response_data["configs"]["security"] == expected_security

            snapshot = await _profile_snapshot(session_factory)
            created_profile = next(row for row in snapshot if row["id"] == response_data["id"])
            assert created_profile["configs"]["security"] == expected_security
            assert response_data["configs"] == created_profile["configs"]
            assert {row["id"]: row for row in snapshot if row["id"] not in created_ids} == {row["id"]: row for row in starting_snapshot}


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ["admin", "user-a"])
async def test_admin_and_regular_updates_preserve_existing_configs_for_omitted_or_equal_security(
    actor: str,
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    initial_snapshot = await _profile_snapshot(session_factory)
    target = next(row for row in initial_snapshot if row["id"] == target_id)
    channel_id = target["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]
    security_values = {
        "audit_channel_id": channel_id,
        "audit_model_id": "chat-model",
        "audit_threshold": 6,
        "audit_depth": 7,
        "audit_confirmation_timeout_seconds": 420,
        "audit_report_language": "en",
        "protect_system_directories": False,
    }
    admin_configs = deepcopy(target["configs"])
    admin_configs["security"] = security_values
    admin_configs["channel"]["chat_channel"]["chat_timeout"] = 97.0
    admin_configs["channel"]["context_summary_channel"]["chat_timeout"] = 43.0
    admin_configs["tool"]["max_parallel_tools"] = 9
    admin_configs["other"]["context_summary_threshold_percent"] = 80
    admin_configs["memory"].update(
        {
            "precheck_enabled": False,
            "top_k": 8,
            "candidate_k": 20,
            "result_max_chars": 6500,
            "chat_history": {"top_k": 7, "candidate_k": 50, "result_max_chars": 7000},
            "knowledge": {"top_k": 6, "candidate_k": 30, "result_max_chars": 8000},
        }
    )
    expected_configs = deepcopy(admin_configs)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        admin_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": admin_configs},
        )
        admin_data = _assert_profile_success(admin_response)
        assert admin_data["configs"]["security"] == security_values
        assert admin_data["configs"] == expected_configs
        after_admin = await _profile_snapshot(session_factory)
        persisted_target = next(row for row in after_admin if row["id"] == target_id)
        assert persisted_target["configs"]["security"] == security_values
        assert persisted_target["configs"] == expected_configs

        unchanged_profiles = {row["id"]: row for row in after_admin if row["id"] != target_id}
        same_security_cases = [
            ("security-omitted", {"configs": {"tool": {"tool_timeout": 31.0}}}),
            ("security-empty", {"configs": {"security": {}, "tool": {"tool_timeout": 32.0}}}),
            ("security-partial-same", {"configs": {"security": {"audit_threshold": 6}, "tool": {"tool_timeout": 33.0}}}),
            ("security-complete-same", {"configs": {"security": security_values, "tool": {"tool_timeout": 34.0}}}),
            (
                "security-legacy-timeout-same",
                {"configs": {"security": {"audit_confirmation_timeout_minutes": 7}, "tool": {"tool_timeout": 35.0}}},
            ),
            ("configs-null", {"configs": None}),
            ("configs-empty", {"configs": {}}),
            ("configs-omitted", {}),
        ]
        for case_name, body in same_security_cases:
            body = {"name": f"{actor}-{case_name}", **body}
            submitted_configs = body.get("configs")
            if isinstance(submitted_configs, dict):
                submitted_tool = submitted_configs.get("tool")
                if isinstance(submitted_tool, dict) and "tool_timeout" in submitted_tool:
                    expected_configs["tool"]["tool_timeout"] = submitted_tool["tool_timeout"]

            first_response = await _update_profile(client, _token(actor), target_id, body)
            first_data = _assert_profile_success(first_response)
            after_first = await _profile_snapshot(session_factory)
            first_target = next(row for row in after_first if row["id"] == target_id)
            assert first_target["configs"]["security"] == security_values
            assert first_data["configs"]["security"] == security_values
            assert first_target["configs"] == expected_configs
            assert first_data["configs"] == expected_configs
            assert first_target["name"] == body["name"]
            assert {row["id"]: row for row in after_first if row["id"] != target_id} == unchanged_profiles

            second_response = await _update_profile(client, _token(actor), target_id, body)
            second_data = _assert_profile_success(second_response)
            after_second = await _profile_snapshot(session_factory)
            second_target = next(row for row in after_second if row["id"] == target_id)
            assert after_second == after_first
            assert second_target["configs"]["security"] == security_values
            assert second_target["configs"] == expected_configs
            assert second_data["configs"] == expected_configs
            assert {row["id"]: row for row in after_second if row["id"] != target_id} == unchanged_profiles


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "expected_changes"),
    [
        pytest.param(
            {
                "security": {"audit_threshold": 0, "protect_system_directories": False},
                "memory": {"precheck_enabled": False},
                "tool": {"enabled_tools": [], "firecrawl_api_key": None},
            },
            (
                (("security", "audit_threshold"), 0),
                (("security", "protect_system_directories"), False),
                (("memory", "precheck_enabled"), False),
                (("tool", "enabled_tools"), []),
                (("tool", "firecrawl_api_key"), None),
            ),
            id="explicit-falsy-values",
        ),
        pytest.param(
            {"channel": {"chat_channel": {"chat_timeout": 19.0}}},
            ((("channel", "chat_channel", "chat_timeout"), 19.0),),
            id="single-channel-leaf",
        ),
        pytest.param(
            {
                "memory": {
                    "top_k": 25,
                    "chat_history": {"top_k": 18},
                    "knowledge": {"top_k": 24},
                }
            },
            (
                (("memory", "top_k"), 25),
                (("memory", "chat_history", "top_k"), 18),
                (("memory", "knowledge", "top_k"), 24),
            ),
            id="memory-top-k-leaves",
        ),
        pytest.param(
            {"rerank_channel": {"kb_query_top_k": 26}},
            ((("memory", "knowledge", "top_k"), 26),),
            id="legacy-rerank-top-k",
        ),
        pytest.param(
            {
                "security": {"audit_channel_id": None, "audit_model_id": None},
                "channel": {"context_summary_channel": {"rules": []}},
            },
            (
                (("security", "audit_channel_id"), None),
                (("security", "audit_model_id"), None),
                (("channel", "context_summary_channel", "rules"), []),
            ),
            id="explicit-nulls-and-empty-list",
        ),
        pytest.param(
            {
                "security": {},
                "memory": {"chat_history": {}, "knowledge": {}},
                "channel": {"chat_channel": {}, "context_summary_channel": {}},
                "tool": {},
            },
            (),
            id="nested-empty-dictionaries",
        ),
        pytest.param(
            {"audit_confirmation_timeout_minutes": 2, "memory_top_k": 27},
            (
                (("security", "audit_confirmation_timeout_seconds"), 120),
                (("memory", "top_k"), 27),
            ),
            id="legacy-flat-fields",
        ),
    ],
)
async def test_admin_partial_update_replaces_only_explicit_config_leaves(
    patch: dict[str, Any],
    expected_changes: tuple[tuple[tuple[str, ...], Any], ...],
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    before_seed = await _profile_snapshot(session_factory)
    target = next(row for row in before_seed if row["id"] == target_id)
    channel_id = target["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]

    seed_configs = deepcopy(target["configs"])
    seed_configs["security"].update(
        {
            "audit_channel_id": channel_id,
            "audit_model_id": "chat-model",
            "audit_threshold": 6,
            "protect_system_directories": True,
        }
    )
    seed_configs["channel"]["chat_channel"]["chat_timeout"] = 97.0
    seed_configs["channel"]["context_summary_channel"]["chat_timeout"] = 43.0
    seed_configs["channel"]["context_summary_channel"]["rules"] = deepcopy(seed_configs["channel"]["chat_channel"]["rules"])
    seed_configs["tool"].update({"firecrawl_api_key": "seed-firecrawl-api-key", "enabled_tools": ["file_tool"]})
    seed_configs["memory"].update(
        {
            "precheck_enabled": True,
            "top_k": 8,
            "candidate_k": 30,
            "result_max_chars": 6500,
            "chat_history": {"top_k": 7, "candidate_k": 80, "result_max_chars": 7000},
            "knowledge": {"top_k": 6, "candidate_k": 31, "result_max_chars": 8000},
        }
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        seed_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": seed_configs},
        )
        seed_data = _assert_profile_success(seed_response)
        assert seed_data["configs"] == seed_configs

        after_seed = await _profile_snapshot(session_factory)
        seeded_target = next(row for row in after_seed if row["id"] == target_id)
        assert seeded_target["configs"] == seed_configs
        unchanged_profiles = {row["id"]: row for row in after_seed if row["id"] != target_id}
        assert {row["id"]: row for row in after_seed if row["id"] != target_id} == {row["id"]: row for row in before_seed if row["id"] != target_id}

        expected_configs = deepcopy(seeded_target["configs"])
        for path, value in expected_changes:
            expected_parent = expected_configs
            for key in path[:-1]:
                expected_parent = expected_parent[key]
            expected_parent[path[-1]] = deepcopy(value)

        first_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": deepcopy(patch)},
        )
        first_data = _assert_profile_success(first_response)
        after_first = await _profile_snapshot(session_factory)
        first_target = next(row for row in after_first if row["id"] == target_id)
        assert first_data["configs"] == expected_configs
        assert first_target["configs"] == expected_configs
        assert {row["id"]: row for row in after_first if row["id"] != target_id} == unchanged_profiles

        second_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": deepcopy(patch)},
        )
        second_data = _assert_profile_success(second_response)
        after_second = await _profile_snapshot(session_factory)
        second_target = next(row for row in after_second if row["id"] == target_id)
        assert second_data == first_data
        assert after_second == after_first
        assert second_data["configs"] == expected_configs
        assert second_target["configs"] == expected_configs
        assert {row["id"]: row for row in after_second if row["id"] != target_id} == unchanged_profiles


@pytest.mark.asyncio
async def test_profile_config_permissions_support_a_single_field_restriction(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, profile_ids = api_context
    monkeypatch.setattr(
        profile_permissions_module,
        "PROFILE_CONFIG_SUPERUSER_ONLY_PATHS",
        ("security", "tool.max_parallel_tools"),
    )
    target_id = profile_ids["user-a"]["target"]
    initial_snapshot = await _profile_snapshot(session_factory)
    target = next(row for row in initial_snapshot if row["id"] == target_id)
    admin_configs = deepcopy(target["configs"])
    admin_configs["tool"]["max_parallel_tools"] = 9

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        admin_update = await _update_profile(client, _token("admin"), target_id, {"configs": admin_configs})
        admin_data = _assert_profile_success(admin_update)
        assert admin_data["configs"]["tool"]["max_parallel_tools"] == 9
        after_admin = await _profile_snapshot(session_factory)
        persisted_target = next(row for row in after_admin if row["id"] == target_id)
        assert persisted_target["configs"]["tool"]["max_parallel_tools"] == 9
        assert admin_data["configs"] == persisted_target["configs"]

        for token, expected in (
            (_token("user-a", is_superuser=True), {"security": False, "tool.max_parallel_tools": False}),
            (_token("admin"), {"security": True, "tool.max_parallel_tools": True}),
        ):
            list_response = await client.get(
                "/api/v1/profiles/list",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert list_response.status_code == 200
            assert list_response.json()["data"]["meta"]["config_permissions"] == expected

        allowed_response = await _update_profile(
            client,
            _token("user-a"),
            target_id,
            {"configs": {"tool": {"tool_timeout": 41.0}}},
        )
        allowed_data = _assert_profile_success(allowed_response)
        after_allowed = await _profile_snapshot(session_factory)
        allowed_target = next(row for row in after_allowed if row["id"] == target_id)
        assert allowed_target["configs"]["tool"]["max_parallel_tools"] == 9
        assert allowed_target["configs"]["tool"]["tool_timeout"] == 41.0
        assert allowed_data["configs"] == allowed_target["configs"]
        assert {row["id"]: row for row in after_allowed if row["id"] != target_id} == {row["id"]: row for row in after_admin if row["id"] != target_id}

        before_forbidden = await _profile_snapshot(session_factory)
        forbidden_response = await _update_profile(
            client,
            _token("user-a"),
            target_id,
            {"configs": {"tool": {"max_parallel_tools": 10, "tool_timeout": 55.0}}},
        )
        _assert_standard(forbidden_response, 403)
        assert await _profile_snapshot(session_factory) == before_forbidden


@pytest.mark.asyncio
async def test_profile_delete_forbidden_cross_owner_does_not_preview_or_mutate(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    foreign_target_id = profile_ids["user-b"]["target"]
    no_owner_target_id = profile_ids[None]["target"]
    regular_token = _token("user-a", is_superuser=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before_preview = await _profile_snapshot(session_factory)
        admin_preview_response = await client.post(
            "/api/v1/profiles/delete",
            headers={"Authorization": f"Bearer {_token('admin')}"},
            params={"profile_id": foreign_target_id},
        )
        assert admin_preview_response.status_code == 200
        admin_preview_payload = admin_preview_response.json()
        assert set(admin_preview_payload) == {"code", "message", "data"}
        assert admin_preview_payload["code"] == 200
        assert isinstance(admin_preview_payload["message"], str)
        assert admin_preview_payload["message"]
        admin_preview_data = admin_preview_payload["data"]
        assert admin_preview_data["requires_confirmation"] is True
        assert admin_preview_data["profile"]["id"] == foreign_target_id
        assert isinstance(admin_preview_data["impact_token"], str)
        assert admin_preview_data["impact_token"]
        impact_token = admin_preview_data["impact_token"]
        assert await _profile_snapshot(session_factory) == before_preview

        before_foreign_preview = await _profile_snapshot(session_factory)
        foreign_preview_response = await client.post(
            "/api/v1/profiles/delete",
            headers={"Authorization": f"Bearer {regular_token}"},
            params={"profile_id": foreign_target_id},
        )
        _assert_standard(foreign_preview_response, 403)
        assert await _profile_snapshot(session_factory) == before_foreign_preview

        before_foreign_confirm = await _profile_snapshot(session_factory)
        foreign_confirm_response = await client.post(
            "/api/v1/profiles/delete",
            headers={"Authorization": f"Bearer {regular_token}"},
            params={
                "profile_id": foreign_target_id,
                "confirm_impact": True,
                "impact_token": impact_token,
            },
        )
        _assert_standard(foreign_confirm_response, 403)
        assert await _profile_snapshot(session_factory) == before_foreign_confirm

        before_no_owner_preview = await _profile_snapshot(session_factory)
        no_owner_preview_response = await client.post(
            "/api/v1/profiles/delete",
            headers={"Authorization": f"Bearer {regular_token}"},
            params={"profile_id": no_owner_target_id},
        )
        _assert_standard(no_owner_preview_response, 403)
    assert await _profile_snapshot(session_factory) == before_no_owner_preview


@pytest.mark.asyncio
@pytest.mark.parametrize(("actor", "owner"), [("user-a", "user-a"), ("admin", "user-b")])
async def test_profile_delete_preview_and_confirmation_remove_only_target(
    actor: str,
    owner: str,
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids[owner]["target"]
    before = await _profile_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        preview_response = await client.post(
            "/api/v1/profiles/delete",
            headers={"Authorization": f"Bearer {_token(actor)}"},
            params={"profile_id": target_id},
        )
        assert preview_response.status_code == 200
        preview_payload = preview_response.json()
        assert set(preview_payload) == {"code", "message", "data"}
        assert preview_payload["code"] == 200
        assert isinstance(preview_payload["message"], str)
        assert preview_payload["message"]
        preview_data = preview_payload["data"]
        assert preview_data["requires_confirmation"] is True
        assert preview_data["profile"]["id"] == target_id
        assert isinstance(preview_data["impact_token"], str)
        assert preview_data["impact_token"]
        assert await _profile_snapshot(session_factory) == before

        confirmation_response = await client.post(
            "/api/v1/profiles/delete",
            headers={"Authorization": f"Bearer {_token(actor)}"},
            params={
                "profile_id": target_id,
                "confirm_impact": True,
                "impact_token": preview_data["impact_token"],
            },
        )

    assert confirmation_response.status_code == 200
    confirmation_payload = confirmation_response.json()
    assert set(confirmation_payload) == {"code", "message", "data"}
    assert confirmation_payload["code"] == 200
    assert isinstance(confirmation_payload["message"], str)
    assert confirmation_payload["message"]
    assert confirmation_payload["data"] == {"requires_confirmation": False}
    after = await _profile_snapshot(session_factory)
    assert after == tuple(row for row in before if row["id"] != target_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("patch", "expected_location"),
    [
        pytest.param(
            {"memory": {"candidate_k": 3}},
            "configs.memory",
            id="memory-candidate-below-top-k",
        ),
        pytest.param(
            {"memory": {"chat_history": {"candidate_k": 3}}},
            "configs.memory.chat_history",
            id="chat-history-candidate-below-top-k",
        ),
        pytest.param(
            {"memory": {"knowledge": {"candidate_k": 3}}},
            "configs.memory.knowledge",
            id="knowledge-candidate-below-top-k",
        ),
        pytest.param(
            None,
            "configs.memory",
            id="embedding-channel-without-model",
        ),
        pytest.param(
            {"memory": {"embedding_model_id": "unpaired-model"}},
            "configs.memory",
            id="embedding-model-without-channel",
        ),
        pytest.param(
            {"security": {"audit_depth": True}},
            "configs.security.audit_depth",
            id="boolean-audit-depth",
        ),
    ],
)
async def test_admin_partial_update_rejects_invalid_merged_config_without_mutation(
    patch: dict[str, Any] | None,
    expected_location: str,
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str | None, dict[str, int]]],
) -> None:
    app, session_factory, profile_ids = api_context
    target_id = profile_ids["user-a"]["target"]
    initial_snapshot = await _profile_snapshot(session_factory)
    target = next(row for row in initial_snapshot if row["id"] == target_id)
    channel_id = target["configs"]["channel"]["chat_channel"]["rules"][0]["channel_id"]

    seed_configs = deepcopy(target["configs"])
    seed_memory = seed_configs["memory"]
    seed_memory.update({"top_k": 8, "candidate_k": 20})
    seed_memory["chat_history"].update({"top_k": 7, "candidate_k": 50})
    seed_memory["knowledge"].update({"top_k": 6, "candidate_k": 30})

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        seed_response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"configs": seed_configs},
        )
        seed_data = _assert_profile_success(seed_response)
        assert seed_data["configs"] == seed_configs

        before = await _profile_snapshot(session_factory)
        seeded_target = next(row for row in before if row["id"] == target_id)
        assert seeded_target["configs"] == seed_configs
        assert {row["id"]: row for row in before if row["id"] != target_id} == {row["id"]: row for row in initial_snapshot if row["id"] != target_id}

        if patch is None:
            invalid_patch = {"memory": {"embedding_channel_id": channel_id}}
        else:
            invalid_patch = deepcopy(patch)
        response = await _update_profile(
            client,
            _token("admin"),
            target_id,
            {"name": "invalid-partial-update-must-not-rename", "configs": invalid_patch},
        )

    payload = _assert_standard(response, 422)
    assert expected_location in payload["message"]
    assert await _profile_snapshot(session_factory) == before
