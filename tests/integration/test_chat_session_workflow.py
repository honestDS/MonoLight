from collections.abc import AsyncGenerator
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.api.v1.chat import router
from app.core.constants import (
    ERR_SESSION_NO_PERMISSION,
    GUIDANCE_MESSAGE_PREFIX,
    GUIDANCE_MESSAGE_SUFFIX,
)
from app.core.i18n import t
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.channel import ModelChannel
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.providers.database import get_db


@pytest_asyncio.fixture
async def chat_session_database(tmp_path) -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'chat-session-workflow.db'}",
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
                    ChatSession.__table__,
                    Message.__table__,
                ],
            )
        )
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


async def _seed_profiles(db: AsyncSession) -> tuple[Profile, Profile, Profile]:
    channel = ModelChannel(
        name="chat-session-channel",
        api_key="enc:v1:chat-session-key",
        base_url="https://chat-session.example.com/v1",
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
    config = _profile_config(channel.id)
    profiles = (
        Profile(uid="user-1", name="primary profile", configs=config),
        Profile(uid="user-1", name="alternate profile", configs=config),
        Profile(uid="user-2", name="other user profile", configs=config),
    )
    db.add_all(profiles)
    await db.commit()
    assert all(profile.id is not None for profile in profiles)
    return profiles


def _build_app(db: AsyncSession, auth_state: dict[str, object]) -> FastAPI:
    app = FastAPI()
    register_handlers(app)
    app.include_router(router, prefix="/api/v1")

    async def override_get_db():
        yield db

    def override_get_current_user():
        return SimpleNamespace(**auth_state)

    app.dependency_overrides[get_db] = override_get_db
    app.dependency_overrides[get_current_user] = override_get_current_user
    return app


@pytest.mark.asyncio
async def test_web_session_lifecycle_creates_with_profile_updates_settings_and_enforces_owner(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "hello",
                "profile_override_id": primary_profile.id,
                "show_tool_calls": False,
                "show_reasoning": False,
            },
        )
        assert created.status_code == 200
        created_payload = created.json()
        assert created_payload["choices"][0]["finish_reason"] == "new_session"
        session_id = created_payload["choices"][0]["message"]["content"]
        assert isinstance(session_id, str) and session_id

        persisted = await chat_session_database.get(ChatSession, session_id)
        assert persisted is not None
        assert persisted.uid == "user-1"
        assert persisted.source == "http"
        assert persisted.reply_target_source == "http"
        assert persisted.profile_id is None
        assert persisted.profile_override_id == primary_profile.id
        assert persisted.show_tool_calls is False
        assert persisted.show_reasoning is False
        assert persisted.enable_markdown is False

        updated = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": session_id,
                "enable_markdown": True,
                "show_tool_calls": True,
                "show_reasoning": True,
                "profile_override_id": alternate_profile.id,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["code"] == 200
        await chat_session_database.refresh(persisted)
        assert persisted.enable_markdown is True
        assert persisted.show_tool_calls is True
        assert persisted.show_reasoning is True
        assert persisted.profile_override_id == alternate_profile.id

        guidance = await client.post(
            "/api/v1/chat/sessions/guidance",
            json={"session_id": session_id, "content": "guide this web session"},
        )
        assert guidance.status_code == 200
        assert guidance.json()["code"] == 403
        guidance_rows = list(
            (
                await chat_session_database.execute(
                    select(Message).where(
                        Message.session_id == session_id,
                        Message.type == MessageType.GUIDANCE,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert guidance_rows == []

        auth_state["uid"] = "user-2"
        forbidden = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session_id, "show_reasoning": False},
        )
        assert forbidden.status_code == 200
        assert forbidden.json()["message"] == t(ERR_SESSION_NO_PERMISSION)
        await chat_session_database.refresh(persisted)
        assert persisted.show_reasoning is True

        auth_state["uid"] = "user-1"
        cleared = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session_id, "profile_override_id": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["code"] == 200
        await chat_session_database.refresh(persisted)
        assert persisted.profile_override_id is None


@pytest.mark.asyncio
async def test_external_session_lifecycle_persists_guidance_allows_profile_override_and_blocks_web_only_setting(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    external_session = ChatSession(
        session_id="external-session-1",
        uid="user-1",
        profile_id=primary_profile.id,
        source="weixin-openclaw",
        reply_target_source="weixin-openclaw",
        enable_markdown=True,
    )
    chat_session_database.add(external_session)
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    expected_content = f"{GUIDANCE_MESSAGE_PREFIX}请先回答重点{GUIDANCE_MESSAGE_SUFFIX}"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        guidance = await client.post(
            "/api/v1/chat/sessions/guidance",
            json={
                "session_id": external_session.session_id,
                "content": " 请先回答重点 ",
            },
        )
        assert guidance.status_code == 200
        guidance_payload = guidance.json()
        assert guidance_payload["code"] == 200
        assert guidance_payload["data"]["type"] == "guidance"
        assert guidance_payload["data"]["role"] == "system"
        assert guidance_payload["data"]["profile_id"] == primary_profile.id
        assert guidance_payload["data"]["is_processed"] is False
        assert guidance_payload["data"]["content"] == expected_content

        stored_guidance = list(
            (
                await chat_session_database.execute(
                    select(Message).where(
                        Message.session_id == external_session.session_id,
                        Message.type == MessageType.GUIDANCE,
                    )
                )
            )
            .scalars()
            .all()
        )
        assert len(stored_guidance) == 1
        assert stored_guidance[0].content == expected_content
        assert stored_guidance[0].role == MessageRole.SYSTEM
        assert stored_guidance[0].profile_id == primary_profile.id

        override = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": external_session.session_id,
                "profile_override_id": alternate_profile.id,
                "show_tool_calls": False,
                "show_reasoning": False,
            },
        )
        assert override.status_code == 200
        assert override.json()["code"] == 200
        await chat_session_database.refresh(external_session)
        assert external_session.profile_override_id == alternate_profile.id
        assert external_session.show_tool_calls is False
        assert external_session.show_reasoning is False

        read_only = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": external_session.session_id,
                "enable_markdown": False,
            },
        )
        assert read_only.status_code == 200
        assert read_only.json()["code"] == 403
        await chat_session_database.refresh(external_session)
        assert external_session.enable_markdown is True

        clear_override = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": external_session.session_id,
                "profile_override_id": None,
            },
        )
        assert clear_override.status_code == 200
        assert clear_override.json()["code"] == 200
        await chat_session_database.refresh(external_session)
        assert external_session.profile_override_id is None
