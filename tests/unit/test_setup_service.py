from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from fastapi.exceptions import RequestValidationError
from pydantic import ValidationError
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

import app.api.v1.auth as auth
import app.core.crypto as crypto
import app.core.setup as setup_service
from app.core.constants import (
    ERR_CHANNEL_NAME_EXISTS,
    ERR_PASSWORD_TOO_LONG_BYTES,
    ERR_SETUP_CONFLICT,
    ERR_SETUP_STATE_UPDATE_FAILED,
    ERR_USER_NAME_EXISTS,
    ERR_USER_NOT_FOUND_OR_DISABLED,
    ERR_VALIDATION_FAILED,
    SETUP_ADMIN_UID_KEY,
    SETUP_STATUS_COMPLETED,
    SETUP_STATUS_KEY,
    SETUP_STATUS_PENDING,
)
from app.core.exceptions import AuthException, ParameterException, ServerException
from app.core.i18n import t
from app.core.security import verify_password
from app.handler import validation_exception_handler
from app.models.channel import ENCRYPTED_API_KEY_PREFIX, ChannelModelItem, ModelChannel, ModelProtocol, ModelUsage
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.system_setting import SystemSetting
from app.models.user import User, UserCreate, UserUpdate
from app.schemas.auth import LoginRequest
from app.schemas.setup import SetupCompleteRequest

FIXED_ACCESS_TOKEN = "setup-test-access-token"
TEST_USERNAME = "setup_admin"
TEST_PASSWORD = "correct-password-123"
TEST_API_KEY = "setup-api-key"

SETUP_TABLES = [
    SystemSetting.__table__,
    User.__table__,
    ModelChannel.__table__,
    PromptLibrary.__table__,
    Profile.__table__,
]


class CountingAsyncSession(AsyncSession):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1
        await super().commit()


@pytest_asyncio.fixture
async def setup_session_factory(tmp_path: Path) -> AsyncIterator[async_sessionmaker[CountingAsyncSession]]:
    database_path = tmp_path / "setup-service.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=SETUP_TABLES,
            )
        )

    session_factory = async_sessionmaker(
        engine,
        class_=CountingAsyncSession,
        expire_on_commit=False,
    )
    async with session_factory() as session:
        session.add_all(
            [
                SystemSetting(key=SETUP_STATUS_KEY, value=SETUP_STATUS_PENDING),
                SystemSetting(key=SETUP_ADMIN_UID_KEY, value=""),
            ]
        )
        await session.commit()

    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def fixed_setup_dependencies(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(crypto, "get_channel_encryption_key", lambda: b"\x11" * 32)
    monkeypatch.setattr(setup_service, "create_access_token", lambda _data: FIXED_ACCESS_TOKEN)


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
                "context_window_k": 4,
            },
            "profile": {"name": profile_name},
        }
    )


