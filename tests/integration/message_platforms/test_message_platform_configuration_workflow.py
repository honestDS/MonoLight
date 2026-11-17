from collections.abc import AsyncGenerator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.core.crypto
from app.api.v1.message_platforms import router
from app.core.constants import ERR_MESSAGE_PLATFORM_NAME_EXISTS, SESSION_MAX_TURNS_UPPER_BOUND
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.i18n import t
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.message_platform import MessagePlatform, MessagePlatformCreate, MessagePlatformResponse, MessagePlatformStatus, MessagePlatformUpdate
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.user import User
from app.providers.database import get_db
from tests.database_support import clone_sqlite_schema


@pytest_asyncio.fixture
async def message_platform_configuration_db(tmp_path) -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'message-platform-configuration.db'}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    await clone_sqlite_schema(tmp_path / "message-platform-configuration.db", tables=[ModelChannel.__table__, ChatSession.__table__, PromptLibrary.__table__, Profile.__table__, MessagePlatform.__table__, User.__table__])
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
            session.add_all([User(uid="user-1", username="user-1"), User(uid="user-2", username="user-2")])
            await session.commit()
            yield session
    finally:
        await engine.dispose()


def _profile_config(channel_id: int) -> dict:
    return {
        "channel": {
            "chat_channel": {
                "rules": [
                    {
                        "channel_id": channel_id,
                        "model_id": "chat-model",
                        "priority": 1,
                        "weight": 1,
                        "is_enabled": True,
                    }
                ]
            }
        },
        "security": {},
        "tool": {},
        "other": {},
        "memory": {},
    }


async def _seed_profiles(db: AsyncSession) -> tuple[Profile, Profile]:
    channel = ModelChannel(
        name="message-platform-chat",
        api_key="enc:v1:message-platform-key",
        base_url="https://message-platform.invalid/v1",
        model_ids=[
            {
                "model_id": "chat-model",
                "usage": "CHAT",
                "protocol": "OPENAI",
                "context_window_k": 64,
                "max_tokens": 4096,
                "is_enabled": True,
            }
        ],
    )
    db.add(channel)
    await db.flush()
    assert channel.id is not None
    profile_one = Profile(uid="user-1", name="platform profile one", configs=_profile_config(channel.id))
    profile_two = Profile(uid="user-2", name="platform profile two", configs=_profile_config(channel.id))
    db.add_all([profile_one, profile_two])
    await db.commit()
    assert profile_one.id is not None and profile_two.id is not None
    return profile_one, profile_two


@pytest.mark.asyncio
async def test_message_platform_name_uniqueness_is_persisted_per_owner(
    message_platform_configuration_db: AsyncSession,
) -> None:
    created_platforms = [
        await message_platform_crud.create(
            message_platform_configuration_db,
            obj_in=MessagePlatformCreate(name="owner-scoped-platform", uid=uid),
            commit=True,
        )
        for uid in ("user-1", "user-2", None)
    ]
    assert all(platform.id is not None for platform in created_platforms)

    for uid, platform in zip(("user-1", "user-2", None), created_platforms, strict=True):
        found = await message_platform_crud.get_by_name(
            message_platform_configuration_db,
            name="owner-scoped-platform",
            uid=uid,
        )
        assert found is not None
        assert found.id == platform.id

    assert (
        await message_platform_crud.get_by_name(
            message_platform_configuration_db,
            name="owner-scoped-platform",
            uid="unknown-user",
        )
        is None
    )

    with pytest.raises(IntegrityError):
        await message_platform_crud.create(
            message_platform_configuration_db,
            obj_in=MessagePlatformCreate(name="owner-scoped-platform", uid="user-1"),
            commit=True,
        )
    await message_platform_configuration_db.rollback()

    persisted_platforms = (await message_platform_configuration_db.execute(select(MessagePlatform).where(MessagePlatform.name == "owner-scoped-platform").order_by(MessagePlatform.id))).scalars().all()
    assert [(platform.id, platform.uid) for platform in persisted_platforms] == [(platform.id, uid) for platform, uid in zip(created_platforms, ("user-1", "user-2", None), strict=True)]


