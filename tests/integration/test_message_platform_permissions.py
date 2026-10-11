import asyncio
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
from app.api.v1 import message_platforms
from app.core.constants import ERR_MESSAGE_PLATFORM_NAME_EXISTS, ERR_MESSAGE_PLATFORM_NOT_FOUND, ERR_ONLY_ADMIN_ALLOWED, ERR_PROFILE_NOT_FOUND, ERR_UNAUTHORIZED
from app.core.i18n import t
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.message_platform import (
    MessagePlatform,
    MessagePlatformLanguage,
    MessagePlatformStatus,
    MessagePlatformType,
)
from app.models.profile import Profile, ProfileConfig
from app.models.user import User
from app.providers.database import get_db

TEST_JWT_SECRET = "message-platform-permissions-integration-jwt-secret"
TEST_PASSWORD_HASH = "fixed-message-platform-permissions-password-hash"
MESSAGE_PLATFORM_OPERATIONS = (
    "get",
    "update",
    "delete",
    "recover",
    "login-start",
    "login-status",
)
MESSAGE_PLATFORM_AUTH_ROUTES = ("types", "list", "create", *MESSAGE_PLATFORM_OPERATIONS)
DENIED_TARGETS = ("other", "unbound", "missing")
SENSITIVE_VALUES = (
    "ordinary-platform-token",
    "other-platform-token",
    "unbound-platform-token",
    "ordinary-platform-qrcode",
    "other-platform-qrcode",
    "unbound-platform-qrcode",
    "ordinary-platform-qrcode-image",
    "other-platform-qrcode-image",
    "unbound-platform-qrcode-image",
    "ordinary-platform-account",
    "other-platform-account",
    "unbound-platform-account",
    "ordinary-platform-error",
    "other-platform-error",
    "unbound-platform-error",
)


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
                            "is_enabled": True,
                        }
                    ]
                }
            }
        }
    ).model_dump()


def _platform_config(token: str) -> dict[str, Any]:
    return {
        "token": token,
        "base_url": "https://weixin.example",
        "cdn_base_url": "https://cdn.weixin.example",
        "api_timeout_ms": 1_000,
        "long_poll_timeout_ms": 1_000,
        "poll_interval_ms": 0,
        "max_inbound_media_size_mb": 8,
        "merge_single_poll_messages": False,
    }


def _platform(
    *,
    name: str,
    uid: str | None,
    profile_id: int | None,
    token: str,
    qrcode: str,
    account_id: str,
    last_error: str,
) -> MessagePlatform:
    return MessagePlatform(
        name=name,
        platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
        language=MessagePlatformLanguage.ZH,
        is_enabled=True,
        status=MessagePlatformStatus.WAITING_LOGIN,
        account_id=account_id,
        uid=uid,
        profile_id=profile_id,
        config=_platform_config(token),
        state={
            "qrcode": qrcode,
            "qrcode_img_content": f"{qrcode}-image",
            "qrcode_status": "wait",
        },
        last_error=last_error,
    )


@pytest_asyncio.fixture
async def api_context(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]]]:
    calls: list[str] = []

    async with database_factory(foreign_keys=True) as session_factory:
        monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x00" * 32)

        async def start_login_session(_adapter) -> dict[str, Any]:
            calls.append("start_login_session")
            return {
                "qrcode": "mock-login-qrcode",
                "qrcode_img_content": "mock-login-qrcode-image",
                "qrcode_status": "wait",
                "started_at": 1.0,
            }

        async def poll_qrcode_status(_adapter, qrcode: str) -> dict[str, Any]:
            calls.append(f"poll_qrcode_status:{qrcode}")
            return {
                "qrcode_status": "confirmed",
                "token": "mock-login-token",
                "account_id": "mock-login-account",
                "base_url": "https://weixin.example",
            }

        async def close(_adapter) -> None:
            calls.append("close")

        monkeypatch.setattr(message_platforms.WeixinOpenClawAdapter, "start_login_session", start_login_session)
        monkeypatch.setattr(message_platforms.WeixinOpenClawAdapter, "poll_qrcode_status", poll_qrcode_status)
        monkeypatch.setattr(message_platforms.WeixinOpenClawAdapter, "close", close)

        async with session_factory() as session:
            channel = ModelChannel(
                name="message-platform-permissions-channel",
                api_key="message-platform-permissions-channel-key",
                base_url="https://llm.example/v1",
                is_active=True,
                model_ids=[
                    {
                        "model_id": "chat-model",
                        "usage": "CHAT",
                        "protocol": "OPENAI",
                        "context_window_k": 32,
                        "max_tokens": 2_048,
                        "is_enabled": True,
                    }
                ],
            )
            session.add(channel)
            await session.flush()
            assert channel.id is not None

            ordinary_profile = Profile(
                uid="ordinary",
                name="ordinary-message-platform-profile",
                configs=_profile_configs(channel.id),
                is_default=True,
            )
            other_profile = Profile(
                uid="other",
                name="other-message-platform-profile",
                configs=_profile_configs(channel.id),
                is_default=True,
            )
            session.add_all([ordinary_profile, other_profile])
            await session.flush()
            assert ordinary_profile.id is not None and other_profile.id is not None

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
                        uid="other",
                        username="other",
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
                    _platform(
                        name="ordinary-message-platform",
                        uid="ordinary",
                        profile_id=ordinary_profile.id,
                        token="ordinary-platform-token",
                        qrcode="ordinary-platform-qrcode",
                        account_id="ordinary-platform-account",
                        last_error="ordinary-platform-error",
                    ),
                    _platform(
                        name="other-message-platform",
                        uid="other",
                        profile_id=other_profile.id,
                        token="other-platform-token",
                        qrcode="other-platform-qrcode",
                        account_id="other-platform-account",
                        last_error="other-platform-error",
                    ),
                    _platform(
                        name="unbound-message-platform",
                        uid=None,
                        profile_id=None,
                        token="unbound-platform-token",
                        qrcode="unbound-platform-qrcode",
                        account_id="unbound-platform-account",
                        last_error="unbound-platform-error",
                    ),
                ]
            )
            await session.commit()

            platforms = (await session.execute(select(MessagePlatform).order_by(MessagePlatform.name))).scalars().all()
            platform_ids = {platform.name.removesuffix("-message-platform"): int(platform.id) for platform in platforms if platform.id is not None}

        monkeypatch.setattr(security, "AsyncSessionLocal", session_factory)
        monkeypatch.setattr(security, "get_jwt_secret_key", lambda: TEST_JWT_SECRET)

        app = FastAPI()
        register_handlers(app)
        app.include_router(message_platforms.router, prefix="/api/v1")

        async def override_get_db() -> AsyncGenerator[AsyncSession]:
            async with session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = override_get_db
        yield app, session_factory, {**platform_ids, "missing": 999_999}, calls


