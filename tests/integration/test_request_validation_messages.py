from collections.abc import AsyncIterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.handler as app_handler
from app.api.v1 import auth, channels, memories, users
from app.core.crud.account.user import user_crud
from app.core.security import get_current_user, verify_password
from app.handler import register_handlers, register_middlewares
from app.providers.database import get_db


@pytest_asyncio.fixture
async def validation_app(
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(app_handler, "AsyncSessionLocal", setup_session_factory)

    app = FastAPI()
    register_handlers(app)
    register_middlewares(app)
    app.include_router(users.router, prefix="/api/v1/admin")
    app.include_router(auth.router, prefix="/api/v1/auth")
    app.include_router(channels.router, prefix="/api/v1")
    app.include_router(memories.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with setup_session_factory() as session:
            yield session

    current_user = SimpleNamespace(uid="validation-admin", is_superuser=True)

    def override_get_current_user() -> SimpleNamespace:
        return current_user

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    yield app


VALIDATION_CASES = [
    {
        "id": "create-short-password-only",
        "method": "POST",
        "path": "/api/v1/admin/user/add",
        "json": {"username": "123", "password": "123"},
        "expected": {
            "zh": ("密码", "至少需要 8 个字符"),
            "en": ("Password", "at least 8 characters"),
        },
        "forbidden": {"zh": ("用户名",), "en": ("Username",)},
        "secrets": ("123",),
    },
    {
        "id": "create-short-username-and-password",
        "method": "POST",
        "path": "/api/v1/admin/user/add",
        "json": {"username": "ab", "password": "123"},
        "expected": {
            "zh": ("用户名", "至少需要 3 个字符", "密码", "至少需要 8 个字符"),
            "en": ("Username", "at least 3 characters", "Password", "at least 8 characters"),
        },
        "secrets": ("ab", "123"),
    },
    {
        "id": "create-invalid-username-format",
        "method": "POST",
        "path": "/api/v1/admin/user/add",
        "json": {"username": "bad.name", "password": "12345678"},
        "expected": {
            "zh": ("只能包含英文字母、数字、下划线和短横线",),
            "en": ("Use only English letters, digits, underscores and hyphens",),
        },
        "secrets": ("bad.name",),
    },
    {
        "id": "create-username-too-long",
        "method": "POST",
        "path": "/api/v1/admin/user/add",
        "json": {"username": "a" * 51, "password": "12345678"},
        "expected": {
            "zh": ("用户名", "最多允许 50 个字符"),
            "en": ("Username", "at most 50 characters"),
        },
        "secrets": ("a" * 51,),
    },
    {
        "id": "create-password-too-long",
        "method": "POST",
        "path": "/api/v1/admin/user/add",
        "json": {"username": "valid-user", "password": "a" * 73},
        "expected": {
            "zh": ("密码", "最多允许 72 个字符"),
            "en": ("Password", "at most 72 characters"),
        },
        "secrets": ("a" * 73,),
    },
    {
        "id": "create-password-too-many-bytes",
        "method": "POST",
        "path": "/api/v1/admin/user/add",
        "json": {"username": "valid-user", "password": "中" * 25},
        "expected": {
            "zh": ("密码", "密码不能超过 72 字节"),
            "en": ("Password", "Password must not exceed 72 bytes"),
        },
        "secrets": ("中" * 25,),
    },
    {
        "id": "create-missing-fields",
        "method": "POST",
        "path": "/api/v1/admin/user/add",
        "json": {},
        "expected": {
            "zh": ("用户名", "缺失必填字段", "密码", "缺失必填字段"),
            "en": ("Username", "Missing required field", "Password", "Missing required field"),
        },
    },
    {
        "id": "create-password-not-text",
        "method": "POST",
        "path": "/api/v1/admin/user/add",
        "json": {"username": "valid-user", "password": 123},
        "expected": {
            "zh": ("密码", "必须填写文本"),
            "en": ("Password", "Must be text"),
        },
        "secrets": ("123",),
    },
    {
        "id": "update-short-password",
        "method": "POST",
        "path": "/api/v1/admin/user/update",
        "json": {"uid": "missing", "password": "123"},
        "expected": {
            "zh": ("密码", "至少需要 8 个字符"),
            "en": ("Password", "at least 8 characters"),
        },
        "secrets": ("missing", "123"),
    },
    {
        "id": "update-invalid-active-flag",
        "method": "POST",
        "path": "/api/v1/admin/user/update",
        "json": {"uid": "missing", "is_active": "invalid"},
        "expected": {
            "zh": ("is_active", "必须是布尔值（true 或 false）"),
            "en": ("is_active", "Must be a boolean (true or false)"),
        },
        "secrets": ("missing", "invalid"),
    },
    {
        "id": "login-empty-password",
        "method": "POST",
        "path": "/api/v1/auth/login",
        "json": {"username": "admin", "password": ""},
        "expected": {
            "zh": ("密码", "至少需要 1 个字符"),
            "en": ("Password", "at least 1 characters"),
        },
        "secrets": ("admin",),
    },
    {
        "id": "channel-name-empty",
        "method": "POST",
        "path": "/api/v1/channels/create",
        "json": {"name": "", "api_key": "validation-private-api-key"},
        "expected": {
            "zh": ("name", "至少需要 1 个字符"),
            "en": ("name", "at least 1 characters"),
        },
        "secrets": ("validation-private-api-key",),
    },
    {
        "id": "channel-model-item-must-be-object",
        "method": "POST",
        "path": "/api/v1/channels/create",
        "json": {
            "name": "validation-channel",
            "api_key": "validation-private-api-key",
            "model_ids": [["validation-private-api-key"]],
        },
        "expected": {
            "zh": ("model_ids.0", "必须是对象"),
            "en": ("model_ids.0", "Must be an object"),
        },
        "secrets": ("validation-private-api-key",),
    },
    {
        "id": "channel-http-proxy-scheme",
        "method": "POST",
        "path": "/api/v1/channels/create",
        "json": {
            "name": "validation-channel",
            "api_key": "validation-private-api-key",
            "http_proxy": "https://proxy.example:8080",
        },
        "expected": {
            "zh": ("http://host:port", "仅支持"),
            "en": ("http://host:port", "Only"),
        },
        "forbidden": {"zh": ("Value error,",), "en": ("Value error,",)},
        "secrets": ("validation-private-api-key",),
    },
    {
        "id": "memory-page-minimum",
        "method": "GET",
        "path": "/api/v1/memories/list",
        "params": {"page": 0},
        "expected": {
            "zh": ("page", "必须大于或等于 1"),
            "en": ("page", "Must be greater than or equal to 1"),
        },
    },
    {
        "id": "memory-size-maximum",
        "method": "GET",
        "path": "/api/v1/memories/list",
        "params": {"size": 101},
        "expected": {
            "zh": ("size", "必须小于或等于 100"),
            "en": ("size", "Must be less than or equal to 100"),
        },
    },
    {
        "id": "memory-page-integer",
        "method": "GET",
        "path": "/api/v1/memories/list",
        "params": {"page": "bad"},
        "expected": {
            "zh": ("page", "必须是有效的整数"),
            "en": ("page", "Must be a valid integer"),
        },
        "secrets": ("bad",),
    },
    {
        "id": "memory-sort-enum",
        "method": "GET",
        "path": "/api/v1/memories/list",
        "params": {"sort_by": "invalid"},
        "expected": {
            "zh": ("sort_by", "必须为以下值之一", "updated_at"),
            "en": ("sort_by", "Must be one of", "updated_at"),
        },
        "secrets": ("invalid",),
    },
    {
        "id": "memory-job-id-minimum",
        "method": "GET",
        "path": "/api/v1/memories/jobs/0",
        "expected": {
            "zh": ("job_id", "必须大于或等于 1"),
            "en": ("job_id", "Must be greater than or equal to 1"),
        },
    },
]


REQUEST_SHAPE_CASES = [
    {
        "id": "missing-body",
        "expected": {
            "zh": ("请求数据", "缺失必填字段"),
            "en": ("Request data", "Missing required field"),
        },
    },
    {
        "id": "list-body",
        "expected": {
            "zh": ("请求数据", "必须是对象"),
            "en": ("Request data", "Must be an object"),
        },
    },
    {
        "id": "invalid-json-with-secret",
        "expected": {
            "zh": ("请求数据", "无效的 JSON 格式"),
            "en": ("Request data", "Invalid JSON format"),
        },
        "secrets": ("validation-json-secret",),
    },
]


def _assert_validation(
    response: httpx.Response,
    expected: tuple[str, ...],
    *,
    forbidden: tuple[str, ...] = (),
    secrets: tuple[str, ...] = (),
) -> str:
    payload = response.json()
    assert response.status_code == 422
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == 422
    assert payload["data"] is None
    message = payload["message"]
    assert isinstance(message, str) and message
    assert message not in {"参数验证失败", "Parameter validation failed"}
    assert all(fragment in message for fragment in expected)
    assert all(fragment not in message for fragment in forbidden)
    assert all(fragment not in message for fragment in ("{", "}", "Input should", "String should", "Field required", "Value error,"))
    assert all(secret not in message for secret in secrets)
    return message


async def _user_count(setup_session_factory: async_sessionmaker[AsyncSession]) -> int:
    async with setup_session_factory() as session:
        return await user_crud.count(session)


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize("case", VALIDATION_CASES, ids=lambda case: case["id"])
async def test_request_validation_messages(
    validation_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    locale: str,
    case: dict[str, Any],
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_app),
        base_url="http://test",
        headers={"Accept-Language": locale},
    ) as client:
        response = await client.request(
            case["method"],
            case["path"],
            json=case.get("json"),
            params=case.get("params"),
        )

    _assert_validation(
        response,
        case["expected"][locale],
        forbidden=case.get("forbidden", {}).get(locale, ()),
        secrets=case.get("secrets", ()),
    )
    assert await _user_count(setup_session_factory) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize("case", REQUEST_SHAPE_CASES, ids=lambda case: case["id"])
async def test_request_body_shape_validation_messages(
    validation_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    locale: str,
    case: dict[str, Any],
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_app),
        base_url="http://test",
        headers={"Accept-Language": locale},
    ) as client:
        if case["id"] == "missing-body":
            response = await client.post("/api/v1/admin/user/add")
        elif case["id"] == "list-body":
            response = await client.post("/api/v1/admin/user/add", json=[])
        else:
            response = await client.post(
                "/api/v1/admin/user/add",
                content='{"username":"valid-user","password":"validation-json-secret"',
                headers={"Content-Type": "application/json"},
            )

    _assert_validation(response, case["expected"][locale], secrets=case.get("secrets", ()))
    assert response.status_code != 500
    assert await _user_count(setup_session_factory) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