@pytest.mark.asyncio
async def test_message_platform_profile_assignment_follows_final_owner_through_create_update_and_clear(
    message_platform_configuration_db: AsyncSession,
) -> None:
    profile_one, profile_two = await _seed_profiles(message_platform_configuration_db)
    admin_state = {"uid": " user-1 ", "is_superuser": True}
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield message_platform_configuration_db

    async def override_admin():
        return SimpleNamespace(**admin_state)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_admin

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/message-platforms/create",
            json={
                "name": "profile-assignment-platform",
                "is_enabled": True,
                "profile_id": profile_one.id,
                "config": {},
            },
        )
        assert created.status_code == 200
        created_payload = created.json()
        assert created_payload["code"] == 200
        platform_id = created_payload["data"]["id"]
        assert created_payload["data"]["uid"] == "user-1"
        assert created_payload["data"]["profile_id"] == profile_one.id

        invalid_owner_change = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            json={"uid": " user-2 "},
        )
        assert invalid_owner_change.status_code == 404
        persisted = await message_platform_configuration_db.get(MessagePlatform, platform_id)
        assert persisted is not None
        assert persisted.uid == "user-1"
        assert persisted.profile_id == profile_one.id

        owner_and_profile_change = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            json={"uid": " user-2 ", "profile_id": profile_two.id},
        )
        assert owner_and_profile_change.status_code == 200
        changed_payload = owner_and_profile_change.json()
        assert changed_payload["code"] == 200
        assert changed_payload["data"]["uid"] == "user-2"
        assert changed_payload["data"]["profile_id"] == profile_two.id
        await message_platform_configuration_db.refresh(persisted)
        assert persisted.uid == "user-2"
        assert persisted.profile_id == profile_two.id

        cleared = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            json={"profile_id": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["code"] == 200
        await message_platform_configuration_db.refresh(persisted)
        assert persisted.uid == "user-2"
        assert persisted.profile_id is None


@pytest.mark.parametrize(
    "input_kind",
    ["model", "dict"],
)
@pytest.mark.asyncio
async def test_message_platform_crud_create_encrypts_config_secrets(
    message_platform_configuration_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    input_kind: str,
) -> None:
    monkeypatch.setattr(app.core.crypto, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
    create_payload = MessagePlatformCreate(
        name="encrypted-config-platform",
        config={
            "token": "plain-token",
            "bot_token": "plain-bot-token",
            "base_url": "https://message-platform.invalid/v1",
        },
    )
    obj_in = create_payload if input_kind == "model" else create_payload.model_dump()

    created = await message_platform_crud.create(
        message_platform_configuration_db,
        obj_in=obj_in,
    )
    persisted_config = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == created.id))).scalar_one()

    assert persisted_config["token"].startswith("enc:v1:")
    assert persisted_config["token"] != "plain-token"
    assert persisted_config["bot_token"].startswith("enc:v1:")
    assert persisted_config["bot_token"] != "plain-bot-token"
    assert persisted_config["base_url"] == "https://message-platform.invalid/v1"

    await message_platform_configuration_db.refresh(created)
    assert created.get_config_secret("token") == "plain-token"
    assert created.get_config_secret("bot_token") == "plain-bot-token"
    response = MessagePlatformResponse.model_validate(created)
    assert response.config["token"] == "plain-token"
    assert response.config["bot_token"] == "plain-bot-token"
    assert response.config["base_url"] == "https://message-platform.invalid/v1"


