from collections.abc import AsyncGenerator
from copy import deepcopy
from datetime import timedelta
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.crypto as crypto_module
import app.core.security as security
from app.api.v1 import channels
from app.api.v1.profile import router
from app.core.crud.channel.channel import channel_crud
from app.handler import register_handlers
from app.models.channel import ChannelCreate, ModelChannel
from app.models.profile import Profile
from app.models.user import User
from app.providers.database import get_db

TEST_JWT_SECRET = "profile-model-options-integration-jwt-secret"
TEST_PASSWORD_HASH = "fixed-profile-model-options-password-hash"

PUBLIC_CHANNEL_FIELDS = {"id", "name", "is_active", "model_ids"}
PUBLIC_MODEL_FIELDS = {
    "model_id",
    "usage",
    "protocol",
    "is_enabled",
    "context_window_k",
    "max_tokens",
    "reasoning_effort",
    "reasoning_efforts",
}
FORBIDDEN_RESPONSE_FIELDS = (
    "api_key",
    "base_url",
    "http_proxy",
    "advanced_settings",
    "custom_headers",
    "description",
    "lifecycle_status",
    "unknown_model_field",
)
SENSITIVE_RESPONSE_SENTINELS = (
    "active-plain-api-key-secret",
    "inactive-plain-api-key-secret",
    "https://private-active.example.internal/v1",
    "https://private-inactive.example.internal/v1",
    "http://private-active-proxy.internal:8080",
    "http://private-inactive-proxy.internal:8081",
    "active-description-secret",
    "Bearer active-authorization-secret",
    "active-header-secret",
    "active-unknown-field-secret",
    "active-second-description-secret",
    "active-provider-header-secret",
    "active-second-unknown-secret",
    "active-disabled-description-secret",
    "active-disabled-header-secret",
    "active-disabled-unknown-secret",
    "active-pending-description-secret",
    "active-pending-header-secret",
    "active-pending-unknown-secret",
    "inactive-description-secret",
    "Bearer inactive-authorization-secret",
    "inactive-header-secret",
    "inactive-unknown-field-secret",
    "inactive-second-description-secret",
    "inactive-provider-header-secret",
    "inactive-second-unknown-secret",
    "inactive-disabled-description-secret",
    "inactive-disabled-header-secret",
    "inactive-disabled-unknown-secret",
    "inactive-pending-description-secret",
    "inactive-pending-header-secret",
    "inactive-pending-unknown-secret",
)


def _model_entries(prefix: str) -> list[dict[str, Any]]:
    return [
        {
            "model_id": f"{prefix}-legacy-chat",
            "usage": "CHAT",
            "protocol": "OPENAI",
            "is_enabled": True,
            "context_window_k": 64,
            "max_tokens": 2048,
            "reasoning_effort": "legacy-medium",
            "description": f"{prefix}-description-secret",
            "advanced_settings": {
                "custom_headers": {
                    "Authorization": f"Bearer {prefix}-authorization-secret",
                    "X-Private-Request": f"{prefix}-header-secret",
                }
            },
            "unknown_model_field": f"{prefix}-unknown-field-secret",
        },
        {
            "model_id": f"{prefix}-reasoning-chat",
            "usage": "CHAT",
            "protocol": "OPENAI_RESPONSES",
            "is_enabled": True,
            "context_window_k": 128,
            "max_tokens": 4096,
            "reasoning_effort": "legacy-high",
            "reasoning_efforts": ["low", "high"],
            "description": f"{prefix}-second-description-secret",
            "advanced_settings": {
                "custom_headers": {
                    "X-Provider-Secret": f"{prefix}-provider-header-secret",
                }
            },
            "unknown_model_field": f"{prefix}-second-unknown-secret",
        },
        {
            "model_id": f"{prefix}-disabled-embedding",
            "usage": "EMBEDDING",
            "protocol": "OPENAI_EMBEDDING",
            "is_enabled": False,
            "lifecycle_status": "active",
            "description": f"{prefix}-disabled-description-secret",
            "advanced_settings": {
                "custom_headers": {
                    "X-Disabled-Secret": f"{prefix}-disabled-header-secret",
                }
            },
            "unknown_model_field": f"{prefix}-disabled-unknown-secret",
        },
        {
            "model_id": f"{prefix}-pending-embedding",
            "usage": "EMBEDDING",
            "protocol": "OPENAI_EMBEDDING",
            "is_enabled": False,
            "lifecycle_status": "pending_delete",
            "description": f"{prefix}-pending-description-secret",
            "advanced_settings": {
                "custom_headers": {
                    "X-Pending-Secret": f"{prefix}-pending-header-secret",
                }
            },
            "unknown_model_field": f"{prefix}-pending-unknown-secret",
        },
    ]


