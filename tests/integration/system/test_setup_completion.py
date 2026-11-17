from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import app.api.v1.setup as setup_api
import app.core.setup.service as setup_service
from app.core.constants import (
    CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
    CONTEXT_WINDOW_TOKENS_PER_K,
    ERR_CHANNEL_MODEL_MAX_TOKENS_EXCEEDS_CONTEXT_WINDOW,
    ERR_CHANNEL_NAME_EXISTS,
    ERR_SETUP_ALREADY_COMPLETED,
    ERR_SETUP_CONFLICT,
    ERR_SETUP_NOT_ALLOWED,
    ERR_SETUP_STATE_UPDATE_FAILED,
    ERR_SETUP_STATUS_INVALID,
    ERR_SETUP_STATUS_NOT_INITIALIZED,
    ERR_SYSTEM_SECRETS_FILE_INVALID,
    ERR_USER_NAME_EXISTS,
    SETUP_ADMIN_UID_KEY,
    SETUP_SESSION_COOKIE_NAME,
    SETUP_SESSION_RECORD_KEY,
    SETUP_STATUS_COMPLETED,
    SETUP_STATUS_CONFIGURING,
    SETUP_STATUS_KEY,
    SETUP_STATUS_PENDING,
)
from app.core.i18n import t
from app.core.i18n.context import reset_current_locale, set_current_locale
from app.core.security import verify_password
from app.core.system_secrets import SystemSecrets, SystemSecretsError
from app.models.channel import ENCRYPTED_API_KEY_PREFIX, ModelChannel, ModelProtocol, ModelUsage
from app.models.user import User
from tests.integration.system import test_setup_api as setup_api_tests
from tests.integration.system.test_setup_api import (
    FIXED_ACCESS_TOKEN,
    FIXED_JWT_SECRET,
    TEST_API_KEY,
    TEST_MODEL_ID,
    TEST_PASSWORD,
    TEST_USERNAME,
    _assert_standard_response,
    _business_record_counts,
    _read_setup_data,
    _request,
    _set_setup_status,
    _setup_cookie_value,
    _setup_payload,
)

setup_app = setup_api_tests.setup_app