@pytest.mark.parametrize(
    "input_kind",
    ["model", "dict"],
)
@pytest.mark.asyncio
async def test_message_platform_crud_update_encrypts_config_secrets_idempotently(
    message_platform_configuration_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    input_kind: str,
) -> None:
    monkeypatch.setattr(app.core.crypto, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
    created = await message_platform_crud.create(
        message_platform_configuration_db,
        obj_in=MessagePlatformCreate(
            name="encrypted-config-platform",
            config={
                "token": "plain-token",
                "bot_token": "plain-bot-token",
                "base_url": "https://message-platform.invalid/v1",
            },
        ),
        commit=True,
    )
    update_payload = MessagePlatformUpdate(
        config={
            "token": "updated-token",
            "bot_token": "updated-bot-token",
            "base_url": "https://message-platform.invalid/v1",
        },
    )
    update_input = update_payload if input_kind == "model" else update_payload.model_dump(exclude_unset=True)
    updated = await message_platform_crud.update(
        message_platform_configuration_db,
        db_obj=created,
        obj_in=update_input,
        commit=True,
    )
    persisted_config = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == updated.id))).scalar_one()

    assert persisted_config["token"].startswith("enc:v1:")
    assert persisted_config["token"] != "updated-token"
    assert persisted_config["bot_token"].startswith("enc:v1:")
    assert persisted_config["bot_token"] != "updated-bot-token"
    assert persisted_config["base_url"] == "https://message-platform.invalid/v1"
    encrypted_config = dict(persisted_config)

    await message_platform_configuration_db.refresh(updated)
    assert updated.get_config_secret("token") == "updated-token"
    assert updated.get_config_secret("bot_token") == "updated-bot-token"
    response = MessagePlatformResponse.model_validate(updated)
    assert response.config["token"] == "updated-token"
    assert response.config["bot_token"] == "updated-bot-token"
    assert response.config["base_url"] == "https://message-platform.invalid/v1"

    await message_platform_crud.update(
        message_platform_configuration_db,
        db_obj=updated,
        obj_in=MessagePlatformUpdate(name="encrypted-config-platform-renamed"),
        commit=True,
    )
    unchanged_config = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == updated.id))).scalar_one()
    assert unchanged_config == encrypted_config

    await message_platform_configuration_db.refresh(updated)
    assert updated.get_config_secret("token") == "updated-token"
    assert updated.get_config_secret("bot_token") == "updated-bot-token"


@pytest.mark.parametrize(
    "update_kind",
    ["metadata", "same-config", "runtime-state"],
)
@pytest.mark.asyncio
async def test_message_platform_legacy_plaintext_config_reads_and_encrypts_on_update(
    message_platform_configuration_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    update_kind: str,
) -> None:
    monkeypatch.setattr(app.core.crypto, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
    payload = MessagePlatformCreate(
        name="legacy-config-platform",
        uid="user-1",
        config={
            "token": "legacy-token",
            "bot_token": "legacy-bot-token",
            "base_url": "https://message-platform.invalid/v1",
        },
        state={"sync_buf": "legacy-sync"},
    )
    inserted = await message_platform_configuration_db.execute(
        MessagePlatform.__table__.insert().values(
            **payload.model_dump(),
            status=MessagePlatformStatus.DISCONNECTED,
        )
    )
    platform_id = inserted.inserted_primary_key[0]
    await message_platform_configuration_db.commit()

    loaded = await message_platform_crud.get(message_platform_configuration_db, id=platform_id)
    assert loaded is not None
    assert loaded.get_config_secret("token") == "legacy-token"
    assert loaded.get_config_secret("bot_token") == "legacy-bot-token"
    response = MessagePlatformResponse.model_validate(loaded)
    assert response.config["token"] == "legacy-token"
    assert response.config["bot_token"] == "legacy-bot-token"
    assert response.config["base_url"] == "https://message-platform.invalid/v1"

    plaintext_config = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == platform_id))).scalar_one()
    assert plaintext_config == payload.config

    if update_kind == "metadata":
        await message_platform_crud.update(
            message_platform_configuration_db,
            db_obj=loaded,
            obj_in=MessagePlatformUpdate(name="legacy-config-platform-renamed"),
            commit=True,
        )
    elif update_kind == "same-config":
        await message_platform_crud.update(
            message_platform_configuration_db,
            db_obj=loaded,
            obj_in=MessagePlatformUpdate(config=payload.config),
            commit=True,
        )
    else:
        await message_platform_crud.update_runtime_state(
            message_platform_configuration_db,
            platform=loaded,
            state={"sync_buf": "updated-sync"},
        )

    persisted_config = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == platform_id))).scalar_one()
    assert persisted_config["token"].startswith("enc:v1:")
    assert persisted_config["token"] != "legacy-token"
    assert persisted_config["bot_token"].startswith("enc:v1:")
    assert persisted_config["bot_token"] != "legacy-bot-token"
    assert persisted_config["base_url"] == "https://message-platform.invalid/v1"

    await message_platform_configuration_db.refresh(loaded)
    assert loaded.get_config_secret("token") == "legacy-token"
    assert loaded.get_config_secret("bot_token") == "legacy-bot-token"
    response = MessagePlatformResponse.model_validate(loaded)
    assert response.config["token"] == "legacy-token"
    assert response.config["bot_token"] == "legacy-bot-token"
    assert response.config["base_url"] == "https://message-platform.invalid/v1"


