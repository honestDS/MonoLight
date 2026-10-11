from collections.abc import AsyncGenerator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

import app.core.crypto
from app.api.v1.message_platforms import router
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.message_platform import MessagePlatform, MessagePlatformCreate, MessagePlatformResponse, MessagePlatformStatus, MessagePlatformUpdate
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
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

    await clone_sqlite_schema(tmp_path / "message-platform-configuration.db", tables=[ModelChannel.__table__, PromptLibrary.__table__, Profile.__table__, MessagePlatform.__table__])
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with session_factory() as session:
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
