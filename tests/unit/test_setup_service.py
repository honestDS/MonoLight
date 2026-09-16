from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

import app.api.v1.auth as auth
from app.core.constants import (
    ERR_PASSWORD_TOO_LONG_BYTES,
    ERR_USER_NOT_FOUND_OR_DISABLED,
    ERR_VALIDATION_FAILED,
)
from app.core.exceptions import AuthException
from app.core.i18n import t
from app.handler import validation_exception_handler
from app.models.channel import ModelProtocol
from app.models.user import UserCreate, UserUpdate
from app.schemas.auth import LoginRequest
from app.schemas.setup import SetupCompleteRequest

TEST_USERNAME = "setup_admin"
TEST_PASSWORD = "correct-password-123"
TEST_API_KEY = "setup-api-key"


def make_setup_request(
    *,
    username: str = TEST_USERNAME,
    password: str = TEST_PASSWORD,
    base_url: str = "https://api.example.test/v1",
    model_id: str = "chat-model",
    protocol: ModelProtocol = ModelProtocol.OPENAI,
    channel_name: str = "setup-channel",
    profile_name: str = "setup-profile",
) -> SetupCompleteRequest:
    return SetupCompleteRequest.model_validate(
        {
            "admin": {"username": username, "password": password},
            "channel": {
                "name": channel_name,
                "base_url": base_url,
                "api_key": TEST_API_KEY,
                "model_id": model_id,
                "protocol": protocol,
                "context_window_k": 64,
            },
            "profile": {"name": profile_name},
        }
    )


def test_setup_schema_accepts_valid_username_and_utf8_password_limit() -> None:
    request = make_setup_request(username="admin_user-01", password="中" * 24)

    assert request.admin.username == "admin_user-01"
    assert len(request.admin.password.encode("utf-8")) == 72

    with pytest.raises(ValidationError):
        make_setup_request(password="中" * 25)


@pytest.mark.parametrize("api_key", ["", " ", "\t\n"])
def test_setup_schema_rejects_blank_api_key(api_key: str) -> None:
    payload = make_setup_request().model_dump(mode="json")
    payload["channel"]["api_key"] = api_key

    with pytest.raises(ValidationError):
        SetupCompleteRequest.model_validate(payload)


def test_setup_schema_preserves_non_blank_api_key_whitespace() -> None:
    api_key = "  setup-api-key  "
    payload = make_setup_request().model_dump(mode="json")
    payload["channel"]["api_key"] = api_key

    request = SetupCompleteRequest.model_validate(payload)

    assert request.channel.api_key == api_key


def test_user_update_password_uses_utf8_byte_limit() -> None:
    update = UserUpdate(uid="existing-user", password="中" * 24)

    assert len(update.password.encode("utf-8")) == 72
    assert UserUpdate(uid="existing-user", password=None).password is None

    with pytest.raises(ValidationError):
        UserUpdate(uid="existing-user", password="中" * 25)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model", "payload"),
    [
        pytest.param(UserCreate, {"username": "valid_user", "password": "中" * 25}, id="user-create"),
        pytest.param(UserUpdate, {"uid": "existing-user", "password": "中" * 25}, id="user-update"),
    ],
)
async def test_validation_exception_handler_translates_password_byte_limit(
    model: type[UserCreate | UserUpdate],
    payload: dict[str, str],
) -> None:
    with pytest.raises(ValidationError) as exc_info:
        model.model_validate(payload)

    errors = exc_info.value.errors()
    assert any(error["type"] == ERR_PASSWORD_TOO_LONG_BYTES for error in errors)

    response = await validation_exception_handler(SimpleNamespace(), RequestValidationError(errors))
    body = json.loads(response.body)

    assert response.status_code == 422
    assert body["code"] == 422
    assert body["message"] == t(ERR_PASSWORD_TOO_LONG_BYTES)
    assert body["data"] is None


@pytest.mark.asyncio
async def test_validation_exception_handler_keeps_generic_validation_message() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UserCreate(username="ab", password="valid_password")

    response = await validation_exception_handler(SimpleNamespace(), RequestValidationError(exc_info.value.errors()))
    body = json.loads(response.body)

    assert response.status_code == 422
    assert body["code"] == 422
    assert body["message"] == t(ERR_VALIDATION_FAILED)
    assert body["data"] is None


@pytest.mark.parametrize("username", ["ab", "bad.name", "管理员"])
def test_setup_schema_rejects_invalid_username(username: str) -> None:
    with pytest.raises(ValidationError):
        make_setup_request(username=username)


@pytest.mark.parametrize("base_url", ["api.example.test", "ftp://api.example.test", ""])
def test_setup_schema_rejects_invalid_url(base_url: str) -> None:
    with pytest.raises(ValidationError):
        make_setup_request(base_url=base_url)


def test_setup_schema_rejects_missing_url() -> None:
    payload = make_setup_request().model_dump(mode="json")
    del payload["channel"]["base_url"]

    with pytest.raises(ValidationError):
        SetupCompleteRequest.model_validate(payload)


def test_setup_schema_rejects_missing_context_window() -> None:
    payload = make_setup_request().model_dump(mode="json")
    del payload["channel"]["context_window_k"]

    with pytest.raises(ValidationError):
        SetupCompleteRequest.model_validate(payload)


def test_setup_schema_rejects_empty_model() -> None:
    with pytest.raises(ValidationError):
        make_setup_request(model_id="")


def test_setup_schema_rejects_non_chat_protocol() -> None:
    with pytest.raises(ValidationError):
        make_setup_request(protocol=ModelProtocol.OPENAI_EMBEDDING)


@pytest.mark.parametrize("protocol", [ModelProtocol.OPENAI, ModelProtocol.OPENAI_RESPONSES])
def test_setup_schema_accepts_chat_protocols(protocol: ModelProtocol) -> None:
    request = make_setup_request(protocol=protocol)

    assert request.channel.protocol == protocol


@pytest.mark.asyncio
async def test_login_short_password_reaches_authentication_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = object()
    get_by_username = AsyncMock(return_value=None)
    monkeypatch.setattr(auth, "user_crud", SimpleNamespace(get_by_username=get_by_username))
    request = LoginRequest(username="short-password-user", password="short")

    with pytest.raises(AuthException) as exc_info:
        await auth.login(request, db)

    assert exc_info.value.code == 401
    assert exc_info.value.message == ERR_USER_NOT_FOUND_OR_DISABLED
    get_by_username.assert_awaited_once_with(db, request.username)


@pytest.mark.asyncio
async def test_login_rejects_utf8_password_over_72_bytes_before_user_lookup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    get_by_username = AsyncMock()
    monkeypatch.setattr(auth, "user_crud", SimpleNamespace(get_by_username=get_by_username))
    request = LoginRequest(username="long-password-user", password="中" * 25)

    result = await auth.login(request, object())

    assert result.code == 422
    get_by_username.assert_not_awaited()