@pytest.mark.parametrize("operation", ("create", "rename", "transfer"))
@pytest.mark.asyncio
async def test_message_platform_name_conflict_rolls_back_and_keeps_session_usable(
    message_platform_configuration_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
) -> None:
    monkeypatch.setattr(app.core.crypto, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")

    await message_platform_crud.create(
        message_platform_configuration_db,
        obj_in=MessagePlatformCreate(
            uid="user-1",
            name="occupied",
            config={"token": "occupied-user-one-token"},
        ),
        commit=True,
    )
    editable_user_one = await message_platform_crud.create(
        message_platform_configuration_db,
        obj_in=MessagePlatformCreate(
            uid="user-1",
            name="editable",
            config={"token": "editable-user-one-token"},
        ),
        commit=True,
    )
    occupied_user_two = await message_platform_crud.create(
        message_platform_configuration_db,
        obj_in=MessagePlatformCreate(
            uid="user-2",
            name="occupied",
            config={"token": "occupied-user-two-token"},
        ),
        commit=True,
    )

    initial_platforms = (await message_platform_configuration_db.execute(select(MessagePlatform).order_by(MessagePlatform.id))).scalars().all()
    platform_ids = [int(platform.id) for platform in initial_platforms]
    snapshot = [platform.model_dump() for platform in initial_platforms]
    editable_user_one_id = int(editable_user_one.id)
    occupied_user_two_id = int(occupied_user_two.id)

    async def stale_get_by_name(*_args, **_kwargs):
        return None

    monkeypatch.setattr(message_platform_crud, "get_by_name", stale_get_by_name)

    test_app = FastAPI()
    register_handlers(test_app)
    test_app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield message_platform_configuration_db

    async def override_current_user():
        return SimpleNamespace(uid="user-1", is_superuser=True)

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_current_user

    conflict_token = f"{operation}-conflict-token"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        if operation == "create":
            failed = await client.post(
                "/api/v1/message-platforms/create",
                json={"uid": "user-1", "name": "occupied", "config": {"token": conflict_token}},
            )
        elif operation == "rename":
            failed = await client.post(
                "/api/v1/message-platforms/update",
                params={"platform_id": editable_user_one_id},
                json={"name": "occupied", "config": {"token": conflict_token}},
            )
        else:
            failed = await client.post(
                "/api/v1/message-platforms/update",
                params={"platform_id": occupied_user_two_id},
                json={"uid": "user-1", "config": {"token": conflict_token}},
            )

        failed_payload = failed.json()
        assert failed.status_code == 400
        assert failed_payload["code"] == 400
        assert failed_payload["message"] == t(ERR_MESSAGE_PLATFORM_NAME_EXISTS)
        assert failed_payload["data"] is None
        assert set(failed_payload) == {"code", "message", "data"}
        assert conflict_token not in failed.text
        assert message_platform_configuration_db.is_active

        restored_platforms = (await message_platform_configuration_db.execute(select(MessagePlatform).order_by(MessagePlatform.id))).scalars().all()
        assert [int(platform.id) for platform in restored_platforms] == platform_ids
        assert [platform.model_dump() for platform in restored_platforms] == snapshot

        recovered_name = f"recovered-{operation}"
        recovered_token = f"{operation}-recovered-token"
        recovered = await client.post(
            "/api/v1/message-platforms/create",
            json={"uid": "user-1", "name": recovered_name, "config": {"token": recovered_token}},
        )
        assert recovered.status_code == 200
        recovered_payload = recovered.json()
        assert recovered_payload["code"] == 200
        assert recovered_payload["data"]["name"] == recovered_name

        persisted_recovered = (await message_platform_configuration_db.execute(select(MessagePlatform).where(MessagePlatform.id == recovered_payload["data"]["id"]))).scalar_one()
        assert persisted_recovered.uid == "user-1"
        assert persisted_recovered.name == recovered_name
        assert persisted_recovered.get_config_secret("token") == recovered_token


@pytest.mark.asyncio
async def test_message_platform_session_settings_api_round_trip_and_partial_updates(
    message_platform_configuration_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.core.crypto, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
    test_app = FastAPI()
    register_handlers(test_app)
    test_app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield message_platform_configuration_db

    async def override_admin():
        return SimpleNamespace(uid="user-1", is_superuser=True)

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_admin

    session_settings = {
        "enable_markdown": True,
        "goal_mode": False,
        "max_turns": 7,
        "show_tool_calls": False,
        "show_reasoning": False,
    }
    initial_config = {
        "token": "session-settings-token",
        "api_timeout_ms": 8000,
        "long_poll_timeout_ms": 20000,
        "poll_interval_ms": 300,
        "session_settings": session_settings,
        "stream_llm_request": True,
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/message-platforms/create",
            json={
                "name": "session-settings-platform",
                "is_enabled": False,
                "use_stream_dispatch": False,
                "config": initial_config,
            },
        )
        assert created.status_code == 200
        created_data = created.json()["data"]
        platform_id = created_data["id"]
        assert created_data["use_stream_dispatch"] is False
        assert created_data["config"]["session_settings"] == session_settings
        assert created_data["config"]["stream_llm_request"] is True
        assert created_data["config"]["token"] == initial_config["token"]
        assert created_data["config"]["api_timeout_ms"] == initial_config["api_timeout_ms"]
        assert created_data["config"]["long_poll_timeout_ms"] == initial_config["long_poll_timeout_ms"]
        assert created_data["config"]["poll_interval_ms"] == initial_config["poll_interval_ms"]

        stored_config = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == platform_id))).scalar_one()
        assert stored_config["session_settings"] == session_settings
        assert stored_config["stream_llm_request"] is True
        assert stored_config["token"].startswith("enc:v1:")
        assert stored_config["token"] != initial_config["token"]
        assert stored_config["api_timeout_ms"] == initial_config["api_timeout_ms"]
        assert stored_config["long_poll_timeout_ms"] == initial_config["long_poll_timeout_ms"]
        assert stored_config["poll_interval_ms"] == initial_config["poll_interval_ms"]
        initial_encrypted_token = stored_config["token"]
        persisted = await message_platform_configuration_db.get(MessagePlatform, platform_id)
        assert persisted is not None
        assert persisted.get_config_secret("token") == initial_config["token"]
        assert persisted.use_stream_dispatch is False

        fetched = await client.get(
            "/api/v1/message-platforms/get",
            params={"platform_id": platform_id},
        )
        assert fetched.status_code == 200
        fetched_data = fetched.json()["data"]
        assert fetched_data["use_stream_dispatch"] is False
        assert fetched_data["config"]["session_settings"] == session_settings
        assert fetched_data["config"]["stream_llm_request"] is True
        assert fetched_data["config"]["token"] == initial_config["token"]
        assert fetched_data["config"]["poll_interval_ms"] == initial_config["poll_interval_ms"]

        listed = await client.get("/api/v1/message-platforms/list", params={"page": 1, "size": 10})
        assert listed.status_code == 200
        listed_data = next(item for item in listed.json()["data"]["items"] if item["id"] == platform_id)
        assert listed_data["use_stream_dispatch"] is False
        assert listed_data["config"]["session_settings"] == session_settings
        assert listed_data["config"]["stream_llm_request"] is True
        assert listed_data["config"]["token"] == initial_config["token"]
        assert listed_data["config"]["poll_interval_ms"] == initial_config["poll_interval_ms"]

        updated_settings = {**session_settings, "max_turns": 11}
        partially_updated = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            json={"config": {"session_settings": {"max_turns": 11}}},
        )
        assert partially_updated.status_code == 200
        partially_updated_data = partially_updated.json()["data"]
        assert partially_updated_data["use_stream_dispatch"] is False
        assert partially_updated_data["config"]["session_settings"] == updated_settings
        assert partially_updated_data["config"]["stream_llm_request"] is True
        assert partially_updated_data["config"]["token"] == initial_config["token"]
        assert partially_updated_data["config"]["poll_interval_ms"] == initial_config["poll_interval_ms"]

        stored_after_partial = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == platform_id))).scalar_one()
        assert stored_after_partial["session_settings"] == updated_settings
        assert stored_after_partial["stream_llm_request"] is True
        assert stored_after_partial["token"] == initial_encrypted_token
        assert stored_after_partial["api_timeout_ms"] == initial_config["api_timeout_ms"]
        assert stored_after_partial["long_poll_timeout_ms"] == initial_config["long_poll_timeout_ms"]
        assert stored_after_partial["poll_interval_ms"] == initial_config["poll_interval_ms"]
        await message_platform_configuration_db.refresh(persisted)
        assert persisted.use_stream_dispatch is False
        assert persisted.get_config_secret("token") == initial_config["token"]

        stream_disabled = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            json={"config": {"stream_llm_request": False}},
        )
        assert stream_disabled.status_code == 200
        stream_disabled_data = stream_disabled.json()["data"]
        assert stream_disabled_data["use_stream_dispatch"] is False
        assert stream_disabled_data["config"]["stream_llm_request"] is False
        assert stream_disabled_data["config"]["session_settings"] == updated_settings
        assert stream_disabled_data["config"]["token"] == initial_config["token"]

        dispatch_enabled = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            json={"use_stream_dispatch": True},
        )
        assert dispatch_enabled.status_code == 200
        dispatch_enabled_data = dispatch_enabled.json()["data"]
        assert dispatch_enabled_data["use_stream_dispatch"] is True
        assert dispatch_enabled_data["config"]["stream_llm_request"] is False
        assert dispatch_enabled_data["config"]["session_settings"] == updated_settings
        assert dispatch_enabled_data["config"]["token"] == initial_config["token"]

        stream_enabled = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            json={"config": {"stream_llm_request": True}},
        )
        assert stream_enabled.status_code == 200
        stream_enabled_data = stream_enabled.json()["data"]
        assert stream_enabled_data["use_stream_dispatch"] is True
        assert stream_enabled_data["config"]["stream_llm_request"] is True
        assert stream_enabled_data["config"]["session_settings"] == updated_settings
        assert stream_enabled_data["config"]["token"] == initial_config["token"]

        dispatch_disabled = await client.post(
            "/api/v1/message-platforms/update",
            params={"platform_id": platform_id},
            json={"use_stream_dispatch": False},
        )
        assert dispatch_disabled.status_code == 200
        dispatch_disabled_data = dispatch_disabled.json()["data"]
        assert dispatch_disabled_data["use_stream_dispatch"] is False
        assert dispatch_disabled_data["config"]["stream_llm_request"] is True
        assert dispatch_disabled_data["config"]["session_settings"] == updated_settings
        assert dispatch_disabled_data["config"]["token"] == initial_config["token"]

        repeated_updates = []
        for _ in range(2):
            repeated = await client.post(
                "/api/v1/message-platforms/update",
                params={"platform_id": platform_id},
                json={"config": {"session_settings": {"max_turns": 11}}},
            )
            assert repeated.status_code == 200
            repeated_updates.append(repeated.json()["data"])
        first_repeated = dict(repeated_updates[0])
        second_repeated = dict(repeated_updates[1])
        first_repeated.pop("updated_at", None)
        second_repeated.pop("updated_at", None)
        assert first_repeated == second_repeated

        final_get = await client.get("/api/v1/message-platforms/get", params={"platform_id": platform_id})
        assert final_get.status_code == 200
        final_data = final_get.json()["data"]
        assert final_data["use_stream_dispatch"] is False
        assert final_data["config"]["session_settings"] == updated_settings
        assert final_data["config"]["stream_llm_request"] is True
        assert final_data["config"]["token"] == initial_config["token"]
        assert final_data["config"]["api_timeout_ms"] == initial_config["api_timeout_ms"]
        assert final_data["config"]["long_poll_timeout_ms"] == initial_config["long_poll_timeout_ms"]
        assert final_data["config"]["poll_interval_ms"] == initial_config["poll_interval_ms"]

        final_list = await client.get("/api/v1/message-platforms/list", params={"page": 1, "size": 10})
        assert final_list.status_code == 200
        final_list_data = next(item for item in final_list.json()["data"]["items"] if item["id"] == platform_id)
        assert final_list_data["use_stream_dispatch"] is False
        assert final_list_data["config"]["session_settings"] == updated_settings
        assert final_list_data["config"]["stream_llm_request"] is True
        assert final_list_data["config"]["token"] == initial_config["token"]
        assert final_list_data["config"]["api_timeout_ms"] == initial_config["api_timeout_ms"]
        assert final_list_data["config"]["long_poll_timeout_ms"] == initial_config["long_poll_timeout_ms"]
        assert final_list_data["config"]["poll_interval_ms"] == initial_config["poll_interval_ms"]

        final_stored_config = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == platform_id))).scalar_one()
        assert final_stored_config["session_settings"] == updated_settings
        assert final_stored_config["stream_llm_request"] is True
        assert final_stored_config["token"] == initial_encrypted_token
        await message_platform_configuration_db.refresh(persisted)
        assert persisted.use_stream_dispatch is False