def _token(username: str, **claims: Any) -> str:
    return security.create_access_token(data={"sub": username, **claims})


def _unauthorized_token(credential: str) -> str | None:
    if credential == "missing":
        return None
    if credential == "invalid":
        return "badJWT"
    if credential == "expired":
        return security.create_access_token(data={"sub": "ordinary"}, expires_delta=timedelta(seconds=-1))
    if credential == "disabled":
        return _token("disabled")
    raise AssertionError(f"unknown credential: {credential}")


def _request_for(operation: str, platform_id: int) -> dict[str, Any]:
    base_path = "/api/v1/message-platforms"
    if operation == "types":
        return {"method": "GET", "path": f"{base_path}/types"}
    if operation == "list":
        return {
            "method": "GET",
            "path": f"{base_path}/list",
            "params": {"page": 1, "size": 10},
        }
    if operation == "create":
        return {
            "method": "POST",
            "path": f"{base_path}/create",
            "json": {
                "name": "authentication-denied-message-platform",
                "is_enabled": False,
                "config": {},
            },
        }
    if operation == "get":
        return {
            "method": "GET",
            "path": f"{base_path}/get",
            "params": {"platform_id": platform_id},
        }
    if operation == "update":
        return {
            "method": "POST",
            "path": f"{base_path}/update",
            "params": {"platform_id": platform_id},
            "json": {
                "name": "denied-message-platform-update",
                "is_enabled": False,
                "config": {"api_timeout_ms": 1_000},
            },
        }
    if operation == "delete":
        return {
            "method": "POST",
            "path": f"{base_path}/delete",
            "params": {"platform_id": platform_id},
        }
    if operation == "recover":
        return {
            "method": "POST",
            "path": f"{base_path}/recover",
            "params": {"platform_id": platform_id},
        }
    if operation == "login-start":
        return {
            "method": "POST",
            "path": f"{base_path}/{platform_id}/weixin-openclaw/login/start",
        }
    if operation == "login-status":
        return {
            "method": "GET",
            "path": f"{base_path}/{platform_id}/weixin-openclaw/login/status",
        }
    raise AssertionError(f"unknown operation: {operation}")


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
        params=request.get("params"),
        json=request.get("json"),
    )


async def _platform_snapshot(session_factory: async_sessionmaker[AsyncSession]) -> tuple[tuple[Any, ...], ...]:
    async with session_factory() as session:
        platforms = (await session.execute(select(MessagePlatform).order_by(MessagePlatform.id))).scalars().all()
        return tuple(
            (
                platform.id,
                platform.name,
                platform.platform_type,
                platform.language,
                platform.is_enabled,
                platform.use_stream_dispatch,
                platform.status,
                platform.account_id,
                platform.uid,
                platform.profile_id,
                deepcopy(platform.config or {}),
                deepcopy(platform.state or {}),
                platform.last_error,
                platform.created_at,
                platform.updated_at,
            )
            for platform in platforms
        )


def _assert_not_found(response: httpx.Response) -> None:
    payload = response.json()
    assert response.status_code == 404
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 404
    assert payload["message"] == t(ERR_MESSAGE_PLATFORM_NOT_FOUND)
    assert payload["data"] is None
    response_text = response.text.lower()
    for sensitive_value in SENSITIVE_VALUES:
        assert sensitive_value not in response_text
    for sensitive_field in ("token", "qrcode", "account_id", "last_error"):
        assert sensitive_field not in response_text


def _assert_unauthorized(response: httpx.Response) -> None:
    payload = response.json()
    assert response.status_code == 401
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 401
    assert payload["message"] == t(ERR_UNAUTHORIZED)
    assert payload["data"] is None
    response_text = response.text.lower()
    for sensitive_value in SENSITIVE_VALUES:
        assert sensitive_value not in response_text
    for sensitive_field in ("token", "qrcode", "account_id", "last_error"):
        assert sensitive_field not in response_text


def _assert_list_page(response: httpx.Response, *, page: int, size: int, total: int) -> dict[str, Any]:
    payload = response.json()
    assert response.status_code == 200
    assert payload["code"] == 200
    data = payload["data"]
    assert data["page"] == page
    assert data["size"] == size
    assert data["total"] == total
    return data


def _assert_forbidden(response: httpx.Response) -> None:
    payload = response.json()
    assert response.status_code == 403
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 403
    assert payload["message"] == t(ERR_ONLY_ADMIN_ALLOWED)
    assert payload["data"] is None


def _create_uid_payload(uid_case: str) -> dict[str, Any]:
    if uid_case == "missing":
        return {}
    return {
        "null": {"uid": None},
        "empty": {"uid": ""},
        "whitespace": {"uid": "   "},
        "ordinary": {"uid": "ordinary"},
        "ordinary-spaced": {"uid": " ordinary "},
    }[uid_case]


async def _get_owned_profile_id(session_factory: async_sessionmaker[AsyncSession], uid: str) -> int:
    async with session_factory() as session:
        profile = (await session.execute(select(Profile).where(Profile.uid == uid).order_by(Profile.id))).scalars().first()
        assert profile is not None and profile.id is not None
        return int(profile.id)


