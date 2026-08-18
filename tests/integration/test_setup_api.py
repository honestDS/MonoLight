from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.api.v1.setup as setup_api
import app.core.crypto as crypto_module
import app.core.setup as setup_service
from app.core.constants import (
    ERR_SETUP_ALREADY_COMPLETED,
    ERR_SETUP_CONFLICT,
    ERR_SETUP_NOT_ALLOWED,
    ERR_SETUP_STATUS_INVALID,
    ERR_SETUP_STATUS_NOT_INITIALIZED,
    ERR_SYSTEM_SECRETS_FILE_INVALID,
    MSG_SETUP_STATUS_SUCCESS,
    SETUP_STATUS_COMPLETED,
    SETUP_STATUS_CONFIGURING,
    SETUP_STATUS_KEY,
    SETUP_STATUS_PENDING,
)
from app.core.i18n import t
from app.core.system_secrets import SystemSecrets, SystemSecretsError
from app.handler import register_handlers
from app.models.channel import ENCRYPTED_API_KEY_PREFIX, MODEL_PROTOCOLS_BY_USAGE, ModelChannel, ModelUsage
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.system_setting import SystemSetting
from app.models.user import User
from app.providers.database import get_db
from app.schemas.setup import SetupAdminInput, SetupChannelInput, SetupCompleteRequest, SetupProfileInput

FIXED_ACCESS_TOKEN = "setup-api-fixed-token"
FIXED_JWT_SECRET = "setup-api-fixed-jwt-secret"
TEST_USERNAME = "setup_admin"
TEST_PASSWORD = "setup-password-123"
TEST_API_KEY = "setup-api-key"
TEST_MODEL_ID = "setup-chat-model"


def _successful_system_secrets() -> SystemSecrets:
    return SystemSecrets(jwt_secret_key=FIXED_JWT_SECRET, channel_encryption_key=b"\x11" * 32)


def _setup_payload(
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
    api_key: str = TEST_API_KEY,
    channel_name: str = "setup-channel",
    model_id: str = TEST_MODEL_ID,
    profile_name: str = "setup-profile",
) -> dict[str, Any]:
    return {
        "admin": {"username": username, "password": password},
        "channel": {
            "name": channel_name,
            "base_url": "https://api.example.test/v1",
            "api_key": api_key,
            "model_id": model_id,
            "protocol": "OPENAI",
        },
        "profile": {"name": profile_name},
    }