def _public_models(models: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{field: model[field] for field in PUBLIC_MODEL_FIELDS if field in model} for model in models]


async def _channel_snapshot(session_factory: async_sessionmaker[AsyncSession]) -> tuple[dict[str, Any], ...]:
    async with session_factory() as session:
        rows = (await session.execute(select(ModelChannel).order_by(ModelChannel.id))).scalars().all()
        return tuple(
            {
                "id": row.id,
                "name": row.name,
                "api_key": row.api_key,
                "base_url": row.base_url,
                "http_proxy": row.http_proxy,
                "is_active": row.is_active,
                "model_ids": deepcopy(row.model_ids or []),
            }
            for row in rows
        )


@pytest_asyncio.fixture
async def api_context(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[FastAPI, async_sessionmaker[AsyncSession], list[dict[str, Any]]]]:
    monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x00" * 32)

    async with database_factory(foreign_keys=True) as session_factory:
        active_models = _model_entries("active")
        inactive_models = _model_entries("inactive")

        async with session_factory() as session:
            active_channel = await channel_crud.create_with_plain_api_key(
                session,
                obj_in=ChannelCreate(
                    name="active-private-channel",
                    api_key="active-plain-api-key-secret",
                    base_url="https://private-active.example.internal/v1",
                    http_proxy="http://private-active-proxy.internal:8080",
                    is_active=True,
                    model_ids=active_models,
                ),
                commit=False,
            )
            inactive_channel = await channel_crud.create_with_plain_api_key(
                session,
                obj_in=ChannelCreate(
                    name="inactive-private-channel",
                    api_key="inactive-plain-api-key-secret",
                    base_url="https://private-inactive.example.internal/v1",
                    http_proxy="http://private-inactive-proxy.internal:8081",
                    is_active=False,
                    model_ids=inactive_models,
                ),
                commit=False,
            )
            session.add_all(
                [
                    User(
                        uid="ordinary",
                        username="ordinary",
                        is_active=True,
                        is_superuser=False,
                        hashed_password=TEST_PASSWORD_HASH,
                    ),
                    User(
                        uid="admin",
                        username="admin",
                        is_active=True,
                        is_superuser=True,
                        hashed_password=TEST_PASSWORD_HASH,
                    ),
                    User(
                        uid="disabled",
                        username="disabled",
                        is_active=False,
                        is_superuser=False,
                        hashed_password=TEST_PASSWORD_HASH,
                    ),
                ]
            )
            await session.commit()

        assert active_channel.id is not None
        assert inactive_channel.id is not None
        expected_items = [
            {
                "id": int(active_channel.id),
                "name": active_channel.name,
                "is_active": active_channel.is_active,
                "model_ids": _public_models(active_models),
            },
            {
                "id": int(inactive_channel.id),
                "name": inactive_channel.name,
                "is_active": inactive_channel.is_active,
                "model_ids": _public_models(inactive_models),
            },
        ]
        expected_items.sort(key=lambda item: item["id"], reverse=True)

        monkeypatch.setattr(security, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

        app = FastAPI()
        register_handlers(app)
        app.include_router(router, prefix="/api/v1")
        app.include_router(channels.router, prefix="/api/v1")

        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        yield app, session_factory, expected_items


def _token(username: str, *, expires_delta: timedelta | None = None) -> str:
    return security.create_access_token(data={"sub": username}, expires_delta=expires_delta)


def _assert_standard_error(response: httpx.Response, status_code: int) -> None:
    payload = response.json()
    assert response.status_code == status_code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == status_code
    assert isinstance(payload["message"], str)
    assert payload["message"]
    assert payload["data"] is None


def _assert_model_options_page(
    response: httpx.Response,
    *,
    expected_items: list[dict[str, Any]],
    total: int,
    page: int,
    size: int,
) -> dict[str, Any]:
    payload = response.json()
    assert response.status_code == 200
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 200
    assert isinstance(payload["message"], str)
    assert payload["message"]
    data = payload["data"]
    assert set(data) == {"items", "total", "page", "size", "meta"}
    assert data["items"] == expected_items
    assert data["total"] == total
    assert data["page"] == page
    assert data["size"] == size
    assert data["meta"] is None

    for channel in data["items"]:
        assert set(channel) == PUBLIC_CHANNEL_FIELDS
        for model in channel["model_ids"]:
            assert set(model) <= PUBLIC_MODEL_FIELDS

    for field in FORBIDDEN_RESPONSE_FIELDS:
        assert f'"{field}"' not in response.text
    for sentinel in SENSITIVE_RESPONSE_SENTINELS:
        assert sentinel not in response.text
    return payload


@pytest.mark.asyncio
@pytest.mark.parametrize("username", ("ordinary", "admin"))
async def test_model_options_is_read_only_and_exposes_only_public_model_fields(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], list[dict[str, Any]]],
    username: str,
) -> None:
    app, session_factory, expected_items = api_context
    token = _token(username)

    before = await _channel_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        first_response = await client.get(
            "/api/v1/profiles/model-options",
            headers={"Authorization": f"Bearer {token}"},
            params={"page": 1, "size": 100},
        )
        first_payload = _assert_model_options_page(
            first_response,
            expected_items=expected_items,
            total=2,
            page=1,
            size=100,
        )
        assert await _channel_snapshot(session_factory) == before

        second_response = await client.get(
            "/api/v1/profiles/model-options",
            headers={"Authorization": f"Bearer {token}"},
            params={"page": 1, "size": 100},
        )
        second_payload = _assert_model_options_page(
            second_response,
            expected_items=expected_items,
            total=2,
            page=1,
            size=100,
        )

    assert second_payload == first_payload
    assert await _channel_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_model_options_size_one_pagination_has_no_missing_or_duplicate_channels(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], list[dict[str, Any]]],
) -> None:
    app, session_factory, expected_items = api_context
    expected_ids = {item["id"] for item in expected_items}
    token = _token("ordinary")
    before = await _channel_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        page_payloads = []
        for page, expected_page in enumerate(expected_items, start=1):
            response = await client.get(
                "/api/v1/profiles/model-options",
                headers={"Authorization": f"Bearer {token}"},
                params={"page": page, "size": 1},
            )
            page_payloads.append(
                _assert_model_options_page(
                    response,
                    expected_items=[expected_page],
                    total=2,
                    page=page,
                    size=1,
                )
            )

        empty_response = await client.get(
            "/api/v1/profiles/model-options",
            headers={"Authorization": f"Bearer {token}"},
            params={"page": 3, "size": 1},
        )
        empty_payload = _assert_model_options_page(
            empty_response,
            expected_items=[],
            total=2,
            page=3,
            size=1,
        )

    page_ids = [{item["id"] for item in payload["data"]["items"]} for payload in page_payloads]
    assert page_ids[0].isdisjoint(page_ids[1])
    assert page_ids[0] | page_ids[1] == expected_ids
    assert empty_payload["data"]["items"] == []
    assert await _channel_snapshot(session_factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("credential", ("missing", "invalid", "expired", "disabled"))
async def test_model_options_rejects_unauthorized_credentials_without_mutation(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], list[dict[str, Any]]],
    credential: str,
) -> None:
    app, session_factory, _expected_items = api_context
    headers = {
        "invalid": {"Authorization": "Bearer not-a-valid-jwt"},
        "expired": {"Authorization": f"Bearer {_token('ordinary', expires_delta=timedelta(seconds=-1))}"},
        "disabled": {"Authorization": f"Bearer {_token('disabled')}"},
    }.get(credential, {})
    before = await _channel_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/api/v1/profiles/model-options", headers=headers)

    _assert_standard_error(response, 401)
    assert await _channel_snapshot(session_factory) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("params", ({"page": 0}, {"size": 0}, {"size": 1001}))