@pytest.mark.asyncio
async def test_setup_complete_persists_multiple_models_and_binds_profile_to_first_model(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    first_model_id = "setup-first-chat-model"
    second_model_id = "setup-second-responses-model"
    payload = {
        "admin": {"username": TEST_USERNAME, "password": TEST_PASSWORD},
        "channel": {
            "name": "setup-multi-channel",
            "base_url": "https://api.example.test/v1",
            "api_key": "setup-multi-api-key",
            "http_proxy": "http://proxy.example.test:8080",
            "model_ids": [
                {
                    "model_id": first_model_id,
                    "protocol": "OPENAI",
                    "image_understanding": True,
                    "audio_understanding": False,
                    "video_understanding": True,
                    "context_window_k": 64,
                    "temperature": 0.2,
                    "top_p": 0.7,
                    "reasoning_efforts": ["low", "high"],
                    "max_tokens": 512,
                    "description": "First chat model",
                    "advanced_settings": {"custom_headers": {"X-Setup-First": "first"}},
                },
                {
                    "model_id": second_model_id,
                    "protocol": "OPENAI_RESPONSES",
                    "image_understanding": False,
                    "audio_understanding": True,
                    "video_understanding": False,
                    "context_window_k": 128,
                    "temperature": 0.8,
                    "top_p": 0.95,
                    "reasoning_efforts": ["medium"],
                    "max_tokens": 1024,
                    "description": "Second responses model",
                    "advanced_settings": {"custom_headers": {"X-Setup-Second": "second"}},
                },
            ],
        },
        "profile": {"name": "setup-multi-profile"},
    }

    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=payload)
    body = _assert_standard_response(response, 200)
    database = await _read_setup_data(setup_session_factory)

    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
    assert len(database["users"]) == 1
    assert len(database["channels"]) == 1
    assert len(database["prompts"]) == 1
    assert len(database["profiles"]) == 1

    channel = database["channels"][0]
    assert body["data"]["channel_id"] == channel.id
    assert channel.api_key.startswith(ENCRYPTED_API_KEY_PREFIX)
    assert channel.api_key != "setup-multi-api-key"
    assert channel.get_decrypted_api_key() == "setup-multi-api-key"
    assert channel.http_proxy == "http://proxy.example.test:8080"
    assert len(channel.model_ids) == 2

    models = {model["model_id"]: model for model in channel.model_ids}
    assert set(models) == {first_model_id, second_model_id}
    first_model = models[first_model_id]
    assert first_model["usage"] == ModelUsage.CHAT.value
    assert first_model["protocol"] == ModelProtocol.OPENAI.value
    assert first_model["image_understanding"] is True
    assert first_model["audio_understanding"] is False
    assert first_model["video_understanding"] is True
    assert first_model["context_window_k"] == 64
    assert first_model["temperature"] == 0.2
    assert first_model["top_p"] == 0.7
    assert first_model["reasoning_efforts"] == ["low", "high"]
    assert first_model["max_tokens"] == 512
    assert first_model["description"] == "First chat model"
    assert first_model["advanced_settings"] == {"custom_headers": {"x-setup-first": "first"}}

    second_model = models[second_model_id]
    assert second_model["usage"] == ModelUsage.CHAT.value
    assert second_model["protocol"] == ModelProtocol.OPENAI_RESPONSES.value
    assert second_model["image_understanding"] is False
    assert second_model["audio_understanding"] is True
    assert second_model["video_understanding"] is False
    assert second_model["context_window_k"] == 128
    assert second_model["temperature"] == 0.8
    assert second_model["top_p"] == 0.95
    assert second_model["reasoning_efforts"] == ["medium"]
    assert second_model["max_tokens"] == 1024
    assert second_model["description"] == "Second responses model"
    assert second_model["advanced_settings"] == {"custom_headers": {"x-setup-second": "second"}}

    profile = database["profiles"][0]
    assert profile.is_default is True
    assert profile.prompt_id == database["prompts"][0].id
    assert body["data"]["profile_id"] == profile.id
    for channel_name in ("chat_channel", "subagent_channel", "context_summary_channel"):
        rules = profile.configs["channel"][channel_name]["rules"]
        assert len(rules) == 1
        assert rules[0]["channel_id"] == channel.id
        assert rules[0]["model_id"] == first_model_id


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "invalid_model_ids",
    [
        [],
        [
            {"model_id": " model-a ", "protocol": "OPENAI", "context_window_k": 64},
            {"model_id": "model-a", "protocol": "OPENAI", "context_window_k": 64},
        ],
        [
            {"model_id": "first-model", "protocol": "OPENAI", "context_window_k": 64},
            {"model_id": "", "protocol": "OPENAI", "context_window_k": 64},
        ],
        [
            {"model_id": "first-model", "protocol": "OPENAI", "context_window_k": 64},
            {"model_id": "second-model", "protocol": "INVALID", "context_window_k": 64},
        ],
        [
            {"model_id": "first-model", "protocol": "OPENAI", "context_window_k": 64},
            {"model_id": "second-model", "protocol": "OPENAI"},
        ],
    ],
)
async def test_setup_complete_rejects_invalid_model_arrays_without_creating_records(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    invalid_model_ids: list[dict[str, Any]],
) -> None:
    payload = _setup_payload()
    payload["channel"] = {
        "name": "setup-invalid-models-channel",
        "base_url": "https://api.example.test/v1",
        "api_key": "setup-invalid-models-api-key",
        "http_proxy": None,
        "model_ids": invalid_model_ids,
    }

    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=payload)
    _assert_standard_response(response, 422)

    database = await _read_setup_data(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
    assert _business_record_counts(database) == {"users": 0, "channels": 0, "prompts": 0, "profiles": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize(
    ("model_style", "context_window_k", "max_tokens", "include_max_tokens", "model_index", "expected_max_tokens"),
    [
        pytest.param("flat", 1, 20480, True, 0, 20480, id="flat-context1-max20480"),
        pytest.param("flat", 1, 744, True, 0, 744, id="flat-context1-max744"),
        pytest.param("flat", 1, 745, True, 0, 745, id="flat-context1-max745"),
        pytest.param("flat", 1, None, False, 0, 20480, id="flat-context1-max-omitted"),
        pytest.param("flat", 1, None, True, 0, 20480, id="flat-context1-max-none"),
        pytest.param("list", 12, 20480, True, 1, 20480, id="list-context12-second-max20480"),
    ],
)
async def test_setup_complete_rejects_model_token_budget_without_writes(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    locale: str,
    model_style: str,
    context_window_k: int,
    max_tokens: int | None,
    include_max_tokens: bool,
    model_index: int,
    expected_max_tokens: int,
) -> None:
    locale_token = set_current_locale(locale)
    try:
        payload = _setup_payload(channel_name="setup-budget-channel", api_key="setup-budget-api-key")
        if model_style == "flat":
            payload["channel"]["context_window_k"] = context_window_k
            if include_max_tokens:
                payload["channel"]["max_tokens"] = max_tokens
        else:
            second_model: dict[str, Any] = {
                "model_id": "setup-budget-invalid-model",
                "protocol": "OPENAI",
                "context_window_k": context_window_k,
            }
            if include_max_tokens:
                second_model["max_tokens"] = max_tokens
            payload["channel"] = {
                "name": "setup-budget-channel",
                "base_url": "https://api.example.test/v1",
                "api_key": "setup-budget-api-key",
                "model_ids": [
                    {
                        "model_id": "setup-budget-first-model",
                        "protocol": "OPENAI",
                        "context_window_k": 64,
                        "max_tokens": 1024,
                    },
                    second_model,
                ],
            }

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=setup_app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            status_response = await client.get("/api/v1/setup/status")
            _assert_standard_response(status_response, 200)
            setup_token = _setup_cookie_value(status_response)
            assert client.cookies.get(SETUP_SESSION_COOKIE_NAME) == setup_token
            before = await _read_setup_data(setup_session_factory)

            response = await client.post("/api/v1/setup/complete", json=payload)

        body = _assert_standard_response(response, 422)
        assert body["data"] is None
        expected_reason = t(
            ERR_CHANNEL_MODEL_MAX_TOKENS_EXCEEDS_CONTEXT_WINDOW,
            max_tokens=expected_max_tokens,
            safety_margin_tokens=CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
            context_window_k=context_window_k,
            context_window_tokens=context_window_k * CONTEXT_WINDOW_TOKENS_PER_K,
        )
        assert f"channel.model_ids.{model_index}" in body["message"]
        assert expected_reason in body["message"]
        response_text = response.text.lower()
        assert TEST_PASSWORD not in response.text
        assert "setup-budget-api-key" not in response.text
        assert "value error" not in response_text
        assert "value_error" not in response_text
        assert "pydantic" not in response_text
        assert "input should" not in response_text

        after = await _read_setup_data(setup_session_factory)
        assert after["settings"] == before["settings"]
        assert after["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
        assert _business_record_counts(after) == _business_record_counts(before)
    finally:
        reset_current_locale(locale_token)


@pytest.mark.asyncio
@pytest.mark.parametrize("locale", ["zh", "en"])
@pytest.mark.parametrize("model_style", ["flat", "list"])
async def test_setup_complete_can_retry_invalid_model_token_budget_with_same_cookie(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    locale: str,
    model_style: str,
) -> None:
    locale_token = set_current_locale(locale)
    try:
        first_model_id = TEST_MODEL_ID if model_style == "flat" else "setup-retry-first-model"
        second_model_id = "setup-retry-second-model"
        payload = _setup_payload(channel_name="setup-retry-channel", api_key="setup-retry-api-key")
        if model_style == "flat":
            payload["channel"].update({"context_window_k": 1, "max_tokens": 744})
            model_index = 0
        else:
            payload["channel"] = {
                "name": "setup-retry-channel",
                "base_url": "https://api.example.test/v1",
                "api_key": "setup-retry-api-key",
                "model_ids": [
                    {
                        "model_id": first_model_id,
                        "protocol": "OPENAI",
                        "context_window_k": 64,
                        "max_tokens": 1024,
                    },
                    {
                        "model_id": second_model_id,
                        "protocol": "OPENAI",
                        "context_window_k": 1,
                        "max_tokens": 744,
                    },
                ],
            }
            model_index = 1

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=setup_app, raise_app_exceptions=False),
            base_url="http://test",
        ) as client:
            status_response = await client.get("/api/v1/setup/status")
            _assert_standard_response(status_response, 200)
            setup_token = _setup_cookie_value(status_response)
            assert client.cookies.get(SETUP_SESSION_COOKIE_NAME) == setup_token
            before = await _read_setup_data(setup_session_factory)

            failed_response = await client.post("/api/v1/setup/complete", json=payload)
            failed_body = _assert_standard_response(failed_response, 422)
            assert failed_body["data"] is None
            expected_reason = t(
                ERR_CHANNEL_MODEL_MAX_TOKENS_EXCEEDS_CONTEXT_WINDOW,
                max_tokens=744,
                safety_margin_tokens=CONTEXT_REQUEST_SAFETY_MARGIN_TOKENS,
                context_window_k=1,
                context_window_tokens=CONTEXT_WINDOW_TOKENS_PER_K,
            )
            assert f"channel.model_ids.{model_index}" in failed_body["message"]
            assert expected_reason in failed_body["message"]
            failed_response_text = failed_response.text.lower()
            assert TEST_PASSWORD not in failed_response.text
            assert "setup-retry-api-key" not in failed_response.text
            assert "value error" not in failed_response_text
            assert "value_error" not in failed_response_text
            assert "pydantic" not in failed_response_text
            assert "input should" not in failed_response_text

            failed_database = await _read_setup_data(setup_session_factory)
            assert failed_database["settings"] == before["settings"]
            assert failed_database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
            assert _business_record_counts(failed_database) == _business_record_counts(before)

            if model_style == "flat":
                payload["channel"]["max_tokens"] = 743
            else:
                payload["channel"]["model_ids"][1]["max_tokens"] = 743

            retry_response = await client.post("/api/v1/setup/complete", json=payload)
            _assert_standard_response(retry_response, 200)

        database = await _read_setup_data(setup_session_factory)
        assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
        assert _business_record_counts(database) == {"users": 1, "channels": 1, "prompts": 1, "profiles": 1}

        channel = database["channels"][0]
        models = {model["model_id"]: model for model in channel.model_ids}
        assert models[first_model_id]["model_id"] == first_model_id
        if model_style == "list":
            assert models[first_model_id]["context_window_k"] == 64
            assert models[first_model_id]["max_tokens"] == 1024
        assert models[second_model_id if model_style == "list" else first_model_id]["max_tokens"] == 743

        profile = database["profiles"][0]
        for channel_name in ("chat_channel", "subagent_channel", "context_summary_channel"):
            rules = profile.configs["channel"][channel_name]["rules"]
            assert len(rules) == 1
            assert rules[0]["channel_id"] == channel.id
            assert rules[0]["model_id"] == first_model_id
    finally:
        reset_current_locale(locale_token)


@pytest.mark.asyncio
async def test_setup_complete_returns_token_data_and_creates_initial_records(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    payload = _setup_payload()
    payload["channel"].update(
        {
            "http_proxy": "http://user%40name:password%3Awith%2Fslash@PROXY.EXAMPLE.TEST:8080/",
            "image_understanding": True,
            "audio_understanding": True,
            "video_understanding": True,
            "context_window_k": 128,
            "temperature": 0.35,
            "top_p": 0.85,
            "reasoning_effort": "high",
            "max_tokens": 2048,
            "description": "Full chat setup model",
            "advanced_settings": {"custom_headers": {"X-Setup-Trace": "setup-test"}},
        }
    )
    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=payload)
    body = _assert_standard_response(response, 200)
    clear_cookie = response.headers["set-cookie"]
    clear_cookie_lower = clear_cookie.lower()
    assert f'{SETUP_SESSION_COOKIE_NAME}=""' in clear_cookie
    assert "; max-age=0" in clear_cookie_lower
    assert "; path=/api/v1/setup" in clear_cookie_lower
    assert "; httponly" in clear_cookie_lower
    assert "samesite=strict" in clear_cookie_lower

    assert set(body["data"]) == {"access_token", "token_type", "profile_id", "channel_id"}
    assert body["data"]["access_token"] == FIXED_ACCESS_TOKEN
    assert body["data"]["token_type"] == "bearer"
    assert TEST_PASSWORD not in response.text
    assert TEST_API_KEY not in response.text
    assert FIXED_JWT_SECRET not in response.text
    assert "jwt_secret_key" not in response.text
    assert "channel_encryption_key" not in response.text

    database = await _read_setup_data(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
    assert SETUP_SESSION_RECORD_KEY not in database["settings"]

    users = database["users"]
    assert len(users) == 1
    assert sum(user.is_superuser for user in users) == 1
    admin = users[0]
    assert database["settings"][SETUP_ADMIN_UID_KEY] == admin.uid
    assert admin.username == TEST_USERNAME
    assert admin.is_superuser is True
    assert admin.is_active is True
    assert admin.hashed_password != TEST_PASSWORD
    assert verify_password(TEST_PASSWORD, admin.hashed_password)

    channels = database["channels"]
    assert len(channels) == 1
    channel = channels[0]
    assert channel.api_key.startswith(ENCRYPTED_API_KEY_PREFIX)
    assert channel.api_key != TEST_API_KEY
    assert channel.get_decrypted_api_key() == TEST_API_KEY
    assert body["data"]["channel_id"] == channel.id
    assert channel.http_proxy == "http://user%40name:password%3Awith%2Fslash@proxy.example.test:8080"
    assert len(channel.model_ids) == 1
    model = channel.model_ids[0]
    assert model["model_id"] == TEST_MODEL_ID
    assert model["usage"] == ModelUsage.CHAT.value
    assert model["protocol"] == ModelProtocol.OPENAI.value
    assert model["image_understanding"] is True
    assert model["audio_understanding"] is True
    assert model["video_understanding"] is True
    assert model["context_window_k"] == 128
    assert model["temperature"] == 0.35
    assert model["top_p"] == 0.85
    assert model["reasoning_effort"] == "high"
    assert model["max_tokens"] == 2048
    assert model["description"] == "Full chat setup model"
    assert model["advanced_settings"] == {"custom_headers": {"x-setup-trace": "setup-test"}}

    prompts = database["prompts"]
    assert len(prompts) == 1
    assert prompts[0].name == "default"

    profiles = database["profiles"]
    assert len(profiles) == 1
    assert profiles[0].uid == users[0].uid
    assert profiles[0].prompt_id == prompts[0].id
    assert profiles[0].is_default is True
    assert body["data"]["profile_id"] == profiles[0].id
    for channel_name in ("chat_channel", "subagent_channel", "context_summary_channel"):
        rule = profiles[0].configs["channel"][channel_name]["rules"][0]
        assert rule["channel_id"] == channel.id
        assert rule["model_id"] == TEST_MODEL_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conflict", "expected_error"),
    [
        ("username", ERR_USER_NAME_EXISTS),
        ("channel", ERR_CHANNEL_NAME_EXISTS),
    ],
)
async def test_setup_complete_rejects_existing_names_and_rolls_back(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    conflict: str,
    expected_error: str,
) -> None:
    async with setup_session_factory() as session:
        if conflict == "username":
            session.add(User(uid="existing-user", username=TEST_USERNAME, hashed_password="existing-hash"))
        else:
            session.add(ModelChannel(name="setup-channel", api_key="existing-key", base_url="https://existing.example.test/v1", model_ids=[]))
        await session.commit()

    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=_setup_payload())
    body = _assert_standard_response(response, 400)
    assert body["message"] == t(expected_error)
    assert body["data"] is None
    database = await _read_setup_data(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
    assert len(database["users"]) == int(conflict == "username")
    assert not any(user.is_superuser for user in database["users"])
    assert len(database["channels"]) == int(conflict == "channel")
    assert database["prompts"] == []
    assert database["profiles"] == []


@pytest.mark.asyncio
async def test_setup_complete_rolls_back_failed_creation_and_same_session_can_retry(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_create = setup_service.profile_crud.create

    async def fail_profile_create(_db: AsyncSession, **_kwargs: Any) -> Any:
        raise RuntimeError("simulated profile creation failure")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup_app, raise_app_exceptions=False), base_url="http://test") as client:
        status_response = await client.get("/api/v1/setup/status")
        _assert_standard_response(status_response, 200)
        monkeypatch.setattr(setup_service.profile_crud, "create", fail_profile_create)
        failed_response = await client.post("/api/v1/setup/complete", json=_setup_payload())
        assert _assert_standard_response(failed_response, 500)["data"] is None
        failed_database = await _read_setup_data(setup_session_factory)
        assert failed_database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
        assert _business_record_counts(failed_database) == {"users": 0, "channels": 0, "prompts": 0, "profiles": 0}
        monkeypatch.setattr(setup_service.profile_crud, "create", original_create)
        retry_response = await client.post("/api/v1/setup/complete", json=_setup_payload())
        _assert_standard_response(retry_response, 200)

    database = await _read_setup_data(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
    assert _business_record_counts(database) == {"users": 1, "channels": 1, "prompts": 1, "profiles": 1}


@pytest.mark.asyncio
async def test_setup_complete_rolls_back_when_admin_binding_update_fails(
    setup_app: FastAPI,
    setup_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fail_admin_binding(_db: AsyncSession, *, admin_uid: str) -> bool:
        assert admin_uid
        return False

    monkeypatch.setattr(setup_service.system_setting_crud, "set_setup_admin_uid", fail_admin_binding)
    response = await _request(setup_app, "POST", "/api/v1/setup/complete", json=_setup_payload())
    body = _assert_standard_response(response, 500)
    assert body["message"] == t(ERR_SETUP_STATE_UPDATE_FAILED)
    assert body["data"] is None
    database = await _read_setup_data(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_PENDING
    assert _business_record_counts(database) == {"users": 0, "channels": 0, "prompts": 0, "profiles": 0}


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
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=setup_app), base_url="http://test") as client:
        session_response = await client.get("/api/v1/setup/status")
        _assert_standard_response(session_response, 200)
        session_token = _setup_cookie_value(session_response)
        assert client.cookies.get(SETUP_SESSION_COOKIE_NAME) == session_token

        original_get_valid_setup_status = setup_api._get_valid_setup_status
        status_barrier = asyncio.Barrier(2)

        async def synchronized_get_valid_setup_status(db: AsyncSession) -> str:
            status = await original_get_valid_setup_status(db)
            assert status == SETUP_STATUS_PENDING
            await status_barrier.wait()
            return status

        monkeypatch.setattr(setup_api, "_get_valid_setup_status", synchronized_get_valid_setup_status)

        async def submit_setup() -> httpx.Response:
            return await client.post(
                "/api/v1/setup/complete",
                json=_setup_payload(),
            )

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
