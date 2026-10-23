import json
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
from app.core.constants import ERR_ONLY_ADMIN_ALLOWED, ERR_UNAUTHORIZED
from app.core.crud.channel.channel import channel_crud
from app.core.i18n import t
from app.handler import register_handlers
from app.models.channel import ChannelCreate, ModelChannel
from app.models.message import InternalMessage, InternalResponse, MessageRole
from app.models.profile import Profile, ProfileConfig
from app.models.user import User
from app.providers.database import get_db
from app.providers.embedding import EmbeddingClient
from app.providers.image_generation import ImageGenerationClient
from app.providers.llm.client import LLMClient
from app.transformers.openai import OpenAIChatCompletionsTransformer
from app.transformers.openai import base as openai_transformer_base

TEST_JWT_SECRET = "channel-permissions-integration-jwt-secret"
TEST_PASSWORD_HASH = "fixed-channel-permissions-password-hash"

CHANNEL_MANAGEMENT_ROUTES = (
    "types",
    "list",
    "get",
    "create",
    "update",
    "delete",
    "models",
    "test-chat",
    "test-image-generation",
    "test-embedding-dimension",
)
UNAUTHORIZED_CREDENTIALS = ("missing", "invalid", "expired", "disabled")


def _model_ids() -> list[dict[str, Any]]:
    return [
        {
            "model_id": "chat-model",
            "usage": "CHAT",
            "protocol": "OPENAI",
            "context_window_k": 32,
            "max_tokens": 2048,
            "temperature": 0.25,
            "top_p": 0.8,
            "is_enabled": True,
        },
        {
            "model_id": "embedding-model",
            "usage": "EMBEDDING",
            "protocol": "OPENAI_EMBEDDING",
            "embedding_dimensions": 1536,
            "is_enabled": True,
        },
    ]


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


def _management_request(route_name: str, channel_id: int) -> dict[str, Any]:
    requests = {
        "types": {"method": "GET", "path": "/api/v1/channels/types"},
        "list": {
            "method": "GET",
            "path": "/api/v1/channels/list",
            "params": {"page": 1, "size": 10},
        },
        "get": {
            "method": "GET",
            "path": "/api/v1/channels/get",
            "params": {"channel_id": channel_id},
        },
        "create": {
            "method": "POST",
            "path": "/api/v1/channels/create",
            "json": {
                "name": "denied-create-channel",
                "api_key": "denied-create-api-key",
                "base_url": "https://llm.example/v1",
                "is_active": True,
                "model_ids": _model_ids(),
            },
        },
        "update": {
            "method": "POST",
            "path": "/api/v1/channels/update",
            "params": {"channel_id": channel_id},
            "json": {"name": "denied-update-channel"},
        },
        "delete": {
            "method": "POST",
            "path": "/api/v1/channels/delete",
            "params": {"channel_id": channel_id},
        },
        "models": {
            "method": "POST",
            "path": "/api/v1/channels/models",
            "json": {
                "api_key": "test-api-key",
                "base_url": "https://llm.example/v1",
                "timeout": 1,
            },
        },
        "test-chat": {
            "method": "POST",
            "path": "/api/v1/channels/test-chat",
            "json": {
                "api_key": "test-api-key",
                "base_url": "https://llm.example/v1",
                "model_id": "chat-model",
                "protocol": "OPENAI",
                "prompt": "permission test",
                "timeout": 1,
            },
        },
        "test-image-generation": {
            "method": "POST",
            "path": "/api/v1/channels/test-image-generation",
            "json": {
                "api_key": "test-api-key",
                "base_url": "https://llm.example/v1",
                "model_id": "image-model",
                "protocol": "OPENAI_IMAGE",
                "timeout": 1,
            },
        },
        "test-embedding-dimension": {
            "method": "POST",
            "path": "/api/v1/channels/test-embedding-dimension",
            "params": {"channel_id": channel_id, "model_id": "embedding-model"},
        },
    }
    return requests[route_name]