async def test_model_options_rejects_invalid_pagination_for_authenticated_user(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], list[dict[str, Any]]],
    params: dict[str, int],
) -> None:
    app, session_factory, _expected_items = api_context
    before = await _channel_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get(
            "/api/v1/profiles/model-options",
            headers={"Authorization": f"Bearer {_token('ordinary')}"},
            params=params,
        )

    _assert_standard_error(response, 422)
    assert await _channel_snapshot(session_factory) == before


@pytest.mark.asyncio
async def test_ordinary_user_can_use_model_options_for_own_profile_workflow_without_channel_access(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], list[dict[str, Any]]],
) -> None:
    app, session_factory, expected_items = api_context
    token = _token("ordinary")
    headers = {"Authorization": f"Bearer {token}"}
    before_channels = await _channel_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        model_options_response = await client.get(
            "/api/v1/profiles/model-options",
            headers=headers,
            params={"page": 1, "size": 100},
        )
        model_options_payload = _assert_model_options_page(
            model_options_response,
            expected_items=expected_items,
            total=2,
            page=1,
            size=100,
        )
        selected_channel = next(item for item in model_options_payload["data"]["items"] if item["is_active"] is True)
        selected_model = next(model for model in selected_channel["model_ids"] if model["usage"] == "CHAT" and model["is_enabled"] is True)
        channel_id = selected_channel["id"]
        model_id = selected_model["model_id"]
        configs = {
            "memory": {"enabled": False},
            "channel": {
                "chat_channel": {
                    "rules": [
                        {
                            "channel_id": channel_id,
                            "model_id": model_id,
                            "priority": 1,
                            "weight": 1,
                        }
                    ]
                }
            },
        }
        selected_rule = configs["channel"]["chat_channel"]["rules"][0]

        def assert_selected_rule(profile_configs: dict[str, Any]) -> None:
            rules = profile_configs["channel"]["chat_channel"]["rules"]
            assert len(rules) == 1
            rule = rules[0]
            assert {field: rule[field] for field in selected_rule} == selected_rule

        list_response = await client.get(
            "/api/v1/channels/list",
            headers=headers,
            params={"page": 1, "size": 10},
        )
        _assert_standard_error(list_response, 403)
        get_response = await client.get(
            "/api/v1/channels/get",
            headers=headers,
            params={"channel_id": channel_id},
        )
        _assert_standard_error(get_response, 403)

        create_response = await client.post(
            "/api/v1/profiles/create",
            headers=headers,
            json={"name": "ordinary-options-profile", "configs": configs},
        )
        assert create_response.status_code == 200
        create_payload = create_response.json()
        assert set(create_payload) == {"code", "message", "data"}
        assert create_payload["code"] == 200
        assert create_payload["data"]["uid"] == "ordinary"
        assert_selected_rule(create_payload["data"]["configs"])
        profile_id = create_payload["data"]["id"]

        update_response = await client.post(
            "/api/v1/profiles/update",
            headers=headers,
            params={"profile_id": profile_id},
            json={"name": "ordinary-options-updated", "configs": configs},
        )
        assert update_response.status_code == 200
        update_payload = update_response.json()
        assert set(update_payload) == {"code", "message", "data"}
        assert update_payload["code"] == 200
        assert update_payload["data"]["uid"] == "ordinary"
        assert update_payload["data"]["name"] == "ordinary-options-updated"
        assert_selected_rule(update_payload["data"]["configs"])

    async with session_factory() as session:
        profile = await session.get(Profile, profile_id)
        assert profile is not None
        assert profile.uid == "ordinary"
        assert profile.name == "ordinary-options-updated"
        assert_selected_rule(profile.configs)

    assert await _channel_snapshot(session_factory) == before_channels