def _assert_platforms_unchanged_except(
    before: tuple[tuple[Any, ...], ...],
    after: tuple[tuple[Any, ...], ...],
    excluded_id: int,
) -> None:
    before_by_id = {row[0]: row for row in before}
    after_by_id = {row[0]: row for row in after}
    assert {platform_id: row for platform_id, row in before_by_id.items() if platform_id != excluded_id} == {platform_id: row for platform_id, row in after_by_id.items() if platform_id != excluded_id}


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", MESSAGE_PLATFORM_OPERATIONS)
@pytest.mark.parametrize("target", DENIED_TARGETS)
async def test_ordinary_user_cannot_access_other_unbound_or_missing_message_platforms(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    operation: str,
    target: str,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    request = _request_for(operation, platform_ids[target])
    token = _token("ordinary", uid="other", is_superuser=True)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = await _platform_snapshot(session_factory)
        response = await _request(client, request, token)
        _assert_not_found(response)
        assert await _platform_snapshot(session_factory) == before

    assert calls == []


@pytest.mark.asyncio
async def test_message_platform_list_scopes_paginates_and_allows_superuser_full_access(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
) -> None:
    app, session_factory, platform_ids, calls = api_context

    async with session_factory() as session:
        extra_ordinary = _platform(
            name="ordinary-extra-message-platform",
            uid="ordinary",
            profile_id=None,
            token="ordinary-extra-platform-token",
            qrcode="ordinary-extra-platform-qrcode",
            account_id="ordinary-extra-platform-account",
            last_error="ordinary-extra-platform-error",
        )
        extra_other = _platform(
            name="other-extra-message-platform",
            uid="other",
            profile_id=None,
            token="other-extra-platform-token",
            qrcode="other-extra-platform-qrcode",
            account_id="other-extra-platform-account",
            last_error="other-extra-platform-error",
        )
        session.add_all([extra_ordinary, extra_other])
        await session.commit()
        assert extra_ordinary.id is not None and extra_other.id is not None

    before = await _platform_snapshot(session_factory)
    ordinary_token = _token("ordinary", uid="other", is_superuser=True)
    admin_token = _token("admin")
    ordinary_ids = {platform_ids["ordinary"], int(extra_ordinary.id)}
    expected_all_ids = {
        platform_ids["ordinary"],
        platform_ids["other"],
        platform_ids["unbound"],
        int(extra_ordinary.id),
        int(extra_other.id),
    }
    forbidden_values = tuple(value for value in SENSITIVE_VALUES if value.startswith(("other-", "unbound-"))) + (
        "other-extra-platform-token",
        "other-extra-platform-qrcode",
        "other-extra-platform-qrcode-image",
        "other-extra-platform-account",
        "other-extra-platform-error",
    )

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        page_one_response = await client.get(
            "/api/v1/message-platforms/list",
            headers={"Authorization": f"Bearer {ordinary_token}"},
            params={"page": 1, "size": 1, "uid": "other"},
        )
        page_one = _assert_list_page(page_one_response, page=1, size=1, total=2)
        assert [item["id"] for item in page_one["items"]] == [max(ordinary_ids)]
        assert all(item["uid"] == "ordinary" for item in page_one["items"])

        page_two_response = await client.get(
            "/api/v1/message-platforms/list",
            headers={"Authorization": f"Bearer {ordinary_token}"},
            params={"page": 2, "size": 1, "uid": "other"},
        )
        page_two = _assert_list_page(page_two_response, page=2, size=1, total=2)
        assert [item["id"] for item in page_two["items"]] == [min(ordinary_ids)]
        assert all(item["uid"] == "ordinary" for item in page_two["items"])

        page_three_response = await client.get(
            "/api/v1/message-platforms/list",
            headers={"Authorization": f"Bearer {ordinary_token}"},
            params={"page": 3, "size": 1, "uid": "other"},
        )
        page_three = _assert_list_page(page_three_response, page=3, size=1, total=2)
        assert page_three["items"] == []

        for response in (page_one_response, page_two_response, page_three_response):
            for sensitive_value in forbidden_values:
                assert sensitive_value not in response.text

        admin_response = await client.get(
            "/api/v1/message-platforms/list",
            headers={"Authorization": f"Bearer {admin_token}"},
            params={"page": 1, "size": 100},
        )
        admin_page = _assert_list_page(admin_response, page=1, size=100, total=5)
        assert len(admin_page["items"]) == 5
        assert {item["id"] for item in admin_page["items"]} == expected_all_ids
        assert {item["uid"] for item in admin_page["items"]} == {"ordinary", "other", None}
        assert any(item["uid"] is None for item in admin_page["items"])

    assert await _platform_snapshot(session_factory) == before
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("uid_case", ("missing", "null", "empty", "whitespace", "ordinary", "ordinary-spaced"))
@pytest.mark.parametrize("is_enabled", (True, False))
async def test_ordinary_user_create_normalizes_owned_uid_and_profile_assignment(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    uid_case: str,
    is_enabled: bool,
) -> None:
    app, session_factory, _platform_ids, calls = api_context
    profile_id = await _get_owned_profile_id(session_factory, "ordinary")
    name = f"ordinary-create-{uid_case}-{str(is_enabled).lower()}"
    request_payload = {
        **_create_uid_payload(uid_case),
        "name": name,
        "is_enabled": is_enabled,
        "profile_id": profile_id,
        "config": {"token": f"{name}-token"},
    }
    token = _token("ordinary", uid="other", is_superuser=True)

    before = await _platform_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/create",
            headers={"Authorization": f"Bearer {token}"},
            json=request_payload,
        )

    response_payload = response.json()
    assert response.status_code == 200
    assert response_payload["code"] == 200
    created = response_payload["data"]
    assert created["uid"] == "ordinary"
    assert created["profile_id"] == profile_id
    assert created["name"] == name
    assert created["is_enabled"] is is_enabled
    assert created["config"]["token"] == f"{name}-token"
    created_id = created["id"]

    async with session_factory() as session:
        persisted = await session.get(MessagePlatform, created_id)
        assert persisted is not None
        assert persisted.uid == "ordinary"
        assert persisted.profile_id == profile_id
        assert persisted.name == name
        assert persisted.is_enabled is is_enabled
        assert persisted.get_config_secret("token") == f"{name}-token"

    after = await _platform_snapshot(session_factory)
    before_by_id = {row[0]: row for row in before}
    after_by_id = {row[0]: row for row in after if row[0] in before_by_id}
    assert len(after) == len(before) + 1
    assert after_by_id == before_by_id
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("submitted_uid", ("other", " other ", "admin", "unknown"))
@pytest.mark.parametrize("is_enabled", (True, False))
async def test_ordinary_user_cannot_create_message_platform_for_another_uid(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    submitted_uid: str,
    is_enabled: bool,
) -> None:
    app, session_factory, _platform_ids, calls = api_context
    profile_id = await _get_owned_profile_id(session_factory, "ordinary")
    name = f"denied-create-{submitted_uid.strip()}-{str(is_enabled).lower()}"
    token = _token("ordinary", uid="other", is_superuser=True)
    before = await _platform_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/create",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": name,
                "uid": submitted_uid,
                "is_enabled": is_enabled,
                "profile_id": profile_id,
                "config": {"token": f"{name}-token"},
            },
        )

    _assert_forbidden(response)
    assert await _platform_snapshot(session_factory) == before
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_target", ("other", "missing"))
async def test_ordinary_user_cannot_create_message_platform_with_unowned_or_missing_profile(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    profile_target: str,
) -> None:
    app, session_factory, _platform_ids, calls = api_context
    profile_id = await _get_owned_profile_id(session_factory, "other") if profile_target == "other" else 999_999
    name = f"denied-profile-create-{profile_target}"
    token = _token("ordinary", uid="other", is_superuser=True)
    before = await _platform_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/create",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": name,
                "is_enabled": True,
                "profile_id": profile_id,
                "config": {"token": f"{name}-token"},
            },
        )

    payload = response.json()
    assert response.status_code == 404
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 404
    assert payload["message"] == t(ERR_PROFILE_NOT_FOUND)
    assert payload["data"] is None
    assert await _platform_snapshot(session_factory) == before
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "submitted_uid",
    (None, "", "   ", "other", " other ", "admin", "unknown"),
    ids=("null", "empty", "whitespace", "other", "other-spaced", "admin", "unknown"),
)
@pytest.mark.parametrize("is_enabled", (True, False))
async def test_ordinary_user_cannot_transfer_or_unbind_message_platform_with_other_updates(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    submitted_uid: str | None,
    is_enabled: bool,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    token = _token("ordinary", uid="other", is_superuser=True)
    name_suffix = str(submitted_uid).strip() or "empty"
    name = f"denied-update-{name_suffix}-{str(is_enabled).lower()}"
    before = await _platform_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_ids["ordinary"]},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": name,
                "uid": submitted_uid,
                "is_enabled": is_enabled,
                "config": {"api_timeout_ms": 1_000},
                "profile_id": None,
            },
        )

    _assert_forbidden(response)
    assert await _platform_snapshot(session_factory) == before
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("uid_case", ("missing", "ordinary", "ordinary-spaced"))
@pytest.mark.parametrize("is_enabled", (True, False))
async def test_ordinary_user_can_update_owned_message_platform_and_clear_profile(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    uid_case: str,
    is_enabled: bool,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    platform_id = platform_ids["ordinary"]
    profile_id = await _get_owned_profile_id(session_factory, "ordinary")
    name = f"ordinary-update-{uid_case}-{str(is_enabled).lower()}"
    token_value = f"{name}-token"
    token = _token("ordinary", uid="other", is_superuser=True)
    before = await _platform_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {token}"},
            json={
                **_create_uid_payload(uid_case),
                "name": name,
                "language": "en",
                "use_stream_dispatch": True,
                "is_enabled": is_enabled,
                "config": {"token": token_value},
            },
        )

        response_payload = response.json()
        assert response.status_code == 200
        assert response_payload["code"] == 200
        updated = response_payload["data"]
        assert updated["uid"] == "ordinary"
        assert updated["profile_id"] == profile_id
        assert updated["name"] == name
        assert updated["language"] == "en"
        assert updated["use_stream_dispatch"] is True
        assert updated["is_enabled"] is is_enabled
        assert updated["config"]["token"] == token_value

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, platform_id)
            assert persisted is not None
            assert persisted.uid == "ordinary"
            assert persisted.profile_id == profile_id
            assert persisted.name == name
            assert persisted.language.value == "en"
            assert persisted.use_stream_dispatch is True
            assert persisted.is_enabled is is_enabled
            assert persisted.get_config_secret("token") == token_value

        after_update = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_update, platform_id)

        clear_response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {token}"},
            json={"profile_id": None},
        )
        clear_payload = clear_response.json()
        assert clear_response.status_code == 200
        assert clear_payload["code"] == 200
        cleared = clear_payload["data"]
        assert cleared["uid"] == "ordinary"
        assert cleared["profile_id"] is None

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, platform_id)
            assert persisted is not None
            assert persisted.uid == "ordinary"
            assert persisted.profile_id is None

    after_clear = await _platform_snapshot(session_factory)
    _assert_platforms_unchanged_except(before, after_clear, platform_id)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("profile_target", ("other", "missing"))