@pytest_asyncio.fixture
async def api_context(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[FastAPI, async_sessionmaker[AsyncSession], int, dict[str, int]]]:
    calls = {
        "list_models": 0,
        "generate": 0,
        "generate_with_stream_callback": 0,
        "generate_image": 0,
        "get_embeddings": 0,
    }

    async with database_factory(foreign_keys=True) as session_factory:
        monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x00" * 32)

        async with session_factory() as session:
            channel = await channel_crud.create_with_plain_api_key(
                session,
                obj_in=ChannelCreate(
                    name="channel-permissions-channel",
                    api_key="channel-permissions-api-key",
                    base_url="https://llm.example/v1",
                    is_active=True,
                    model_ids=_model_ids(),
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
                        hashed_password=TEST_PASSWORD_HASH,
                    ),
                    User(
                        uid="ordinary",
                        username="ordinary",
                        is_active=True,
                        is_superuser=False,
                        hashed_password=TEST_PASSWORD_HASH,
                    ),
                    User(
                        uid="disabled",
                        username="disabled",
                        is_active=False,
                        is_superuser=False,
                        hashed_password=TEST_PASSWORD_HASH,
                    ),
                    Profile(
                        uid="ordinary",
                        name="ordinary-profile",
                        configs=_profile_configs(channel_id),
                        is_default=True,
                    ),
                ]
            )
            await session.commit()

        monkeypatch.setattr(security, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

        def blocked_call(name: str):
            async def call(*args: Any, **kwargs: Any) -> None:
                calls[name] += 1
                raise AssertionError(f"unexpected external call: {name}")

            return call

        for name, target, attribute in (
            ("list_models", LLMClient, "list_models"),
            ("generate", LLMClient, "generate"),
            ("generate_with_stream_callback", LLMClient, "generate_with_stream_callback"),
            ("generate_image", ImageGenerationClient, "generate_image"),
            ("get_embeddings", EmbeddingClient, "get_embeddings"),
        ):
            monkeypatch.setattr(target, attribute, blocked_call(name))

        app = FastAPI()
        register_handlers(app)
        app.include_router(channels.router, prefix="/api/v1")

        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        yield app, session_factory, channel_id, calls


def _token(username: str, **claims: Any) -> str:
    return security.create_access_token(data={"sub": username, **claims})


async def _request(
    client: httpx.AsyncClient,
    request: dict[str, Any],
    token: str | None,
) -> httpx.Response:
    headers = {} if token is None else {"Authorization": f"Bearer {token}"}
    return await client.request(
        request["method"],
        request["path"],
        headers=headers,
        json=request.get("json"),
        params=request.get("params"),
    )


async def _state_snapshot(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[tuple[tuple[Any, ...], ...], tuple[tuple[Any, ...], ...]]:
    async with session_factory() as session:
        channel_rows = (await session.execute(select(ModelChannel).order_by(ModelChannel.id))).scalars().all()
        profile_rows = (await session.execute(select(Profile).order_by(Profile.id))).scalars().all()

        channels_snapshot = tuple(
            (
                row.id,
                row.name,
                row.api_key,
                row.base_url,
                row.http_proxy,
                row.is_active,
                deepcopy(row.model_ids or []),
            )
            for row in channel_rows
        )
        profiles_snapshot = tuple(
            (
                row.id,
                row.uid,
                row.name,
                row.prompt_id,
                deepcopy(row.configs or {}),
                row.is_default,
            )
            for row in profile_rows
        )
    return channels_snapshot, profiles_snapshot


def _assert_standard(response: httpx.Response, code: int, message: str) -> None:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert payload["message"] == message
    assert payload["data"] is None


def _assert_success(response: httpx.Response) -> Any:
    payload = response.json()
    assert response.status_code == 200
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 200
    assert isinstance(payload["message"], str)
    assert payload["message"]
    return payload["data"]


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", CHANNEL_MANAGEMENT_ROUTES)
@pytest.mark.parametrize(
    "token_claims",
    ({}, {"is_superuser": True}),
    ids=("ordinary-jwt", "forged-superuser-claim"),
)
async def test_ordinary_user_cannot_access_channel_management_routes(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], int, dict[str, int]],
    route_name: str,
    token_claims: dict[str, Any],
) -> None:
    app, session_factory, channel_id, calls = api_context
    token = _token("ordinary", **token_claims)
    request = _management_request(route_name, channel_id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = await _state_snapshot(session_factory)
        response = await _request(client, request, token)
        _assert_standard(response, 403, t(ERR_ONLY_ADMIN_ALLOWED))
        assert await _state_snapshot(session_factory) == before

    assert calls == {name: 0 for name in calls}


def _unauthorized_token(credential: str) -> str | None:
    if credential == "missing":
        return None
    if credential == "invalid":
        return "not-a-valid-jwt"
    if credential == "expired":
        return security.create_access_token(data={"sub": "ordinary"}, expires_delta=timedelta(seconds=-1))
    return _token("disabled")


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", CHANNEL_MANAGEMENT_ROUTES)
@pytest.mark.parametrize("credential", UNAUTHORIZED_CREDENTIALS)
async def test_channel_management_routes_reject_unauthorized_credentials_without_side_effects(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], int, dict[str, int]],
    route_name: str,
    credential: str,
) -> None:
    app, session_factory, channel_id, calls = api_context
    request = _management_request(route_name, channel_id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = await _state_snapshot(session_factory)
        response = await _request(client, request, _unauthorized_token(credential))
        _assert_standard(response, 401, t(ERR_UNAUTHORIZED))
        assert await _state_snapshot(session_factory) == before

    assert calls == {name: 0 for name in calls}


@pytest.mark.asyncio
async def test_superuser_can_complete_channel_management_workflow(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], int, dict[str, int]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, seed_channel_id, _calls = api_context
    token = _token("admin", is_superuser=False)
    headers = {"Authorization": f"Bearer {token}"}
    seed_state = await _state_snapshot(session_factory)

    http_models_sample = {"object": "list", "data": [{"id": "chat-model"}]}

    class FakeResponse:
        status = 200

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            return None

        async def text(self) -> str:
            return json.dumps(http_models_sample)

    class FakeClientSession:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def __aenter__(self) -> Any:
            return self

        async def __aexit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
            return None

        def get(self, url: str, *args: Any, **kwargs: Any) -> FakeResponse:
            assert url == "https://llm.example/v1/models"
            return FakeResponse()

    def chat_response() -> InternalResponse:
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="permission test reply"),
            model="chat-model",
            usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    async def generate(*args: Any, **kwargs: Any) -> InternalResponse:
        return chat_response()

    async def generate_with_stream_callback(*args: Any, **kwargs: Any) -> InternalResponse:
        await kwargs["on_content"]("reply")
        return chat_response()

    async def generate_image(*args: Any, **kwargs: Any) -> dict[str, list[dict[str, str]]]:
        return {"data": [{"url": "https://image.example/test.png"}]}

    async def get_embeddings(*args: Any, **kwargs: Any) -> dict[str, list[dict[str, list[float]]]]:
        return {"data": [{"embedding": [0.1, 0.2, 0.3]}]}

    monkeypatch.setattr(openai_transformer_base.aiohttp, "ClientSession", FakeClientSession)
    monkeypatch.setattr(openai_transformer_base.aiohttp, "TCPConnector", lambda *args, **kwargs: None)
    monkeypatch.setattr(LLMClient, "list_models", OpenAIChatCompletionsTransformer().list_models)
    monkeypatch.setattr(LLMClient, "generate", generate)
    monkeypatch.setattr(LLMClient, "generate_with_stream_callback", generate_with_stream_callback)
    monkeypatch.setattr(ImageGenerationClient, "generate_image", generate_image)
    monkeypatch.setattr(EmbeddingClient, "get_embeddings", get_embeddings)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        types_data = _assert_success(await client.get("/api/v1/channels/types", headers=headers))
        assert "CHAT" in types_data["model_usages"]
        assert "EMBEDDING" in types_data["model_usages"]

        list_data = _assert_success(
            await client.get(
                "/api/v1/channels/list",
                headers=headers,
                params={"page": 1, "size": 10},
            )
        )
        assert list_data["total"] == 1
        assert list_data["items"][0]["id"] == seed_channel_id
        assert list_data["items"][0]["api_key"] == "channel-permissions-api-key"

        get_data = _assert_success(
            await client.get(
                "/api/v1/channels/get",
                headers=headers,
                params={"channel_id": seed_channel_id},
            )
        )
        assert get_data["id"] == seed_channel_id
        assert get_data["api_key"] == "channel-permissions-api-key"

        create_data = _assert_success(
            await client.post(
                "/api/v1/channels/create",
                headers=headers,
                json={
                    "name": "admin-created-channel",
                    "api_key": "admin-created-api-key",
                    "base_url": "https://llm.example/v1",
                    "is_active": True,
                    "model_ids": _model_ids(),
                },
            )
        )
        created_channel_id = create_data["id"]
        assert create_data["api_key"] == "admin-created-api-key"
        assert {item["model_id"] for item in create_data["model_ids"]} == {"chat-model", "embedding-model"}

        renamed = _assert_success(
            await client.post(
                "/api/v1/channels/update",
                headers=headers,
                params={"channel_id": created_channel_id},
                json={"name": "admin-renamed-channel", "is_active": False},
            )
        )
        assert renamed["channel"]["name"] == "admin-renamed-channel"
        assert renamed["channel"]["is_active"] is False
        after_disable = await _state_snapshot(session_factory)
        disabled_row = next(row for row in after_disable[0] if row[0] == created_channel_id)
        assert disabled_row[1] == "admin-renamed-channel"
        assert disabled_row[5] is False

        reenabled = _assert_success(
            await client.post(
                "/api/v1/channels/update",
                headers=headers,
                params={"channel_id": created_channel_id},
                json={"is_active": True},
            )
        )
        assert reenabled["channel"]["name"] == "admin-renamed-channel"
        assert reenabled["channel"]["is_active"] is True
        after_enable = await _state_snapshot(session_factory)
        enabled_row = next(row for row in after_enable[0] if row[0] == created_channel_id)
        assert enabled_row[1] == "admin-renamed-channel"
        assert enabled_row[5] is True

        models_data = _assert_success(
            await client.post(
                "/api/v1/channels/models",
                headers=headers,
                json={
                    "api_key": "test-api-key",
                    "base_url": "https://llm.example/v1",
                    "timeout": 1,
                },
            )
        )
        assert models_data == {"models": [{"id": "chat-model", "owned_by": None, "created": None}]}

        chat_request = {
            "api_key": "test-api-key",
            "base_url": "https://llm.example/v1",
            "model_id": "chat-model",
            "protocol": "OPENAI",
            "prompt": "permission test",
            "timeout": 1,
        }
        for test_mode in ("non_stream", "stream"):
            chat_data = _assert_success(
                await client.post(
                    "/api/v1/channels/test-chat",
                    headers=headers,
                    json={**chat_request, "test_mode": test_mode},
                )
            )
            assert chat_data["model"] == "chat-model"
            assert chat_data["reply"] == "permission test reply"
            assert chat_data["test_mode"] == test_mode

        image_data = _assert_success(
            await client.post(
                "/api/v1/channels/test-image-generation",
                headers=headers,
                json={
                    "api_key": "test-api-key",
                    "base_url": "https://llm.example/v1",
                    "model_id": "image-model",
                    "protocol": "OPENAI_IMAGE",
                    "timeout": 1,
                },
            )
        )
        assert image_data["image"] == {"url": "https://image.example/test.png"}

        dimension_data = _assert_success(
            await client.post(
                "/api/v1/channels/test-embedding-dimension",
                headers=headers,
                params={"channel_id": seed_channel_id, "model_id": "embedding-model"},
            )
        )
        assert dimension_data["dimension"] == 3

        _assert_success(
            await client.post(
                "/api/v1/channels/delete",
                headers=headers,
                params={"channel_id": created_channel_id},
            )
        )

    async with session_factory() as session:
        assert await session.get(ModelChannel, created_channel_id) is None
    assert await _state_snapshot(session_factory) == seed_state


