from collections.abc import AsyncGenerator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlmodel import SQLModel

from app.api.v1.message_platforms import router
from app.api.v1.users import check_admin_privilege
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.message_platform import MessagePlatform
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.providers.database import get_db


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

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    ModelChannel.__table__,
                    PromptLibrary.__table__,
                    Profile.__table__,
                    MessagePlatform.__table__,
                ],
            )
        )
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
    app.dependency_overrides[check_admin_privilege] = override_admin

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