async def read_database(session_factory: async_sessionmaker[CountingAsyncSession]) -> dict[str, object]:
    async with session_factory() as session:
        settings_result = await session.execute(select(SystemSetting))
        users_result = await session.execute(select(User))
        channels_result = await session.execute(select(ModelChannel))
        prompts_result = await session.execute(select(PromptLibrary))
        profiles_result = await session.execute(select(Profile))
        return {
            "settings": {item.key: item.value for item in settings_result.scalars().all()},
            "users": list(users_result.scalars().all()),
            "channels": list(channels_result.scalars().all()),
            "prompts": list(prompts_result.scalars().all()),
            "profiles": list(profiles_result.scalars().all()),
        }


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
async def test_complete_setup_creates_consistent_initial_data_and_commits_once(
    setup_session_factory: async_sessionmaker[CountingAsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    request = make_setup_request()
    token_commit_counts: list[int] = []

    async with setup_session_factory() as session:

        def issue_token(payload: dict) -> str:
            token_commit_counts.append(session.commit_count)
            assert payload == {"sub": request.admin.username}
            return FIXED_ACCESS_TOKEN

        monkeypatch.setattr(setup_service, "create_access_token", issue_token)
        result = await setup_service.complete_setup(session, request)

        assert session.commit_count == 1

    assert token_commit_counts == [1]
    assert set(result.model_dump()) == {"access_token", "token_type", "profile_id", "channel_id"}
    assert result.access_token == FIXED_ACCESS_TOKEN
    assert result.token_type == "bearer"
    assert isinstance(result.profile_id, int) and result.profile_id > 0
    assert isinstance(result.channel_id, int) and result.channel_id > 0
    assert not {"redirect", "password", "api_key"}.intersection(result.model_dump())

    database = await read_database(setup_session_factory)
    settings = database["settings"]
    users = database["users"]
    channels = database["channels"]
    prompts = database["prompts"]
    profiles = database["profiles"]

    assert settings[SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
    assert len(users) == 1
    assert sum(user.is_superuser for user in users) == 1
    admin = users[0]
    assert settings[SETUP_ADMIN_UID_KEY] == admin.uid
    assert admin.is_superuser is True
    assert admin.is_active is True
    assert admin.hashed_password != TEST_PASSWORD
    assert admin.hashed_password.startswith("$2")
    assert verify_password(TEST_PASSWORD, admin.hashed_password)

    assert len(channels) == 1
    channel = channels[0]
    assert result.channel_id == channel.id
    assert channel.api_key.startswith(ENCRYPTED_API_KEY_PREFIX)
    assert channel.get_decrypted_api_key() == TEST_API_KEY
    assert channel.api_key != TEST_API_KEY
    assert len(channel.model_ids) == 1
    model = channel.model_ids[0]
    assert model["model_id"] == request.channel.model_id
    assert model["usage"] == ModelUsage.CHAT.value
    assert model["protocol"] == request.channel.protocol.value

    assert len(prompts) == 1
    prompt = prompts[0]
    assert prompt.name == "default"
    assert prompt.uid is None

    assert len(profiles) == 1
    profile = profiles[0]
    assert result.profile_id == profile.id
    assert profile.uid == admin.uid
    assert profile.prompt_id == prompt.id
    assert profile.is_default is True
    for channel_name in ("chat_channel", "context_summary_channel"):
        rule = profile.configs["channel"][channel_name]["rules"][0]
        assert rule["channel_id"] == channel.id
        assert rule["model_id"] == request.channel.model_id


@pytest.mark.asyncio
async def test_complete_setup_persists_full_chat_model_configuration(
    setup_session_factory: async_sessionmaker[CountingAsyncSession],
) -> None:
    normalized_proxy = "http://user%40name:password%3Awith%2Fslash@proxy.example.test:8080"
    request = SetupCompleteRequest.model_validate(
        {
            "admin": {"username": "full_chat_admin", "password": TEST_PASSWORD},
            "channel": {
                "name": "full-chat-channel",
                "base_url": "https://api.example.test/v1",
                "api_key": TEST_API_KEY,
                "http_proxy": "http://user%40name:password%3Awith%2Fslash@PROXY.EXAMPLE.TEST:8080/",
                "model_id": "full-chat-model",
                "protocol": ModelProtocol.OPENAI,
                "image_understanding": True,
                "audio_understanding": True,
                "video_understanding": True,
                "context_window_k": 128,
                "temperature": 0.35,
                "top_p": 0.85,
                "max_tokens": 2048,
                "description": "Full chat setup model",
                "advanced_settings": {"custom_headers": {"X-Setup-Trace": "setup-test"}},
            },
            "profile": {"name": "full-chat-profile"},
        }
    )

    assert request.channel.http_proxy == normalized_proxy

    async with setup_session_factory() as session:
        await setup_service.complete_setup(session, request)

    database = await read_database(setup_session_factory)
    channels = database["channels"]
    assert len(channels) == 1
    channel = channels[0]
    assert channel.http_proxy == normalized_proxy
    assert len(channel.model_ids) == 1

    model = channel.model_ids[0]
    assert model["model_id"] == "full-chat-model"
    assert model["usage"] == ModelUsage.CHAT.value
    assert model["protocol"] == ModelProtocol.OPENAI.value
    assert model["image_understanding"] is True
    assert model["audio_understanding"] is True
    assert model["video_understanding"] is True
    assert model["context_window_k"] == 128
    assert model["temperature"] == 0.35
    assert model["top_p"] == 0.85
    assert model["max_tokens"] == 2048
    assert model["description"] == "Full chat setup model"
    assert model["advanced_settings"] == {"custom_headers": {"x-setup-trace": "setup-test"}}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("conflict", "expected_error"),
    [
        ("username", ERR_USER_NAME_EXISTS),
        ("channel", ERR_CHANNEL_NAME_EXISTS),
    ],
)
async def test_complete_setup_rejects_existing_names_and_rolls_back(
    setup_session_factory: async_sessionmaker[CountingAsyncSession],
    conflict: str,
    expected_error: str,
) -> None:
    request = make_setup_request()
    async with setup_session_factory() as session:
        if conflict == "username":
            session.add(
                User(
                    uid="existing-user",
                    username=request.admin.username,
                    hashed_password="existing-hash",
                )
            )
        else:
            session.add(
                ModelChannel(
                    name=request.channel.name,
                    api_key=TEST_API_KEY,
                    base_url=request.channel.base_url,
                    model_ids=[
                        ChannelModelItem(
                            model_id=request.channel.model_id,
                            usage=ModelUsage.CHAT,
                            protocol=request.channel.protocol,
                            context_window_k=request.channel.context_window_k,
                        ).model_dump(mode="json")
                    ],
                )
            )
        await session.commit()

    async with setup_session_factory() as session:
        with pytest.raises(ParameterException) as exc_info:
            await setup_service.complete_setup(session, request)

    assert exc_info.value.code == 400
    assert exc_info.value.message == expected_error

    database = await read_database(setup_session_factory)
    assert database["settings"] == {
        SETUP_STATUS_KEY: SETUP_STATUS_PENDING,
        SETUP_ADMIN_UID_KEY: "",
    }
    assert len(database["users"]) == int(conflict == "username")
    assert not any(user.is_superuser for user in database["users"])
    assert len(database["channels"]) == int(conflict == "channel")
    assert database["prompts"] == []
    assert database["profiles"] == []


@pytest.mark.asyncio
async def test_complete_setup_rolls_back_all_rows_and_can_retry(
    setup_session_factory: async_sessionmaker[CountingAsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_create = setup_service.profile_crud.create

    async def fail_profile_create(_db: AsyncSession, **_kwargs):
        raise RuntimeError("simulated profile creation failure")

    monkeypatch.setattr(setup_service.profile_crud, "create", fail_profile_create)
    async with setup_session_factory() as session:
        with pytest.raises(RuntimeError, match="simulated profile creation failure"):
            await setup_service.complete_setup(session, make_setup_request())
        assert session.commit_count == 0

    database = await read_database(setup_session_factory)
    assert database["settings"] == {
        SETUP_STATUS_KEY: SETUP_STATUS_PENDING,
        SETUP_ADMIN_UID_KEY: "",
    }
    assert database["users"] == []
    assert database["channels"] == []
    assert database["prompts"] == []
    assert database["profiles"] == []

    monkeypatch.setattr(setup_service.profile_crud, "create", original_create)
    async with setup_session_factory() as session:
        result = await setup_service.complete_setup(session, make_setup_request())

    assert result.access_token == FIXED_ACCESS_TOKEN
    database = await read_database(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
    assert len(database["users"]) == 1
    assert len(database["channels"]) == 1
    assert len(database["prompts"]) == 1
    assert len(database["profiles"]) == 1


@pytest.mark.asyncio
async def test_complete_setup_rolls_back_when_setup_admin_uid_update_fails(
    setup_session_factory: async_sessionmaker[CountingAsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        setup_service.system_setting_crud,
        "set_setup_admin_uid",
        AsyncMock(return_value=False),
    )

    async with setup_session_factory() as session:
        with pytest.raises(ServerException) as exc_info:
            await setup_service.complete_setup(session, make_setup_request())

    assert exc_info.value.code == 500
    assert exc_info.value.message == ERR_SETUP_STATE_UPDATE_FAILED

    database = await read_database(setup_session_factory)
    assert database["settings"] == {
        SETUP_STATUS_KEY: SETUP_STATUS_PENDING,
        SETUP_ADMIN_UID_KEY: "",
    }
    assert database["users"] == []
    assert database["channels"] == []
    assert database["prompts"] == []
    assert database["profiles"] == []


@pytest.mark.asyncio
async def test_complete_setup_rejects_duplicate_submission_without_changing_data(
    setup_session_factory: async_sessionmaker[CountingAsyncSession],
) -> None:
    request = make_setup_request()
    async with setup_session_factory() as session:
        await setup_service.complete_setup(session, request)

    before = await read_database(setup_session_factory)
    with pytest.raises(ParameterException) as exc_info:
        async with setup_session_factory() as session:
            await setup_service.complete_setup(session, request)

    assert exc_info.value.code == 409
    assert exc_info.value.message == ERR_SETUP_CONFLICT
    after = await read_database(setup_session_factory)
    assert {key: len(after[key]) for key in ("users", "channels", "prompts", "profiles")} == {key: len(before[key]) for key in ("users", "channels", "prompts", "profiles")}


@pytest.mark.asyncio
async def test_concurrent_setup_has_one_winner_and_one_conflict(
    setup_session_factory: async_sessionmaker[CountingAsyncSession],
) -> None:
    request = make_setup_request()

    async def run_setup() -> tuple[str, int | None]:
        async with setup_session_factory() as session:
            try:
                await setup_service.complete_setup(session, request)
            except ParameterException as error:
                return "conflict", error.code
            return "success", None

    results = await asyncio.gather(run_setup(), run_setup())

    assert sorted(results) == [("conflict", 409), ("success", None)]
    database = await read_database(setup_session_factory)
    assert database["settings"][SETUP_STATUS_KEY] == SETUP_STATUS_COMPLETED
    assert len(database["users"]) == 1
    assert len(database["channels"]) == 1
    assert len(database["prompts"]) == 1
    assert len(database["profiles"]) == 1


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