@pytest.mark.asyncio
async def test_admin_role_revocation_takes_effect_for_existing_token(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], int, dict[str, int]],
) -> None:
    app, session_factory, channel_id, calls = api_context
    token = _token("admin", is_superuser=True)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        initial_response = await client.get(
            "/api/v1/channels/get",
            headers={"Authorization": f"Bearer {token}"},
            params={"channel_id": channel_id},
        )
        initial_data = _assert_success(initial_response)
        assert initial_data["id"] == channel_id

        async with session_factory() as session:
            admin = (await session.execute(select(User).where(User.uid == "admin"))).scalar_one()
            admin.is_superuser = False
            await session.commit()

        for route_name in CHANNEL_MANAGEMENT_ROUTES:
            before = await _state_snapshot(session_factory)
            response = await _request(
                client,
                _management_request(route_name, channel_id),
                token,
            )
            _assert_standard(response, 403, t(ERR_ONLY_ADMIN_ALLOWED))
            assert await _state_snapshot(session_factory) == before

        assert calls == {name: 0 for name in calls}

        async with session_factory() as session:
            admin = (await session.execute(select(User).where(User.uid == "admin"))).scalar_one()
            admin.is_active = False
            await session.commit()

        before_disabled = await _state_snapshot(session_factory)
        disabled_response = await client.get(
            "/api/v1/channels/list",
            headers={"Authorization": f"Bearer {token}"},
            params={"page": 1, "size": 10},
        )
        _assert_standard(disabled_response, 401, t(ERR_UNAUTHORIZED))
        assert await _state_snapshot(session_factory) == before_disabled

    assert calls == {name: 0 for name in calls}