@pytest.mark.parametrize("operation", ("create", "update"))
@pytest.mark.parametrize(
    ("invalid_config", "case_id"),
    [
        pytest.param({"session_settings": {"max_turns": 0}}, "max-turns-zero"),
        pytest.param({"session_settings": {"max_turns": -1}}, "max-turns-negative"),
        pytest.param({"session_settings": {"max_turns": 1.5}}, "max-turns-float"),
        pytest.param({"session_settings": {"max_turns": "7"}}, "max-turns-string"),
        pytest.param({"session_settings": {"max_turns": True}}, "max-turns-bool"),
        pytest.param({"session_settings": {"max_turns": SESSION_MAX_TURNS_UPPER_BOUND + 1}}, "max-turns-too-large"),
        pytest.param({"session_settings": {"goal_mode": "false"}}, "goal-mode-string"),
        pytest.param({"stream_llm_request": "true"}, "stream-llm-request-string"),
        pytest.param({"session_settings": {"unknown_setting": False}}, "unknown-session-setting"),
        pytest.param({"session_settings": None}, "null-session-settings"),
    ],
)
@pytest.mark.asyncio
async def test_message_platform_session_settings_api_rejects_invalid_config_on_create_and_update(
    message_platform_configuration_db: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    invalid_config: dict,
    case_id: str,
) -> None:
    monkeypatch.setattr(app.core.crypto, "get_channel_encryption_key", lambda: b"0123456789abcdef0123456789abcdef")
    test_app = FastAPI()
    register_handlers(test_app)
    test_app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield message_platform_configuration_db

    async def override_admin():
        return SimpleNamespace(uid="user-1", is_superuser=True)

    test_app.dependency_overrides[get_db] = override_get_db
    test_app.dependency_overrides[get_current_user] = override_admin

    valid_config = {
        "token": "validation-token",
        "poll_interval_ms": 300,
        "session_settings": {
            "enable_markdown": True,
            "goal_mode": False,
            "max_turns": 7,
            "show_tool_calls": False,
            "show_reasoning": False,
        },
        "stream_llm_request": True,
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=test_app),
        base_url="http://test",
    ) as client:
        if operation == "create":
            failed = await client.post(
                "/api/v1/message-platforms/create",
                json={"name": f"invalid-{case_id}", "config": invalid_config},
            )
            assert failed.status_code == 422
            persisted_platforms = (await message_platform_configuration_db.execute(select(MessagePlatform))).scalars().all()
            assert persisted_platforms == []
        else:
            created = await client.post(
                "/api/v1/message-platforms/create",
                json={"name": f"baseline-{case_id}", "config": valid_config},
            )
            assert created.status_code == 200
            platform_id = created.json()["data"]["id"]
            before_config = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == platform_id))).scalar_one()
            before_platform = await message_platform_configuration_db.get(MessagePlatform, platform_id)
            assert before_platform is not None
            before_use_stream_dispatch = before_platform.use_stream_dispatch

            failed = await client.post(
                "/api/v1/message-platforms/update",
                params={"platform_id": platform_id},
                json={"config": invalid_config},
            )
            assert failed.status_code == 422

            after_config = (await message_platform_configuration_db.execute(select(MessagePlatform.config).where(MessagePlatform.id == platform_id))).scalar_one()
            after_platform = await message_platform_configuration_db.get(MessagePlatform, platform_id)
            assert after_platform is not None
            assert after_config == before_config
            assert after_platform.use_stream_dispatch == before_use_stream_dispatch