async def test_user_validation_and_business_errors_preserve_persistence(
    validation_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    locale: str,
) -> None:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=validation_app),
        base_url="http://test",
        headers={"Accept-Language": locale},
    ) as client:
        created = await client.post(
            "/api/v1/admin/user/add",
            json={"username": "123", "password": "12345678"},
        )
        created_payload = created.json()
        assert created.status_code == 200
        assert set(created_payload) == {"code", "message", "data"}
        assert created_payload["code"] == 200
        uid = created_payload["data"]["uid"]

        async with setup_session_factory() as session:
            user = await user_crud.get_by_uid(session, uid)
            assert user is not None
            assert user.hashed_password is not None
            original_hash = user.hashed_password
            assert verify_password("12345678", original_hash)

        invalid_update = await client.post(
            "/api/v1/admin/user/update",
            json={"uid": uid, "password": "123"},
        )
        _assert_validation(
            invalid_update,
            {
                "zh": ("密码", "至少需要 8 个字符"),
                "en": ("Password", "at least 8 characters"),
            }[locale],
            secrets=(uid, "123"),
        )

        async with setup_session_factory() as session:
            persisted = await user_crud.get_by_uid(session, uid)
            assert persisted is not None
            assert persisted.hashed_password == original_hash
            assert verify_password("12345678", persisted.hashed_password)

        duplicate = await client.post(
            "/api/v1/admin/user/add",
            json={"username": "123", "password": "12345678"},
        )
        duplicate_payload = duplicate.json()
        assert duplicate.status_code == 400
        assert set(duplicate_payload) == {"code", "message", "data"}
        assert duplicate_payload["code"] == 400
        assert duplicate_payload["data"] is None
        expected_message = {"zh": "用户名已存在", "en": "Username already exists"}[locale]
        assert duplicate_payload["message"] == expected_message

    assert await _user_count(setup_session_factory) == 1