async def test_ordinary_user_cannot_update_message_platform_with_unowned_or_missing_profile(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    profile_target: str,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    profile_id = await _get_owned_profile_id(session_factory, "other") if profile_target == "other" else 999_999
    token = _token("ordinary", uid="other", is_superuser=True)
    before = await _platform_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_ids["ordinary"]},
            headers={"Authorization": f"Bearer {token}"},
            json={"profile_id": profile_id},
        )

    payload = response.json()
    assert response.status_code == 404
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 404
    assert payload["message"] == t(ERR_PROFILE_NOT_FOUND)
    assert payload["data"] is None
    assert await _platform_snapshot(session_factory) == before
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("route_name", MESSAGE_PLATFORM_AUTH_ROUTES)
@pytest.mark.parametrize(
    "credential",
    ("missing", "invalid", "expired", "disabled"),
    ids=("missing", "invalid", "expired", "disabled"),
)
async def test_message_platform_routes_reject_unauthorized_credentials(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    route_name: str,
    credential: str,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    request = _request_for(route_name, platform_ids["ordinary"])
    token = _unauthorized_token(credential)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        before = await _platform_snapshot(session_factory)
        response = await _request(client, request, token)
        _assert_unauthorized(response)
        assert await _platform_snapshot(session_factory) == before

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("username", "target"),
    (("ordinary", "ordinary"), ("admin", "other")),
    ids=("ordinary-owner", "admin-other-owner"),
)
async def test_authorized_user_can_complete_message_platform_lifecycle(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    username: str,
    target: str,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    platform_id = platform_ids[target]
    profile_id = await _get_owned_profile_id(session_factory, target)
    original_token = f"{target}-platform-token"
    token = _token(username)
    updated_name = f"{target}-workflow-updated"
    before = await _platform_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        types_response = await client.get(
            "/api/v1/message-platforms/types",
            headers={"Authorization": f"Bearer {token}"},
        )
        types_payload = types_response.json()
        assert types_response.status_code == 200
        assert types_payload["code"] == 200
        assert "WEIXIN_OPENCLAW" in types_payload["data"]["platform_types"]
        assert "zh" in types_payload["data"]["languages"]
        assert "CONNECTED" in types_payload["data"]["statuses"]

        get_response = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        get_payload = get_response.json()
        assert get_response.status_code == 200
        assert get_payload["code"] == 200
        current = get_payload["data"]
        assert current["id"] == platform_id
        assert current["uid"] == target
        assert current["profile_id"] == profile_id
        assert current["config"]["token"] == original_token
        assert current["status"] == "WAITING_LOGIN"

        update_response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {token}"},
            json={
                "name": updated_name,
                "language": "en",
                "use_stream_dispatch": True,
                "is_enabled": False,
            },
        )
        update_payload = update_response.json()
        assert update_response.status_code == 200
        assert update_payload["code"] == 200
        updated = update_payload["data"]
        assert updated["uid"] == target
        assert updated["profile_id"] == profile_id
        assert updated["name"] == updated_name
        assert updated["language"] == "en"
        assert updated["use_stream_dispatch"] is True
        assert updated["is_enabled"] is False

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, platform_id)
            assert persisted is not None
            assert persisted.uid == target
            assert persisted.profile_id == profile_id
            assert persisted.name == updated_name
            assert persisted.language.value == "en"
            assert persisted.use_stream_dispatch is True
            assert persisted.is_enabled is False
            assert persisted.get_config_secret("token") == original_token
            assert persisted.last_error == f"{target}-platform-error"

        after_update = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_update, platform_id)
        assert calls == []

        recover_response = await client.post(
            "/api/v1/message-platforms/recover",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        recover_payload = recover_response.json()
        assert recover_response.status_code == 200
        assert recover_payload["code"] == 200
        recovered = recover_payload["data"]
        assert recovered["uid"] == target
        assert recovered["profile_id"] == profile_id
        assert recovered["is_enabled"] is False
        assert recovered["status"] == "DISCONNECTED"
        assert recovered["last_error"] == ""

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, platform_id)
            assert persisted is not None
            assert persisted.uid == target
            assert persisted.profile_id == profile_id
            assert persisted.status.value == "DISCONNECTED"
            assert persisted.last_error == ""
            assert persisted.get_config_secret("token") == original_token

        after_recover = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_recover, platform_id)
        assert calls == []

        enable_response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {token}"},
            json={"is_enabled": True},
        )
        enable_payload = enable_response.json()
        assert enable_response.status_code == 200
        assert enable_payload["code"] == 200
        enabled = enable_payload["data"]
        assert enabled["uid"] == target
        assert enabled["profile_id"] == profile_id
        assert enabled["is_enabled"] is True

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, platform_id)
            assert persisted is not None
            assert persisted.uid == target
            assert persisted.profile_id == profile_id
            assert persisted.is_enabled is True

        after_enable = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_enable, platform_id)
        assert calls == []

        login_start_response = await client.post(
            f"/api/v1/message-platforms/{platform_id}/weixin-openclaw/login/start",
            headers={"Authorization": f"Bearer {token}"},
        )
        login_start_payload = login_start_response.json()
        assert login_start_response.status_code == 200
        assert login_start_payload["code"] == 200
        login_start = login_start_payload["data"]
        assert login_start["platform_id"] == platform_id
        assert login_start["qrcode"] == "mock-login-qrcode"
        assert login_start["qrcode_img_content"] == "mock-login-qrcode-image"
        assert login_start["status"] == "WAITING_LOGIN"
        assert calls == ["start_login_session", "close"]

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, platform_id)
            assert persisted is not None
            assert persisted.uid == target
            assert persisted.profile_id == profile_id
            assert persisted.status.value == "WAITING_LOGIN"
            assert (persisted.state or {}).get("qrcode") == "mock-login-qrcode"
            assert persisted.get_config_secret("token") == original_token

        after_login_start = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_login_start, platform_id)

        login_status_response = await client.get(
            f"/api/v1/message-platforms/{platform_id}/weixin-openclaw/login/status",
            headers={"Authorization": f"Bearer {token}"},
        )
        login_status_payload = login_status_response.json()
        assert login_status_response.status_code == 200
        assert login_status_payload["code"] == 200
        login_status = login_status_payload["data"]
        assert login_status["platform_id"] == platform_id
        assert login_status["status"] == "CONNECTED"
        assert login_status["qrcode_status"] == "confirmed"
        assert login_status["account_id"] == "mock-login-account"
        assert calls == ["start_login_session", "close", "poll_qrcode_status:mock-login-qrcode", "close"]

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, platform_id)
            assert persisted is not None
            assert persisted.uid == target
            assert persisted.profile_id == profile_id
            assert persisted.status.value == "CONNECTED"
            assert persisted.account_id == "mock-login-account"
            assert persisted.get_config_secret("token") == "mock-login-token"
            assert (persisted.state or {}).get("qrcode") == ""
            assert (persisted.state or {}).get("qrcode_img_content") == ""

        after_login_status = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_login_status, platform_id)

        delete_response = await client.post(
            "/api/v1/message-platforms/delete",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {token}"},
        )
        delete_payload = delete_response.json()
        assert delete_response.status_code == 200
        assert delete_payload["code"] == 200
        assert delete_payload["data"] is None

    async with session_factory() as session:
        assert await session.get(MessagePlatform, platform_id) is None
    after_delete = await _platform_snapshot(session_factory)
    _assert_platforms_unchanged_except(before, after_delete, platform_id)
    assert len(after_delete) == len(before) - 1
    assert calls == ["start_login_session", "close", "poll_qrcode_status:mock-login-qrcode", "close"]