@pytest_asyncio.fixture
async def setup_app(
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[FastAPI]:
    monkeypatch.setattr(setup_api, "load_system_secrets", _successful_system_secrets)
    monkeypatch.setattr(crypto_module, "get_channel_encryption_key", lambda: b"\x11" * 32)
    monkeypatch.setattr(setup_service, "create_access_token", lambda _data: FIXED_ACCESS_TOKEN)

    app = FastAPI()
    register_handlers(app)
    app.include_router(setup_api.router, prefix="/api/v1")

    async def override_get_db() -> AsyncIterator[AsyncSession]:
        async with setup_session_factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    yield app


def _assert_standard_response(response: httpx.Response, code: int) -> dict[str, Any]:
    payload = response.json()
    assert response.status_code == code
    assert set(payload) == {"code", "message", "data"}
    assert payload["code"] == code
    assert isinstance(payload["message"], str)
    assert payload["message"]
    return payload


async def _request(
    app: FastAPI,
    method: str,
    path: str,
    *,
    json: dict[str, Any] | None = None,
) -> httpx.Response:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        return await client.request(method, path, json=json)


async def _set_setup_status(
    session_factory: async_sessionmaker[AsyncSession],
    status: str | None,
) -> None:
    async with session_factory() as session:
        result = await session.execute(select(SystemSetting).where(SystemSetting.key == SETUP_STATUS_KEY))
        setting = result.scalar_one_or_none()
        if status is None:
            assert setting is not None
            await session.delete(setting)
        else:
            assert setting is not None
            setting.value = status
            session.add(setting)
        await session.commit()


async def _read_setup_data(session_factory: async_sessionmaker[AsyncSession]) -> dict[str, Any]:
    async with session_factory() as session:
        settings_result = await session.execute(select(SystemSetting))
        users_result = await session.execute(select(User))
        channels_result = await session.execute(select(ModelChannel))
        prompts_result = await session.execute(select(PromptLibrary))
        profiles_result = await session.execute(select(Profile))
        return {
            "settings": {setting.key: setting.value for setting in settings_result.scalars().all()},
            "users": list(users_result.scalars().all()),
            "channels": list(channels_result.scalars().all()),
            "prompts": list(prompts_result.scalars().all()),
            "profiles": list(profiles_result.scalars().all()),
        }


def _business_record_counts(database: dict[str, Any]) -> dict[str, int]:
    return {key: len(database[key]) for key in ("users", "channels", "prompts", "profiles")}


@pytest.mark.asyncio
async def test_setup_status_pending_and_completed_returns_required_flag(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    pending_response = await _request(setup_app, "GET", "/api/v1/setup/status")
    pending_payload = _assert_standard_response(pending_response, 200)
    assert pending_payload["message"] == t(MSG_SETUP_STATUS_SUCCESS)
    assert pending_payload["data"] == {"required": True}
    assert set(pending_payload["data"]) == {"required"}

    await _set_setup_status(setup_session_factory, SETUP_STATUS_COMPLETED)
    completed_response = await _request(setup_app, "GET", "/api/v1/setup/status")
    completed_payload = _assert_standard_response(completed_response, 200)
    assert completed_payload["message"] == t(MSG_SETUP_STATUS_SUCCESS)
    assert completed_payload["data"] == {"required": False}
    assert set(completed_payload["data"]) == {"required"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (SETUP_STATUS_CONFIGURING, ERR_SETUP_NOT_ALLOWED),
        (None, ERR_SETUP_STATUS_NOT_INITIALIZED),
        ("unknown-status", ERR_SETUP_STATUS_INVALID),
    ],
)
async def test_setup_status_rejects_non_readable_states(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    status: str | None,
    expected_error: str,
) -> None:
    await _set_setup_status(setup_session_factory, status)

    response = await _request(setup_app, "GET", "/api/v1/setup/status")
    payload = _assert_standard_response(response, 409 if status == SETUP_STATUS_CONFIGURING else 500)
    assert payload["message"] == t(expected_error)
    assert payload["data"] is None
    assert payload["data"] != {"required": False}


@pytest.mark.asyncio
async def test_setup_models_pending_forwards_payload_and_returns_standard_response(
    setup_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_payload = {
        "api_key": "setup-models-api-key",
        "base_url": "https://models.example.test/v1",
        "http_proxy": "http://proxy.example.test:8080",
        "timeout": 12,
    }
    forwarded: dict[str, Any] = {}

    async def fake_list_channel_models(*, payload: Any, _admin: dict[str, Any]) -> Any:
        forwarded["payload"] = payload
        forwarded["admin"] = _admin
        return setup_api.StandardResponse.success(data={"models": [{"id": "setup-model"}]})

    monkeypatch.setattr(setup_api, "list_channel_models", fake_list_channel_models)

    response = await _request(setup_app, "POST", "/api/v1/setup/models", json=request_payload)
    body = _assert_standard_response(response, 200)

    assert forwarded["payload"].model_dump(mode="json") == {
        "api_key": "setup-models-api-key",
        "base_url": "https://models.example.test/v1",
        "http_proxy": "http://proxy.example.test:8080",
        "timeout": 12.0,
    }
    assert forwarded["admin"] == {}
    assert body["data"] == {"models": [{"id": "setup-model"}]}


@pytest.mark.asyncio
async def test_setup_chat_pending_forwards_payload_and_returns_standard_response(
    setup_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request_payload = {
        "prompt": "ping",
        "protocol": "OPENAI",
        "api_key": "setup-chat-api-key",
        "base_url": "https://chat.example.test/v1",
        "model_id": "setup-chat-model",
        "temperature": 0.4,
        "top_p": 0.8,
        "max_tokens": 128,
        "timeout": 45,
        "http_proxy": "http://proxy.example.test:8080",
        "advanced_settings": {"custom_headers": {"X-Setup-Test": "enabled"}},
        "test_mode": "non_stream",
    }
    forwarded: dict[str, Any] = {}

    async def fake_test_channel_chat(*, payload: Any, _admin: dict[str, Any]) -> Any:
        forwarded["payload"] = payload
        forwarded["admin"] = _admin
        return setup_api.StandardResponse.success(data={"model": "setup-chat-model", "reply": "pong"})

    monkeypatch.setattr(setup_api, "test_channel_chat", fake_test_channel_chat)

    response = await _request(setup_app, "POST", "/api/v1/setup/test-chat", json=request_payload)
    body = _assert_standard_response(response, 200)

    assert forwarded["payload"].model_dump(mode="json") == {
        "advanced_settings": {"custom_headers": {"x-setup-test": "enabled"}},
        "api_key": "setup-chat-api-key",
        "base_url": "https://chat.example.test/v1",
        "http_proxy": "http://proxy.example.test:8080",
        "max_tokens": 128,
        "model_id": "setup-chat-model",
        "prompt": "ping",
        "protocol": "OPENAI",
        "temperature": 0.4,
        "test_mode": "non_stream",
        "timeout": 45.0,
        "top_p": 0.8,
    }
    assert forwarded["admin"] == {}
    assert body["data"] == {"model": "setup-chat-model", "reply": "pong"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (SETUP_STATUS_COMPLETED, ERR_SETUP_ALREADY_COMPLETED),
        (SETUP_STATUS_CONFIGURING, ERR_SETUP_NOT_ALLOWED),
    ],
)
@pytest.mark.parametrize(
    ("path", "payload", "delegate_name"),
    [
        (
            "/api/v1/setup/models",
            {
                "api_key": "setup-models-api-key",
                "base_url": "https://models.example.test/v1",
            },
            "list_channel_models",
        ),
        (
            "/api/v1/setup/test-chat",
            {
                "prompt": "ping",
                "protocol": "OPENAI",
                "api_key": "setup-chat-api-key",
                "base_url": "https://chat.example.test/v1",
                "model_id": "setup-chat-model",
            },
            "test_channel_chat",
        ),
    ],
)
async def test_setup_probe_routes_reject_completed_or_configuring_state(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    expected_error: str,
    path: str,
    payload: dict[str, Any],
    delegate_name: str,
) -> None:
    await _set_setup_status(setup_session_factory, status)

    async def fail_delegate(**_kwargs: Any) -> Any:
        pytest.fail(f"{delegate_name} should not be called for setup status {status}")

    monkeypatch.setattr(setup_api, delegate_name, fail_delegate)

    response = await _request(setup_app, "POST", path, json=payload)
    body = _assert_standard_response(response, 409)
    assert body["message"] == t(expected_error)
    assert body["data"] is None


@pytest.mark.asyncio
async def test_setup_complete_returns_only_token_data_and_creates_initial_records(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = _setup_payload()
    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=payload)
    body = _assert_standard_response(response, 200)

    assert set(body["data"]) == {"access_token", "token_type"}
    assert body["data"] == {"access_token": FIXED_ACCESS_TOKEN, "token_type": "bearer"}
    assert TEST_PASSWORD not in response.text
    assert TEST_API_KEY not in response.text
    assert FIXED_JWT_SECRET not in response.text
    assert "jwt_secret_key" not in response.text
    assert "channel_encryption_key" not in response.text

    database = await _read_setup_data(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED

    users = database["users"]
    assert len(users) == 1
    assert sum(user.is_superuser for user in users) == 1

    channels = database["channels"]
    assert len(channels) == 1
    channel = channels[0]
    assert channel.api_key.startswith(ENCRYPTED_API_KEY_PREFIX)
    assert channel.api_key != TEST_API_KEY
    assert channel.get_decrypted_api_key() == TEST_API_KEY

    prompts = database["prompts"]
    assert len(prompts) == 1
    assert prompts[0].name == "default"

    profiles = database["profiles"]
    assert len(profiles) == 1
    assert profiles[0].uid == users[0].uid
    assert profiles[0].prompt_id == prompts[0].id


@pytest.mark.asyncio
async def test_setup_complete_rejects_repeated_submission_without_changing_record_counts(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = _setup_payload()
    first_response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=payload)
    _assert_standard_response(first_response, 200)
    before = await _read_setup_data(setup_session_factory)

    repeated_response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=payload)
    repeated_body = _assert_standard_response(repeated_response, 409)
    assert repeated_body["message"] == t(ERR_SETUP_ALREADY_COMPLETED)
    assert repeated_body["data"] is None

    after = await _read_setup_data(setup_session_factory)
    assert _business_record_counts(after) == _business_record_counts(before)


@pytest.mark.asyncio
async def test_setup_complete_rejects_configuring_state_without_creating_records(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    await _set_setup_status(setup_session_factory, SETUP_STATUS_CONFIGURING)

    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=_setup_payload())
    body = _assert_standard_response(response, 409)
    assert body["message"] == t(ERR_SETUP_NOT_ALLOWED)
    assert body["data"] is None

    database = await _read_setup_data(setup_session_factory)
    assert _business_record_counts(database) == {"users": 0, "channels": 0, "prompts": 0, "profiles": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected_error"),
    [
        (None, ERR_SETUP_STATUS_NOT_INITIALIZED),
        ("unknown-status", ERR_SETUP_STATUS_INVALID),
    ],
)
async def test_setup_complete_rejects_missing_or_unknown_status_without_creating_records(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    status: str | None,
    expected_error: str,
) -> None:
    await _set_setup_status(setup_session_factory, status)

    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=_setup_payload())
    body = _assert_standard_response(response, 500)
    assert body["message"] == t(expected_error)
    assert body["data"] is None

    database = await _read_setup_data(setup_session_factory)
    assert _business_record_counts(database) == {"users": 0, "channels": 0, "prompts": 0, "profiles": 0}


@pytest.mark.asyncio
async def test_concurrent_setup_completion_has_one_success_and_one_conflict(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_get_valid_setup_status = setup_api._get_valid_setup_status
    status_barrier = asyncio.Barrier(2)

    async def synchronized_get_valid_setup_status(db: AsyncSession) -> str:
        status = await original_get_valid_setup_status(db)
        assert status == SETUP_STATUS_PENDING
        await status_barrier.wait()
        return status

    monkeypatch.setattr(setup_api, "_get_valid_setup_status", synchronized_get_valid_setup_status)

    async def submit_setup() -> httpx.Response:
        return await _request(setup_app, "POST", "/api/v1/setup/complete", json=_setup_payload())

    responses = await asyncio.gather(submit_setup(), submit_setup())
    bodies = [response.json() for response in responses]
    assert sorted(response.status_code for response in responses) == [200, 409]
    assert sum(body["code"] == 200 for body in bodies) == 1
    assert sum(body["code"] == 409 and body["message"] == t(ERR_SETUP_CONFLICT) for body in bodies) == 1
    assert all(set(body) == {"code", "message", "data"} for body in bodies)

    database = await _read_setup_data(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
    assert _business_record_counts(database) == {"users": 1, "channels": 1, "prompts": 1, "profiles": 1}


@pytest.mark.asyncio
async def test_setup_status_hides_system_secrets_file_error_details(
    setup_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_invalid_system_secrets() -> SystemSecrets:
        raise SystemSecretsError(ERR_SYSTEM_SECRETS_FILE_INVALID)

    monkeypatch.setattr(setup_api, "load_system_secrets", raise_invalid_system_secrets)

    response = await _request(setup_app, "GET", "/api/v1/setup/status")
    body = _assert_standard_response(response, 500)
    assert body == {"code": 500, "message": t(ERR_SYSTEM_SECRETS_FILE_INVALID), "data": None}
    assert FIXED_JWT_SECRET not in response.text
    assert "system_secrets.json" not in response.text


@pytest.mark.asyncio
async def test_setup_complete_hides_system_secrets_file_error_details_and_preserves_pending_state(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_invalid_system_secrets() -> SystemSecrets:
        raise SystemSecretsError(ERR_SYSTEM_SECRETS_FILE_INVALID)

    monkeypatch.setattr(setup_api, "load_system_secrets", raise_invalid_system_secrets)

    sensitive_password = "setup-sensitive-password"
    sensitive_api_key = "setup-sensitive-api-key"
    payload = _setup_payload(password=sensitive_password, api_key=sensitive_api_key)
    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=payload)
    body = _assert_standard_response(response, 500)
    assert body == {"code": 500, "message": t(ERR_SYSTEM_SECRETS_FILE_INVALID), "data": None}
    assert sensitive_password not in response.text
    assert sensitive_api_key not in response.text
    assert FIXED_JWT_SECRET not in response.text
    assert "password" not in response.text
    assert "api_key" not in response.text
    assert "jwt_secret_key" not in response.text
    assert "channel_encryption_key" not in response.text
    assert "system_secrets.json" not in response.text

    database = await _read_setup_data(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
    assert _business_record_counts(database) == {"users": 0, "channels": 0, "prompts": 0, "profiles": 0}


@pytest.mark.asyncio
async def test_setup_complete_validation_error_is_standard_and_does_not_echo_secrets(
    setup_app: FastAPI,
) -> None:
    invalid_password = "bad"
    sensitive_api_key = "sensitive-api-key-123"
    payload = _setup_payload(password=invalid_password, api_key=sensitive_api_key)

    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=payload)
    body = _assert_standard_response(response, 422)
    assert body["data"] is None
    assert invalid_password not in response.text
    assert sensitive_api_key not in response.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("section", "field", "invalid_value"),
    [
        ("admin", "password", "中" * 25),
        ("channel", "base_url", "ftp://api.example.test"),
        ("channel", "model_id", "   "),
        ("channel", "protocol", "OPENAI_EMBEDDING"),
    ],
)
async def test_setup_complete_rejects_common_validation_errors_without_writes(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    section: str,
    field: str,
    invalid_value: str,
) -> None:
    payload = _setup_payload()
    payload[section][field] = invalid_value

    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=payload)
    body = _assert_standard_response(response, 422)
    assert body["data"] is None
    assert TEST_API_KEY not in response.text
    if section == "admin" and field == "password":
        assert invalid_value not in response.text

    database = await _read_setup_data(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
    assert _business_record_counts(database) == {"users": 0, "channels": 0, "prompts": 0, "profiles": 0}


def _resolve_schema(openapi: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    resolved = schema
    while "$ref" in resolved:
        resolved = openapi["components"]["schemas"][resolved["$ref"].rsplit("/", 1)[-1]]
    return resolved


def _object_schema(openapi: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    resolved = _resolve_schema(openapi, schema)
    if "allOf" not in resolved:
        return resolved

    properties: dict[str, Any] = {}
    required: list[str] = []
    for part in resolved["allOf"]:
        part_schema = _object_schema(openapi, part)
        properties.update(part_schema.get("properties", {}))
        required.extend(part_schema.get("required", []))
    merged = dict(resolved)
    merged["properties"] = properties
    merged["required"] = required
    return merged


def _schema_refs(schema: Any) -> set[str]:
    if isinstance(schema, dict):
        refs = {schema["$ref"]} if "$ref" in schema else set()
        for value in schema.values():
            refs.update(_schema_refs(value))
        return refs
    if isinstance(schema, list):
        refs: set[str] = set()
        for value in schema:
            refs.update(_schema_refs(value))
        return refs
    return set()


def _assert_generic_response_schema(
    openapi: dict[str, Any],
    operation: dict[str, Any],
    data_schema_name: str,
) -> None:
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    generic_response = _resolve_schema(openapi, response_schema)
    assert response_schema["$ref"].startswith("#/components/schemas/StandardResponse_")
    data_schema = generic_response["properties"]["data"]
    assert f"#/components/schemas/{data_schema_name}" in _schema_refs(data_schema)


def _assert_error_response_models(
    operation: dict[str, Any],
    status_codes: tuple[str, ...],
) -> None:
    for status_code in status_codes:
        response_schema = operation["responses"][status_code]["content"]["application/json"]["schema"]
        assert response_schema == {"$ref": "#/components/schemas/StandardResponse"}


def test_main_openapi_exposes_setup_contract_without_reset_admin() -> None:
    from main import create_app

    openapi = create_app().openapi()
    paths = openapi["paths"]
    setup_paths = {path for path in paths if path.startswith("/api/v1/setup/")}
    assert setup_paths == {
        "/api/v1/setup/status",
        "/api/v1/setup/models",
        "/api/v1/setup/test-chat",
        "/api/v1/setup/complete",
    }
    assert "/api/v1/auth/reset_admin" not in paths
    assert "ResetAdminRequest" not in openapi["components"]["schemas"]

    request_schema = _object_schema(
        openapi,
        paths["/api/v1/setup/complete"]["post"]["requestBody"]["content"]["application/json"]["schema"],
    )
    assert set(request_schema["properties"]) == {"admin", "channel", "profile"}
    assert set(request_schema["required"]) == {"admin", "channel", "profile"}

    admin_schema = _object_schema(openapi, request_schema["properties"]["admin"])
    channel_schema = _object_schema(openapi, request_schema["properties"]["channel"])
    profile_schema = _object_schema(openapi, request_schema["properties"]["profile"])
    assert set(admin_schema["properties"]) == set(SetupAdminInput.model_fields) == {"username", "password"}
    assert (
        set(channel_schema["properties"])
        == set(SetupChannelInput.model_fields)
        == {
            "name",
            "base_url",
            "api_key",
            "http_proxy",
            "model_id",
            "protocol",
            "image_understanding",
            "audio_understanding",
            "video_understanding",
            "context_window_k",
            "temperature",
            "top_p",
            "max_tokens",
            "description",
            "advanced_settings",
        }
    )
    protocol_schema = _resolve_schema(openapi, channel_schema["properties"]["protocol"])
    assert set(protocol_schema["enum"]) == {protocol.value for protocol in MODEL_PROTOCOLS_BY_USAGE[ModelUsage.CHAT]}
    assert set(profile_schema["properties"]) == set(SetupProfileInput.model_fields) == {"name"}
    assert set(SetupCompleteRequest.model_fields) == {"admin", "channel", "profile"}

    status_operation = paths["/api/v1/setup/status"]["get"]
    models_operation = paths["/api/v1/setup/models"]["post"]
    chat_operation = paths["/api/v1/setup/test-chat"]["post"]
    complete_operation = paths["/api/v1/setup/complete"]["post"]
    _assert_generic_response_schema(openapi, status_operation, "SetupStatusData")
    _assert_generic_response_schema(openapi, complete_operation, "SetupTokenData")
    _assert_error_response_models(status_operation, ("409", "500"))
    for operation, request_schema_name in (
        (models_operation, "ChannelModelListRequest"),
        (chat_operation, "ChannelChatTestRequest"),
    ):
        assert operation["requestBody"]["content"]["application/json"]["schema"] == {
            "$ref": f"#/components/schemas/{request_schema_name}"
        }
        assert operation["responses"]["200"]["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/StandardResponse"
        }
        _assert_error_response_models(operation, ("409", "500"))
    _assert_error_response_models(complete_operation, ("409", "422", "500"))

    status_data_schema = _object_schema(openapi, openapi["components"]["schemas"]["SetupStatusData"])
    token_data_schema = _object_schema(openapi, openapi["components"]["schemas"]["SetupTokenData"])
    assert set(status_data_schema["properties"]) == {"required"}
    assert set(token_data_schema["properties"]) == {"access_token", "token_type"}
