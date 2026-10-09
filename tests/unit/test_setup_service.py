from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError

import app.api.v1.auth as auth
from app.core.constants import (
    CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    CONTEXT_WINDOW_TOKENS_PER_K,
    DEFAULT_CHAT_MAX_TOKENS,
    ERR_CHANNEL_MODEL_MAX_TOKENS_EXCEEDS_CONTEXT_WINDOW,
    ERR_PASSWORD_TOO_LONG_BYTES,
    ERR_USER_NOT_FOUND_OR_DISABLED,
    ERR_USERNAME_FORMAT,
    ERR_VALIDATION_FAILED,
    MSG_VALIDATION_PASSWORD,
    MSG_VALIDATION_USERNAME,
)
from app.core.exceptions import AuthException
from app.core.i18n import t
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.validation import validate_password, validate_username
from app.handler import validation_exception_handler
from app.models.channel import ModelProtocol
from app.models.user import UserCreate, UserUpdate
from app.schemas.auth import LoginRequest
from app.schemas.setup import SetupCompleteRequest, SetupModelInput

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
    assert set(body) == {"code", "message", "data"}
    assert t(MSG_VALIDATION_PASSWORD) in body["message"]
    assert t(ERR_PASSWORD_TOO_LONG_BYTES) in body["message"]
    assert body["data"] is None