@pytest.mark.asyncio
async def test_superuser_can_transfer_unbind_and_delete_message_platform(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
) -> None:
    app, session_factory, platform_ids, calls = api_context
    admin_token = _token("admin")
    ordinary_token = _token("ordinary")
    other_token = _token("other")
    ordinary_profile_id = await _get_owned_profile_id(session_factory, "ordinary")
    other_profile_id = await _get_owned_profile_id(session_factory, "other")
    created_token = "admin-transfer-platform-token"
    before = await _platform_snapshot(session_factory)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await client.post(
            "/api/v1/message-platforms/create",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={
                "name": "admin-transfer-message-platform",
                "uid": "other",
                "profile_id": other_profile_id,
                "is_enabled": True,
                "config": {"token": created_token},
            },
        )
        create_payload = create_response.json()
        assert create_response.status_code == 200
        assert create_payload["code"] == 200
        created = create_payload["data"]
        created_id = created["id"]
        assert created["uid"] == "other"
        assert created["profile_id"] == other_profile_id
        assert created["config"]["token"] == created_token

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, created_id)
            assert persisted is not None
            assert persisted.uid == "other"
            assert persisted.profile_id == other_profile_id
            assert persisted.get_config_secret("token") == created_token

        after_create = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_create, created_id)
        assert calls == []

        ordinary_denied = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        _assert_not_found(ordinary_denied)
        assert created_token not in ordinary_denied.text
        assert await _platform_snapshot(session_factory) == after_create

        other_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {other_token}"},
        )
        other_get_payload = other_get.json()
        assert other_get.status_code == 200
        assert other_get_payload["code"] == 200
        other_data = other_get_payload["data"]
        assert other_data["id"] == created_id
        assert other_data["uid"] == "other"
        assert other_data["profile_id"] == other_profile_id
        assert other_data["config"]["token"] == created_token
        assert await _platform_snapshot(session_factory) == after_create

        transfer_response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"uid": "ordinary", "profile_id": ordinary_profile_id},
        )
        transfer_payload = transfer_response.json()
        assert transfer_response.status_code == 200
        assert transfer_payload["code"] == 200
        transferred = transfer_payload["data"]
        assert transferred["uid"] == "ordinary"
        assert transferred["profile_id"] == ordinary_profile_id

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, created_id)
            assert persisted is not None
            assert persisted.uid == "ordinary"
            assert persisted.profile_id == ordinary_profile_id
            assert persisted.get_config_secret("token") == created_token

        after_transfer = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_transfer, created_id)

        for operation in MESSAGE_PLATFORM_OPERATIONS:
            denied = await _request(client, _request_for(operation, created_id), other_token)
            _assert_not_found(denied)
            assert created_token not in denied.text
            assert await _platform_snapshot(session_factory) == after_transfer
        assert calls == []

        ordinary_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        ordinary_get_payload = ordinary_get.json()
        assert ordinary_get.status_code == 200
        assert ordinary_get_payload["code"] == 200
        ordinary_data = ordinary_get_payload["data"]
        assert ordinary_data["uid"] == "ordinary"
        assert ordinary_data["profile_id"] == ordinary_profile_id
        assert ordinary_data["config"]["token"] == created_token
        assert await _platform_snapshot(session_factory) == after_transfer

        unbind_response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"uid": None, "profile_id": None, "is_enabled": False},
        )
        unbind_payload = unbind_response.json()
        assert unbind_response.status_code == 200
        assert unbind_payload["code"] == 200
        unbound = unbind_payload["data"]
        assert unbound["uid"] is None
        assert unbound["profile_id"] is None
        assert unbound["is_enabled"] is False

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, created_id)
            assert persisted is not None
            assert persisted.uid is None
            assert persisted.profile_id is None
            assert persisted.is_enabled is False

        after_unbind = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after_unbind, created_id)

        ordinary_unbound_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        _assert_not_found(ordinary_unbound_get)
        assert created_token not in ordinary_unbound_get.text

        ordinary_unbound_delete = await client.post(
            "/api/v1/message-platforms/delete",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        _assert_not_found(ordinary_unbound_delete)
        assert created_token not in ordinary_unbound_delete.text

        ordinary_list = await client.get(
            "/api/v1/message-platforms/list",
            params={"page": 1, "size": 100},
            headers={"Authorization": f"Bearer {ordinary_token}"},
        )
        ordinary_list_payload = ordinary_list.json()
        assert ordinary_list.status_code == 200
        assert ordinary_list_payload["code"] == 200
        ordinary_items = ordinary_list_payload["data"]["items"]
        assert ordinary_list_payload["data"]["total"] == 1
        assert created_id not in {item["id"] for item in ordinary_items}
        assert created_token not in ordinary_list.text

        admin_unbound_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        admin_unbound_payload = admin_unbound_get.json()
        assert admin_unbound_get.status_code == 200
        assert admin_unbound_payload["code"] == 200
        admin_unbound = admin_unbound_payload["data"]
        assert admin_unbound["uid"] is None
        assert admin_unbound["profile_id"] is None
        assert admin_unbound["config"]["token"] == created_token

        admin_delete = await client.post(
            "/api/v1/message-platforms/delete",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        admin_delete_payload = admin_delete.json()
        assert admin_delete.status_code == 200
        assert admin_delete_payload["code"] == 200
        assert admin_delete_payload["data"] is None

    async with session_factory() as session:
        assert await session.get(MessagePlatform, created_id) is None
    after_delete = await _platform_snapshot(session_factory)
    assert after_delete == before
    assert len(after_delete) == len(before)
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ("login-start", "login-status"))
async def test_old_owner_cannot_write_platform_after_transfer_during_login_external_wait(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    operation: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    platform_id = platform_ids["ordinary"]
    other_profile_id = await _get_owned_profile_id(session_factory, "other")
    admin_token = _token("admin")
    ordinary_token = _token("ordinary")
    transfer_count = 0
    transfer_snapshots: list[tuple[tuple[Any, ...], ...]] = []
    external_calls: list[str] = []
    new_token = "race-login-token"
    new_account_id = "race-login-account"
    new_qrcode = "race-login-qrcode"
    new_qrcode_image = "race-login-qrcode-image"
    before = await _platform_snapshot(session_factory)

    async def transfer_platform_owner() -> None:
        nonlocal transfer_count
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as admin_client:
            admin_response = await admin_client.post(
                "/api/v1/message-platforms/update",
                params={"platform_id": platform_id},
                headers={"Authorization": f"Bearer {admin_token}"},
                json={"uid": "other", "profile_id": other_profile_id},
            )
        admin_payload = admin_response.json()
        assert admin_response.status_code == 200
        assert admin_payload["code"] == 200
        assert admin_payload["data"]["uid"] == "other"
        assert admin_payload["data"]["profile_id"] == other_profile_id
        transfer_count += 1
        transfer_snapshots.append(await _platform_snapshot(session_factory))

    async def race_start_login_session(_adapter) -> dict[str, Any]:
        external_calls.append("start_login_session")
        await transfer_platform_owner()
        return {
            "qrcode": new_qrcode,
            "qrcode_img_content": new_qrcode_image,
            "qrcode_status": "wait",
            "started_at": 1.0,
        }

    async def race_poll_qrcode_status(_adapter, qrcode: str) -> dict[str, Any]:
        external_calls.append(f"poll_qrcode_status:{qrcode}")
        await transfer_platform_owner()
        return {
            "qrcode_status": "confirmed",
            "token": new_token,
            "account_id": new_account_id,
            "base_url": "https://weixin.example",
        }

    if operation == "login-start":
        monkeypatch.setattr(message_platforms.WeixinOpenClawAdapter, "start_login_session", race_start_login_session)
    else:
        monkeypatch.setattr(message_platforms.WeixinOpenClawAdapter, "poll_qrcode_status", race_poll_qrcode_status)

    request = _request_for(operation, platform_id)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await _request(client, request, ordinary_token)

    _assert_not_found(response)
    assert response.status_code == 404
    for sensitive_value in (new_token, new_account_id, new_qrcode, new_qrcode_image):
        assert sensitive_value not in response.text
    assert transfer_count == 1
    assert external_calls == (["start_login_session"] if operation == "login-start" else ["poll_qrcode_status:ordinary-platform-qrcode"])
    assert len(transfer_snapshots) == 1
    transfer_snapshot = transfer_snapshots[0]
    _assert_platforms_unchanged_except(before, transfer_snapshot, platform_id)
    transferred_row = next(row for row in transfer_snapshot if row[0] == platform_id)
    assert transferred_row[8] == "other"
    assert transferred_row[9] == other_profile_id
    assert await _platform_snapshot(session_factory) == transfer_snapshot
    assert calls == ["close"]


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ("other", "admin"))
async def test_distinct_users_can_create_same_message_platform_name(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    actor: str,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    name = "ordinary-message-platform"
    new_token = f"{actor}-same-name-platform-token"
    request_payload: dict[str, Any] = {
        "name": name,
        "is_enabled": True,
        "config": {"token": new_token},
    }
    if actor == "admin":
        request_payload["uid"] = " other "

    before = await _platform_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        create_response = await client.post(
            "/api/v1/message-platforms/create",
            headers={"Authorization": f"Bearer {_token(actor)}"},
            json=request_payload,
        )
        create_payload = create_response.json()
        assert create_response.status_code == 200
        assert create_payload["code"] == 200
        created = create_payload["data"]
        created_id = created["id"]
        assert created["uid"] == "other"
        assert created_id != platform_ids["ordinary"]
        assert created["config"]["token"] == new_token

        ordinary_get = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": created_id},
            headers={"Authorization": f"Bearer {_token('ordinary')}"},
        )
        _assert_not_found(ordinary_get)
        assert new_token not in ordinary_get.text

    async with session_factory() as session:
        same_name = (await session.execute(select(MessagePlatform).where(MessagePlatform.name == name).order_by(MessagePlatform.id))).scalars().all()
        assert len(same_name) == 2
        by_uid = {platform.uid: platform for platform in same_name}
        assert set(by_uid) == {"ordinary", "other"}
        assert by_uid["ordinary"].id == platform_ids["ordinary"]
        assert by_uid["ordinary"].get_config_secret("token") == "ordinary-platform-token"
        assert by_uid["other"].id == created_id
        assert by_uid["other"].get_config_secret("token") == new_token

    after = await _platform_snapshot(session_factory)
    _assert_platforms_unchanged_except(before, after, created_id)
    assert len(after) == len(before) + 1
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("actor", ("ordinary", "admin"))
async def test_same_user_cannot_create_duplicate_message_platform_name(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    actor: str,
) -> None:
    app, session_factory, _platform_ids, calls = api_context
    new_token = f"{actor}-duplicate-platform-token"
    request_payload: dict[str, Any] = {
        "name": "ordinary-message-platform",
        "is_enabled": True,
        "config": {"token": new_token},
    }
    if actor == "admin":
        request_payload["uid"] = " ordinary "

    before = await _platform_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/create",
            headers={"Authorization": f"Bearer {_token(actor)}"},
            json=request_payload,
        )

    payload = response.json()
    assert response.status_code == 400
    assert payload["code"] == 400
    assert payload["message"] == t(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
    assert payload["data"] is None
    assert new_token not in response.text
    assert "ordinary-platform-token" not in response.text
    assert await _platform_snapshot(session_factory) == before
    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name_case", "expected_status"),
    (("cross-owner", 200), ("same-owner", 400), ("unchanged", 200)),
)
async def test_ordinary_user_update_name_checks_owner_scoped_uniqueness(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    name_case: str,
    expected_status: int,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    platform_id = platform_ids["ordinary"]
    target_name = {
        "cross-owner": "other-message-platform",
        "same-owner": "ordinary-second-message-platform",
        "unchanged": "ordinary-message-platform",
    }[name_case]

    async with session_factory() as session:
        second_ordinary = _platform(
            name="ordinary-second-message-platform",
            uid="ordinary",
            profile_id=None,
            token="ordinary-second-platform-token",
            qrcode="ordinary-second-platform-qrcode",
            account_id="ordinary-second-platform-account",
            last_error="ordinary-second-platform-error",
        )
        session.add(second_ordinary)
        await session.commit()
        assert second_ordinary.id is not None
        assert second_ordinary.platform_type == MessagePlatformType.WEIXIN_OPENCLAW

    before = await _platform_snapshot(session_factory)
    token = _token("ordinary")
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {token}"},
            json={"name": target_name, "language": "en"},
        )

    payload = response.json()
    assert response.status_code == expected_status
    if expected_status == 200:
        assert payload["code"] == 200
        updated = payload["data"]
        assert updated["name"] == target_name
        assert updated["language"] == "en"
        assert updated["uid"] == "ordinary"

        async with session_factory() as session:
            persisted = await session.get(MessagePlatform, platform_id)
            assert persisted is not None
            assert persisted.name == target_name
            assert persisted.language.value == "en"
            assert persisted.uid == "ordinary"

        after = await _platform_snapshot(session_factory)
        _assert_platforms_unchanged_except(before, after, platform_id)
    else:
        assert set(payload) == {"code", "message", "data"}
        assert payload["code"] == 400
        assert payload["message"] == t(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
        assert payload["data"] is None
        assert await _platform_snapshot(session_factory) == before

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("case", ("owner-conflict", "renamed-conflict", "owner-free", "renamed-free"))
async def test_superuser_transfer_checks_name_uniqueness_by_final_owner_and_name(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    case: str,
) -> None:
    app, session_factory, platform_ids, calls = api_context
    platform_id = platform_ids["ordinary"]
    other_profile_id = await _get_owned_profile_id(session_factory, "other")
    target_name = {
        "owner-conflict": "ordinary-message-platform",
        "renamed-conflict": "other-message-platform",
        "owner-free": "ordinary-message-platform",
        "renamed-free": "ordinary-local-platform",
    }[case]

    if case == "owner-conflict":
        async with session_factory() as session:
            session.add(
                MessagePlatform(
                    platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
                    uid="other",
                    name="ordinary-message-platform",
                )
            )
            await session.commit()
    elif case == "renamed-free":
        async with session_factory() as session:
            session.add(
                MessagePlatform(
                    platform_type=MessagePlatformType.WEIXIN_OPENCLAW,
                    uid="ordinary",
                    name="ordinary-local-platform",
                )
            )
            await session.commit()

    request_payload: dict[str, Any] = {"uid": " other ", "profile_id": other_profile_id}
    if case in ("renamed-conflict", "renamed-free"):
        request_payload["name"] = target_name

    before = await _platform_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            headers={"Authorization": f"Bearer {_token('admin')}"},
            json=request_payload,
        )
        payload = response.json()

        if case in ("owner-conflict", "renamed-conflict"):
            assert response.status_code == 400
            assert set(payload) == {"code", "message", "data"}
            assert payload["code"] == 400
            assert payload["message"] == t(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
            assert payload["data"] is None
            assert await _platform_snapshot(session_factory) == before
        else:
            assert response.status_code == 200
            assert payload["code"] == 200
            updated = payload["data"]
            assert updated["uid"] == "other"
            assert updated["name"] == target_name
            assert updated["profile_id"] == other_profile_id

            async with session_factory() as session:
                persisted = await session.get(MessagePlatform, platform_id)
                assert persisted is not None
                assert persisted.uid == "other"
                assert persisted.name == target_name
                assert persisted.profile_id == other_profile_id

            after = await _platform_snapshot(session_factory)
            _assert_platforms_unchanged_except(before, after, platform_id)

            old_owner_get = await client.get(
                "/api/v1/message-platforms/get",
                params={"platform_id": platform_id},
                headers={"Authorization": f"Bearer {_token('ordinary')}"},
            )
            _assert_not_found(old_owner_get)

    assert calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("second_actor", ("ordinary", "other"))
async def test_concurrent_users_create_same_message_platform_name(
    api_context: tuple[FastAPI, async_sessionmaker[AsyncSession], dict[str, int], list[str]],
    second_actor: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, session_factory, _platform_ids, calls = api_context
    name = "concurrent-name-platform"
    request_tokens = ("concurrent-token-0", "concurrent-token-1")
    actors = ("ordinary", second_actor)
    barrier = asyncio.Barrier(2)
    original = message_platforms.message_platform_crud.get_by_name

    async def get_by_name_with_barrier(db, queried_name, *, uid):
        result = await original(db, queried_name, uid=uid)
        if queried_name == name:
            await asyncio.wait_for(barrier.wait(), timeout=10)
        return result

    monkeypatch.setattr(message_platforms.message_platform_crud, "get_by_name", get_by_name_with_barrier)
    before = await _platform_snapshot(session_factory)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        responses = await asyncio.wait_for(
            asyncio.gather(
                client.post(
                    "/api/v1/message-platforms/create",
                    headers={"Authorization": f"Bearer {_token(actors[0])}"},
                    json={"name": name, "is_enabled": True, "config": {"token": request_tokens[0]}},
                ),
                client.post(
                    "/api/v1/message-platforms/create",
                    headers={"Authorization": f"Bearer {_token(actors[1])}"},
                    json={"name": name, "is_enabled": True, "config": {"token": request_tokens[1]}},
                ),
            ),
            timeout=30,
        )

    assert sorted(response.status_code for response in responses) == ([200, 400] if second_actor == "ordinary" else [200, 200])
    successful = []
    for index, response in enumerate(responses):
        payload = response.json()
        if response.status_code == 200:
            assert payload["code"] == 200
            created = payload["data"]
            assert created["uid"] == actors[index]
            assert created["name"] == name
            assert created["config"]["token"] == request_tokens[index]
            successful.append((index, created))
        else:
            assert response.status_code == 400
            assert set(payload) == {"code", "message", "data"}
            assert payload["code"] == 400
            assert payload["message"] == t(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
            assert payload["data"] is None
            assert request_tokens[0] not in response.text
            assert request_tokens[1] not in response.text

    async with session_factory() as session:
        same_name = (await session.execute(select(MessagePlatform).where(MessagePlatform.name == name).order_by(MessagePlatform.id))).scalars().all()
        assert len(same_name) == len(successful)
        persisted_by_id = {platform.id: platform for platform in same_name}
        assert set(persisted_by_id) == {created["id"] for _, created in successful}
        for index, created in successful:
            persisted = persisted_by_id[created["id"]]
            assert persisted.uid == actors[index]
            assert persisted.name == name
            assert persisted.get_config_secret("token") == request_tokens[index]

    after = await _platform_snapshot(session_factory)
    before_ids = {row[0] for row in before}
    assert tuple(row for row in after if row[0] in before_ids) == before
    assert len(after) == len(before) + len(successful)
    persisted_tokens = {platform.get_config_secret("token") for platform in same_name}
    assert persisted_tokens == {request_tokens[index] for index, _ in successful}
    assert calls == []