@pytest.mark.asyncio
async def test_validation_exception_handler_translates_field_validation_message() -> None:
    with pytest.raises(ValidationError) as exc_info:
        UserCreate(username="ab", password="valid_password")

    response = await validation_exception_handler(SimpleNamespace(), RequestValidationError(exc_info.value.errors()))
    body = json.loads(response.body)

    assert response.status_code == 422
    assert body["code"] == 422
    assert t(MSG_VALIDATION_USERNAME) in body["message"]
    assert t("string_too_short", min_length=3) in body["message"]
    assert body["message"] != t(ERR_VALIDATION_FAILED)
    assert body["data"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize(
    ("username", "password", "expected_errors"),
    [
        pytest.param(
            "123",
            "123",
            ((MSG_VALIDATION_PASSWORD, "string_too_short", {"min_length": 8}),),
            id="password-too-short",
        ),
        pytest.param(
            "ab",
            "123",
            (
                (MSG_VALIDATION_USERNAME, "string_too_short", {"min_length": 3}),
                (MSG_VALIDATION_PASSWORD, "string_too_short", {"min_length": 8}),
            ),
            id="username-and-password-too-short",
        ),
        pytest.param(
            "bad.name",
            "valid_password",
            ((MSG_VALIDATION_USERNAME, ERR_USERNAME_FORMAT, {}),),
            id="username-format",
        ),
        pytest.param(
            "u" * 51,
            "valid_password",
            ((MSG_VALIDATION_USERNAME, "string_too_long", {"max_length": 50}),),
            id="username-too-long",
        ),
        pytest.param(
            "valid_user",
            "p" * 73,
            ((MSG_VALIDATION_PASSWORD, "string_too_long", {"max_length": 72}),),
            id="password-too-long",
        ),
        pytest.param(
            "ab",
            "中" * 25,
            (
                (MSG_VALIDATION_USERNAME, "string_too_short", {"min_length": 3}),
                (MSG_VALIDATION_PASSWORD, ERR_PASSWORD_TOO_LONG_BYTES, {}),
            ),
            id="username-short-and-password-too-many-bytes",
        ),
    ],
)
async def test_validation_exception_handler_translates_model_errors(
    locale: str,
    username: str,
    password: str,
    expected_errors: tuple[tuple[str, str, dict[str, int]], ...],
) -> None:
    locale_token = set_current_locale(locale)
    try:
        payload = make_setup_request().model_dump(mode="json")
        payload["admin"]["username"] = username
        payload["admin"]["password"] = password

        with pytest.raises(ValidationError) as exc_info:
            SetupCompleteRequest.model_validate(payload)

        response = await validation_exception_handler(SimpleNamespace(), RequestValidationError(exc_info.value.errors()))
        body = json.loads(response.body)
        message = body["message"]
        expected_fields = {field_key for field_key, _, _ in expected_errors}

        assert response.status_code == 422
        assert body["code"] == 422
        assert body["data"] is None
        assert set(body) == {"code", "message", "data"}
        for field_key in (MSG_VALIDATION_USERNAME, MSG_VALIDATION_PASSWORD):
            if field_key in expected_fields:
                assert t(field_key) in message
            else:
                assert t(field_key) not in message
        for field_key, error_key, params in expected_errors:
            assert t(error_key, **params) in message
        assert username not in message
        assert password not in message
        if username == "bad.name":
            assert r"^[a-zA-Z0-9_\-]+$" not in message
    finally:
        reset_current_locale(locale_token)


@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize(
    ("validator", "value", "message_key", "params"),
    [
        pytest.param(validate_password, "123", "string_too_short", {"min_length": 8}, id="short-password"),
        pytest.param(validate_username, "ab", "string_too_short", {"min_length": 3}, id="short-username"),
        pytest.param(validate_username, "u" * 51, "string_too_long", {"max_length": 50}, id="long-username"),
        pytest.param(validate_username, "bad.name", ERR_USERNAME_FORMAT, {}, id="invalid-username"),
    ],
)
def test_shared_validation_messages_are_concrete(
    locale: str,
    validator: Callable[..., str],
    value: str,
    message_key: str,
    params: dict[str, int],
) -> None:
    locale_token = set_current_locale(locale)
    try:
        with pytest.raises(ValueError) as exc_info:
            validator(value)

        message = str(exc_info.value)
        assert message == t(message_key, **params)
        assert "{" not in message
        assert "}" not in message
    finally:
        reset_current_locale(locale_token)


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
    del payload["channel"]["model_ids"][0]["context_window_k"]

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

    assert request.channel.model_ids[0].protocol == protocol


@pytest.mark.parametrize("protocol", [ModelProtocol.OPENAI, ModelProtocol.OPENAI_RESPONSES])
@pytest.mark.parametrize(
    ("context_window_k", "max_tokens", "include_max_tokens"),
    [
        pytest.param(1, 20480, True, id="default-max-tokens"),
        pytest.param(1, 744, True, id="zero-input-budget"),
        pytest.param(1, 745, True, id="negative-input-budget"),
        pytest.param(1, None, False, id="max-tokens-omitted"),
        pytest.param(1, None, True, id="max-tokens-null"),
    ],
)
def test_setup_schema_rejects_chat_model_budget_overflow(
    protocol: ModelProtocol,
    context_window_k: int,
    max_tokens: int | None,
    include_max_tokens: bool,
) -> None:
    payload: dict[str, object] = {
        "model_id": "chat-model",
        "protocol": protocol,
        "context_window_k": context_window_k,
    }
    if include_max_tokens:
        payload["max_tokens"] = max_tokens

    with pytest.raises(ValidationError) as exc_info:
        SetupModelInput.model_validate(payload)

    effective_max_tokens = max_tokens if max_tokens is not None else DEFAULT_CHAT_MAX_TOKENS
    expected_message = t(
        ERR_CHANNEL_MODEL_MAX_TOKENS_EXCEEDS_CONTEXT_WINDOW,
        max_tokens=effective_max_tokens,
        context_window_k=context_window_k,
        context_window_tokens=context_window_k * CONTEXT_WINDOW_TOKENS_PER_K,
        safety_margin_tokens=CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    )
    assert expected_message in str(exc_info.value)


@pytest.mark.parametrize("protocol", [ModelProtocol.OPENAI, ModelProtocol.OPENAI_RESPONSES])
@pytest.mark.parametrize(
    ("context_window_k", "max_tokens", "include_max_tokens"),
    [
        pytest.param(1, 743, True, id="one-token-input-budget"),
        pytest.param(1, 0, True, id="zero-max-tokens"),
        pytest.param(64, None, False, id="max-tokens-omitted"),
        pytest.param(64, None, True, id="max-tokens-null"),
        pytest.param(64, 20480, True, id="explicit-default-max-tokens"),
    ],
)
def test_setup_schema_accepts_chat_model_budget_boundaries(
    protocol: ModelProtocol,
    context_window_k: int,
    max_tokens: int | None,
    include_max_tokens: bool,
) -> None:
    payload: dict[str, object] = {
        "model_id": "chat-model",
        "protocol": protocol,
        "context_window_k": context_window_k,
    }
    if include_max_tokens:
        payload["max_tokens"] = max_tokens

    model = SetupModelInput.model_validate(payload)

    assert model.protocol == protocol
    assert model.context_window_k == context_window_k
    assert model.max_tokens == max_tokens


def test_setup_schema_accepts_independent_model_configurations() -> None:
    payload = make_setup_request().model_dump(mode="json")
    payload["channel"].update(
        {
            "model_id": "legacy-model",
            "protocol": ModelProtocol.OPENAI.value,
            "context_window_k": 32,
            "model_ids": [
                {
                    "model_id": "chat-model",
                    "protocol": ModelProtocol.OPENAI.value,
                    "context_window_k": 64,
                    "image_understanding": True,
                },
                {
                    "model_id": "responses-model",
                    "protocol": ModelProtocol.OPENAI_RESPONSES.value,
                    "context_window_k": 128,
                    "temperature": 0.7,
                },
            ],
        }
    )

    request = SetupCompleteRequest.model_validate(payload)

    assert request.channel.model_ids[0].model_id == "chat-model"
    assert request.channel.model_ids[0].protocol == ModelProtocol.OPENAI
    assert request.channel.model_ids[0].context_window_k == 64
    assert request.channel.model_ids[0].image_understanding is True
    assert request.channel.model_ids[1].model_id == "responses-model"
    assert request.channel.model_ids[1].protocol == ModelProtocol.OPENAI_RESPONSES
    assert request.channel.model_ids[1].context_window_k == 128
    assert request.channel.model_ids[1].temperature == 0.7


@pytest.mark.parametrize("model_ids", [[], None], ids=["empty-list", "null"])
def test_setup_schema_rejects_empty_model_list_even_with_legacy_fields(
    model_ids: list[dict[str, object]] | None,
) -> None:
    payload = make_setup_request().model_dump(mode="json")
    payload["channel"].update(
        {
            "model_id": "legacy-model",
            "protocol": ModelProtocol.OPENAI.value,
            "context_window_k": 64,
            "model_ids": model_ids,
        }
    )

    with pytest.raises(ValidationError):
        SetupCompleteRequest.model_validate(payload)


def test_setup_schema_rejects_duplicate_model_ids_after_normalization() -> None:
    payload = make_setup_request().model_dump(mode="json")
    payload["channel"]["model_ids"] = [
        {
            "model_id": "shared-model",
            "protocol": ModelProtocol.OPENAI.value,
            "context_window_k": 64,
        },
        {
            "model_id": " shared-model ",
            "protocol": ModelProtocol.OPENAI_RESPONSES.value,
            "context_window_k": 128,
        },
    ]

    with pytest.raises(ValidationError):
        SetupCompleteRequest.model_validate(payload)


def test_setup_schema_rejects_blank_second_model_id() -> None:
    payload = make_setup_request().model_dump(mode="json")
    payload["channel"]["model_ids"] = [
        {
            "model_id": "chat-model",
            "protocol": ModelProtocol.OPENAI.value,
            "context_window_k": 64,
        },
        {
            "model_id": "   ",
            "protocol": ModelProtocol.OPENAI_RESPONSES.value,
            "context_window_k": 128,
        },
    ]

    with pytest.raises(ValidationError):
        SetupCompleteRequest.model_validate(payload)


def test_setup_schema_rejects_non_chat_protocol_in_second_model() -> None:
    payload = make_setup_request().model_dump(mode="json")
    payload["channel"]["model_ids"] = [
        {
            "model_id": "chat-model",
            "protocol": ModelProtocol.OPENAI.value,
            "context_window_k": 64,
        },
        {
            "model_id": "embedding-model",
            "protocol": ModelProtocol.OPENAI_EMBEDDING.value,
            "context_window_k": 128,
        },
    ]

    with pytest.raises(ValidationError):
        SetupCompleteRequest.model_validate(payload)


def test_setup_schema_rejects_missing_context_window_in_second_model() -> None:
    payload = make_setup_request().model_dump(mode="json")
    payload["channel"]["model_ids"] = [
        {
            "model_id": "chat-model",
            "protocol": ModelProtocol.OPENAI.value,
            "context_window_k": 64,
        },
        {
            "model_id": "responses-model",
            "protocol": ModelProtocol.OPENAI_RESPONSES.value,
        },
    ]

    with pytest.raises(ValidationError):
        SetupCompleteRequest.model_validate(payload)


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
