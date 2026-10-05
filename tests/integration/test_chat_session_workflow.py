import uuid
from collections.abc import AsyncGenerator
from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import select

from app.api.v1.chat import router
from app.core.constants import (
    ERR_SESSION_NO_PERMISSION,
    ERR_SESSION_TRANSPORT_CHANGE_ACTIVE,
    GUIDANCE_MESSAGE_PREFIX,
    GUIDANCE_MESSAGE_SUFFIX,
    SESSION_MAX_TURNS_UPPER_BOUND,
)
from app.core.i18n import t
from app.core.security import get_current_user
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.core.utils.time import get_local_time
from app.handler import register_handlers
from app.models.audit import AuditConfirmationClaim, AuditRecord
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.channel import ModelChannel
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SESSION_REPLY_ACTIVE_STATUSES,
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.session_todo import SessionTodoPlan
from app.models.user import User
from app.providers.database import get_db
from tests.database_support import clone_sqlite_schema


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

    await clone_sqlite_schema(
        tmp_path / "chat-session-workflow.db",
        tables=[
            ModelChannel.__table__,
            PromptLibrary.__table__,
            Profile.__table__,
            ChatSession.__table__,
            SessionTodoPlan.__table__,
            Message.__table__,
            User.__table__,
            AuditRecord.__table__,
            AuditConfirmationClaim.__table__,
            SessionReplySequence.__table__,
            SessionReplyWorkItem.__table__,
            BackgroundTask.__table__,
        ],
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
    db.add(User(uid="user-1", username="alice"))
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


def _patch_http_submission(monkeypatch: pytest.MonkeyPatch, profile: Profile) -> None:
    async def resolve_profile(_db: AsyncSession, *, uid: str, session_id: str) -> Profile:
        assert uid == profile.uid
        assert session_id
        return profile

    async def validate_initial_message(*_args, **_kwargs) -> None:
        return None

    monkeypatch.setattr("app.adapters.chat_web.resolve_profile_for_session", resolve_profile)
    monkeypatch.setattr("app.adapters.chat_web.ChatDispatcher.validate_initial_message_before_save", validate_initial_message)


@pytest.mark.asyncio
async def test_session_todo_api_reads_current_plan_and_enforces_owner(
    chat_session_database: AsyncSession,
) -> None:
    session = ChatSession(
        session_id="todo-session-1",
        uid="user-1",
        source="http",
        reply_target_source="http",
    )
    plan = SessionTodoPlan(
        session_id=session.session_id,
        uid=session.uid,
        revision=3,
        todos=[
            {"content": "inspect layout", "status": "completed"},
            {"content": "build todo panel", "status": "in_progress"},
        ],
    )
    chat_session_database.add_all([session, plan])
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.get(
            "/api/v1/chat/sessions/todo",
            params={"session_id": session.session_id},
        )
        assert response.status_code == 200
        assert response.json()["data"] == {
            "revision": 3,
            "todos": [
                {"content": "inspect layout", "status": "completed"},
                {"content": "build todo panel", "status": "in_progress"},
            ],
        }

        auth_state["uid"] = "user-2"
        forbidden = await client.get(
            "/api/v1/chat/sessions/todo",
            params={"session_id": session.session_id},
        )
        assert forbidden.status_code == 200
        assert forbidden.json()["message"] == t(ERR_SESSION_NO_PERMISSION)


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
                "transport_mode": "ws",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["code"] == 200
        await chat_session_database.refresh(persisted)
        assert persisted.enable_markdown is True
        assert persisted.show_tool_calls is True
        assert persisted.show_reasoning is True
        assert persisted.profile_override_id == alternate_profile.id
        assert persisted.source == "ws"

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session_id)
        assert listed_session["source"] == "ws"

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
            json={
                "session_id": session_id,
                "show_reasoning": False,
                "transport_mode": "http",
            },
        )
        assert forbidden.status_code == 200
        assert forbidden.json()["message"] == t(ERR_SESSION_NO_PERMISSION)
        await chat_session_database.refresh(persisted)
        assert persisted.show_reasoning is True
        assert persisted.source == "ws"

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
async def test_http_new_session_defaults_goal_mode_and_max_turns_independent_of_profile(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "default session",
                "profile_override_id": primary_profile.id,
            },
        )
        assert created.status_code == 200
        session_id = created.json()["choices"][0]["message"]["content"]

        persisted = await chat_session_database.get(ChatSession, session_id)
        assert persisted is not None
        assert persisted.profile_override_id == primary_profile.id
        assert persisted.goal_mode is True
        assert persisted.max_turns == 5

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session_id)
        assert listed_session["profile_override_id"] == primary_profile.id
        assert listed_session["goal_mode"] is True
        assert listed_session["max_turns"] == 5


@pytest.mark.asyncio
async def test_sessions_using_one_profile_keep_independent_goal_and_max_turn_settings(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    assert alternate_profile.id is not None
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_created = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "configured session",
                "profile_override_id": primary_profile.id,
                "goal_mode": False,
                "max_turns": 21,
            },
        )
        second_created = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "default session",
                "profile_override_id": primary_profile.id,
            },
        )
        assert first_created.status_code == 200
        assert second_created.status_code == 200
        first_id = first_created.json()["choices"][0]["message"]["content"]
        second_id = second_created.json()["choices"][0]["message"]["content"]
        assert first_id != second_id

        first = await chat_session_database.get(ChatSession, first_id)
        second = await chat_session_database.get(ChatSession, second_id)
        assert first is not None
        assert second is not None
        assert (first.goal_mode, first.max_turns) == (False, 21)
        assert (second.goal_mode, second.max_turns) == (True, 5)

        updated = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": first_id,
                "goal_mode": False,
                "max_turns": 1_000_000,
            },
        )
        assert updated.status_code == 200
        assert updated.json()["code"] == 200

        await chat_session_database.refresh(first)
        await chat_session_database.refresh(second)
        assert (first.goal_mode, first.max_turns) == (False, 1_000_000)
        assert (second.goal_mode, second.max_turns) == (True, 5)

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_by_id = {item["session_id"]: item for item in listed.json()["data"]}
        assert (listed_by_id[first_id]["goal_mode"], listed_by_id[first_id]["max_turns"]) == (False, 1_000_000)
        assert (listed_by_id[second_id]["goal_mode"], listed_by_id[second_id]["max_turns"]) == (True, 5)

        for goal_mode in (True, False):
            toggled = await client.post(
                "/api/v1/chat/sessions/setting",
                json={"session_id": first_id, "goal_mode": goal_mode},
            )
            assert toggled.status_code == 200
            assert toggled.json()["code"] == 200
            await chat_session_database.refresh(first)
            assert first.goal_mode is goal_mode
            assert first.max_turns == 1_000_000

        changed_profile = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": first_id, "profile_override_id": alternate_profile.id},
        )
        assert changed_profile.status_code == 200
        assert changed_profile.json()["code"] == 200
        await chat_session_database.refresh(first)
        await chat_session_database.refresh(second)
        assert first.profile_override_id == alternate_profile.id
        assert (first.goal_mode, first.max_turns) == (False, 1_000_000)
        assert second.profile_override_id == primary_profile.id
        assert (second.goal_mode, second.max_turns) == (True, 5)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "ws", "weixin-openclaw", "other-message-platform"])
async def test_session_setting_rejects_non_owner_without_mutating_db(
    chat_session_database: AsyncSession,
    source: str,
) -> None:
    _primary_profile, _alternate_profile, other_profile = await _seed_profiles(chat_session_database)
    assert other_profile.id is not None
    session = ChatSession(
        session_id="other-owner-setting-session",
        uid="user-2",
        profile_override_id=other_profile.id,
        source=source,
        reply_target_source=source,
        goal_mode=False,
        max_turns=21,
    )
    chat_session_database.add(session)
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        forbidden = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": session.session_id,
                "goal_mode": True,
                "max_turns": 1_000_000,
            },
        )
        assert forbidden.status_code == 200
        assert forbidden.json()["message"] == t(ERR_SESSION_NO_PERMISSION)

        await chat_session_database.refresh(session)
        assert session.uid == "user-2"
        assert session.profile_override_id == other_profile.id
        assert (session.goal_mode, session.max_turns) == (False, 21)


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["weixin-openclaw", "other-message-platform"])
@pytest.mark.parametrize(
    ("uid", "is_superuser"),
    [
        pytest.param("user-1", False, id="owner"),
        pytest.param("user-2", True, id="admin"),
    ],
)
async def test_external_session_allows_goal_and_max_turn_updates(
    chat_session_database: AsyncSession,
    source: str,
    uid: str,
    is_superuser: bool,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session = ChatSession(
        session_id=f"external-setting-{source}-{uid}",
        uid="user-1",
        profile_id=primary_profile.id,
        source=source,
        reply_target_source=source,
        goal_mode=True,
        max_turns=3,
    )
    chat_session_database.add(session)
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": uid, "is_superuser": is_superuser}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        goal_disabled = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session.session_id, "goal_mode": False},
        )
        assert goal_disabled.status_code == 200
        assert goal_disabled.json()["code"] == 200
        await chat_session_database.refresh(session)
        assert (session.goal_mode, session.max_turns) == (False, 3)

        max_turns_updated = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session.session_id, "max_turns": 21},
        )
        assert max_turns_updated.status_code == 200
        assert max_turns_updated.json()["code"] == 200
        await chat_session_database.refresh(session)
        assert (session.goal_mode, session.max_turns) == (False, 21)

        goal_enabled = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session.session_id, "goal_mode": True},
        )
        assert goal_enabled.status_code == 200
        assert goal_enabled.json()["code"] == 200
        await chat_session_database.refresh(session)
        assert (session.goal_mode, session.max_turns) == (True, 21)
        assert session.source == source
        assert session.reply_target_source == source
        assert session.profile_id == primary_profile.id

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session.session_id)
        assert listed_session["source"] == source
        assert listed_session["profile_id"] == primary_profile.id
        assert listed_session["goal_mode"] is True
        assert listed_session["max_turns"] == 21


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("setting_field", "invalid_value"),
    [
        pytest.param("max_turns", 0, id="max-zero"),
        pytest.param("max_turns", -1, id="max-negative"),
        pytest.param("max_turns", 1.5, id="max-float"),
        pytest.param("max_turns", True, id="max-bool"),
        pytest.param("max_turns", "2", id="max-string"),
        pytest.param("max_turns", None, id="max-null"),
        pytest.param("max_turns", SESSION_MAX_TURNS_UPPER_BOUND + 1, id="max-above-upper-bound"),
        pytest.param("max_turns", 2**63, id="max-2-to-63"),
        pytest.param("max_turns", 10**100, id="max-10-to-100"),
        pytest.param("goal_mode", 1, id="goal-int"),
        pytest.param("goal_mode", "true", id="goal-string"),
        pytest.param("goal_mode", None, id="goal-null"),
    ],
)
async def test_invalid_session_setting_values_reject_updates_and_new_sessions(
    chat_session_database: AsyncSession,
    setting_field: str,
    invalid_value: object,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    request_id = f"invalid-session-setting-{setting_field}"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "baseline session",
                "profile_override_id": primary_profile.id,
            },
        )
        assert created.status_code == 200
        session_id = created.json()["choices"][0]["message"]["content"]
        persisted = await chat_session_database.get(ChatSession, session_id)
        assert persisted is not None
        assert (persisted.goal_mode, persisted.max_turns) == (True, 5)

        invalid_setting = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session_id, setting_field: invalid_value},
        )
        assert invalid_setting.status_code == 422
        await chat_session_database.refresh(persisted)
        assert (persisted.goal_mode, persisted.max_turns) == (True, 5)

        invalid_new = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "invalid new session",
                "request_id": request_id,
                "profile_override_id": primary_profile.id,
                setting_field: invalid_value,
            },
        )
        assert invalid_new.status_code == 422
        await chat_session_database.refresh(persisted)
        assert (persisted.goal_mode, persisted.max_turns) == (True, 5)
        expected_new_session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"monolight:http:user-1:{request_id}"))
        assert await chat_session_database.get(ChatSession, expected_new_session_id) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "ws", "weixin-openclaw", "other-message-platform"])
async def test_max_turns_upper_bound_persists_across_session_settings(
    chat_session_database: AsyncSession,
    source: str,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "max turns boundary session",
                "profile_override_id": primary_profile.id,
                "goal_mode": False,
                "max_turns": SESSION_MAX_TURNS_UPPER_BOUND,
            },
        )
        assert created.status_code == 200
        created_payload = created.json()
        assert created_payload["choices"][0]["finish_reason"] == "new_session"
        session_id = created_payload["choices"][0]["message"]["content"]

        persisted = await chat_session_database.get(ChatSession, session_id)
        assert persisted is not None
        assert (persisted.goal_mode, persisted.max_turns) == (False, SESSION_MAX_TURNS_UPPER_BOUND)
        persisted.source = source
        persisted.reply_target_source = source
        await chat_session_database.commit()

        enabled = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": session_id,
                "goal_mode": True,
                "max_turns": SESSION_MAX_TURNS_UPPER_BOUND - 1,
            },
        )
        assert enabled.status_code == 200
        assert enabled.json()["code"] == 200
        await chat_session_database.refresh(persisted)
        assert (persisted.goal_mode, persisted.max_turns) == (True, SESSION_MAX_TURNS_UPPER_BOUND - 1)
        assert persisted.source == source
        assert persisted.reply_target_source == source

        restored = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": session_id,
                "goal_mode": False,
                "max_turns": SESSION_MAX_TURNS_UPPER_BOUND,
            },
        )
        assert restored.status_code == 200
        assert restored.json()["code"] == 200
        await chat_session_database.refresh(persisted)
        assert (persisted.goal_mode, persisted.max_turns) == (False, SESSION_MAX_TURNS_UPPER_BOUND)
        assert persisted.source == source
        assert persisted.reply_target_source == source

        invalid = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": session_id,
                "goal_mode": True,
                "max_turns": SESSION_MAX_TURNS_UPPER_BOUND + 1,
            },
        )
        assert invalid.status_code == 422
        await chat_session_database.refresh(persisted)
        assert (persisted.goal_mode, persisted.max_turns) == (False, SESSION_MAX_TURNS_UPPER_BOUND)
        assert persisted.source == source
        assert persisted.reply_target_source == source

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session_id)
        assert listed_session["goal_mode"] is False
        assert listed_session["max_turns"] == SESSION_MAX_TURNS_UPPER_BOUND


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("source", "stream_requested", "expected_show_tool_calls", "expected_stream_requested"),
    [
        pytest.param("http", None, True, False, id="http-none"),
        pytest.param("http", False, True, False, id="http-false"),
        pytest.param("http", True, True, True, id="http-true"),
        pytest.param("ws", None, True, True, id="ws-none"),
        pytest.param("ws", False, True, False, id="ws-false"),
        pytest.param("ws", True, True, True, id="ws-true"),
        pytest.param("weixin-openclaw", None, False, False, id="weixin-openclaw-none"),
        pytest.param("weixin-openclaw", False, False, False, id="weixin-openclaw-false"),
        pytest.param("weixin-openclaw", True, True, True, id="weixin-openclaw-true"),
        pytest.param("other-message-platform", None, False, False, id="other-message-platform-none"),
        pytest.param("other-message-platform", False, False, False, id="other-message-platform-false"),
        pytest.param("other-message-platform", True, True, True, id="other-message-platform-true"),
    ],
)
async def test_session_reply_submission_persists_visibility_defaults_and_manual_override(
    chat_session_database: AsyncSession,
    source: str,
    stream_requested: bool | None,
    expected_show_tool_calls: bool,
    expected_stream_requested: bool,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session_id = f"visibility-{source}-{stream_requested}"

    _first_message, first_work, _first_status, _first_events = await session_reply_queue_manager.submit_user_message(
        chat_session_database,
        uid="user-1",
        session_id=session_id,
        profile=primary_profile,
        message="first message",
        attachments=None,
        source=source,
        stream_requested=stream_requested,
    )

    persisted = await chat_session_database.get(ChatSession, session_id)
    assert persisted is not None
    assert persisted.show_tool_calls is expected_show_tool_calls
    assert first_work.execution_state["stream_requested"] is expected_stream_requested
    assert first_work.execution_state["show_tool_calls"] is expected_show_tool_calls
    assert first_work.execution_state["expose_tool_call_content"] is expected_show_tool_calls

    persisted.show_tool_calls = not expected_show_tool_calls
    chat_session_database.add(persisted)
    await chat_session_database.commit()

    _second_message, second_work, _second_status, _second_events = await session_reply_queue_manager.submit_user_message(
        chat_session_database,
        uid="user-1",
        session_id=session_id,
        profile=primary_profile,
        message="second message",
        attachments=None,
        source=source,
        stream_requested=stream_requested,
    )

    await chat_session_database.refresh(persisted)
    assert persisted.show_tool_calls is (not expected_show_tool_calls)
    assert second_work.execution_state["stream_requested"] is expected_stream_requested
    assert second_work.execution_state["show_tool_calls"] is (not expected_show_tool_calls)
    assert second_work.execution_state["expose_tool_call_content"] is (not expected_show_tool_calls)


@pytest.mark.asyncio
async def test_ws_session_setting_to_http_updates_source_only_after_active_reply_finishes(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    session = ChatSession(
        session_id="ws-transport-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="ws",
        reply_target_source="ws",
    )
    active_work = SessionReplyWorkItem(
        uid="user-1",
        session_id=session.session_id,
        profile_id=primary_profile.id,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="transport-mode-active-work",
        status=SessionReplyWorkStatus.RUNNING,
    )
    chat_session_database.add(session)
    chat_session_database.add(active_work)
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        blocked = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": session.session_id,
                "transport_mode": "http",
            },
        )
        assert blocked.status_code == 200
        assert blocked.json()["code"] == 409
        assert blocked.json()["message"] == t(ERR_SESSION_TRANSPORT_CHANGE_ACTIVE)

        await chat_session_database.refresh(session)
        assert session.source == "ws"

        active_work.status = SessionReplyWorkStatus.SUCCEEDED
        chat_session_database.add(active_work)
        await chat_session_database.commit()

        updated = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": session.session_id,
                "transport_mode": "http",
            },
        )
        assert updated.status_code == 200
        assert updated.json()["code"] == 200

        await chat_session_database.refresh(session)
        assert session.source == "http"

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session.session_id)
        assert listed_session["source"] == "http"


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
                "transport_mode": "http",
            },
        )
        assert read_only.status_code == 200
        assert read_only.json()["code"] == 403
        await chat_session_database.refresh(external_session)
        assert external_session.enable_markdown is True
        assert external_session.source == "weixin-openclaw"

        transport_only = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": external_session.session_id,
                "transport_mode": "http",
            },
        )
        assert transport_only.status_code == 200
        assert transport_only.json()["code"] == 403
        await chat_session_database.refresh(external_session)
        assert external_session.source == "weixin-openclaw"

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


@pytest.mark.asyncio
async def test_http_completion_submission_is_deterministic_idempotent_and_non_streaming(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    _patch_http_submission(monkeypatch, primary_profile)
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    request_id = "http-request-1"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={"message": "hello", "request_id": request_id},
        )
        assert created.status_code == 200
        created_payload = created.json()
        expected_session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"monolight:http:user-1:{request_id}"))
        session_id = created_payload["choices"][0]["message"]["content"]
        assert session_id == expected_session_id
        assert created_payload["choices"][0]["finish_reason"] == "new_session"

        persisted_session = await chat_session_database.get(ChatSession, session_id)
        assert persisted_session is not None
        assert persisted_session.uid == "user-1"
        assert persisted_session.source == "http"

        submission = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "hello from HTTP",
                "session_id": session_id,
                "request_id": request_id,
            },
        )
        assert submission.status_code == 200
        submission_data = submission.json()["data"]
        assert submission_data["session_id"] == session_id
        assert submission_data["request_id"] == request_id
        assert isinstance(submission_data["work_id"], int)
        assert submission_data["submission_status"] == "accepted"

        work_id = submission_data["work_id"]
        work = await chat_session_database.get(SessionReplyWorkItem, work_id)
        assert work is not None
        assert work.status == SessionReplyWorkStatus.READY_FOR_LLM
        assert work.execution_state["stream_requested"] is False
        assert work.execution_state["message_source"] == "http"

        messages = list((await chat_session_database.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
        assert len(messages) == 1
        assert messages[0].content == "hello from HTTP"

        duplicate = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "hello from HTTP",
                "session_id": session_id,
                "request_id": request_id,
            },
        )
        assert duplicate.status_code == 200
        assert duplicate.json()["data"]["work_id"] == work_id
        assert duplicate.json()["data"]["submission_status"] == "accepted"

        works_after_duplicate = list((await chat_session_database.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id))).scalars().all())
        messages_after_duplicate = list((await chat_session_database.execute(select(Message).where(Message.session_id == session_id))).scalars().all())
        assert [item.id for item in works_after_duplicate] == [work_id]
        assert [message.id for message in messages_after_duplicate] == [messages[0].id]

        conflict = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "different body",
                "session_id": session_id,
                "request_id": request_id,
            },
        )
        assert conflict.status_code == 400
        assert conflict.json()["code"] == 400
        assert conflict.json()["message"] == t("ERR_CHAT_REQUEST_ID_CONFLICT")

        stream_rejected = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "stream body",
                "session_id": session_id,
                "request_id": "http-stream-request",
                "stream": True,
            },
        )
        assert stream_rejected.status_code == 200
        assert stream_rejected.json()["code"] == 400
        assert stream_rejected.json()["message"] == t("ERR_CHAT_STREAM_NOT_SUPPORTED")

        await chat_session_database.delete(work)
        await chat_session_database.commit()

        unavailable = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "hello from HTTP",
                "session_id": session_id,
                "request_id": request_id,
            },
        )
        assert unavailable.status_code == 400
        assert unavailable.json()["code"] == 400
        assert unavailable.json()["message"] == t("ERR_CHAT_REQUEST_WORK_UNAVAILABLE")

        final_messages = list((await chat_session_database.execute(select(Message).where(Message.session_id == session_id))).scalars().all())
        final_works = list((await chat_session_database.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.session_id == session_id))).scalars().all())
        assert len(final_messages) == 1
        assert len(final_works) == 0


@pytest.mark.asyncio
async def test_http_session_recovers_in_progress_work_through_list_and_status(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    _patch_http_submission(monkeypatch, primary_profile)
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={"message": "create status session", "request_id": "status-session-request"},
        )
        session_id = created.json()["choices"][0]["message"]["content"]
        active_submission = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "active work",
                "session_id": session_id,
                "request_id": "active-request",
            },
        )
        active_work_id = active_submission.json()["data"]["work_id"]

        input_message = (await chat_session_database.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id.desc()))).scalars().first()
        assert input_message is not None and input_message.id is not None

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_data = listed.json()["data"]
        assert len(listed_data) == 1
        session_row = listed_data[0]
        assert session_row["session_id"] == session_id
        assert session_row["uid"] == "user-1"
        assert session_row["username"] == "alice"
        assert session_row["latest_message_id"] == input_message.id
        assert session_row["is_loading"] is True
        assert session_row["reply_works"] == [
            {
                "work_id": active_work_id,
                "status": SessionReplyWorkStatus.READY_FOR_LLM.value,
                "request_ids": ["active-request"],
                "result_message_id": None,
                "merged_into_id": None,
                "sequence_no": 1,
            }
        ]

        active = await client.get(f"/api/v1/chat/reply-works/{active_work_id}")
        assert active.status_code == 200
        active_data = active.json()["data"]
        assert active_data["status"] == SessionReplyWorkStatus.READY_FOR_LLM.value
        assert active_data["response"] is None
        assert active_data["error"] is None

        active_work = await chat_session_database.get(SessionReplyWorkItem, active_work_id)
        assert active_work is not None
        success_message = Message(
            session_id=session_id,
            uid="user-1",
            profile_id=primary_profile.id,
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content="completed reply",
            is_processed=True,
        )
        chat_session_database.add(success_message)
        await chat_session_database.flush()
        assert success_message.id is not None
        active_work.status = SessionReplyWorkStatus.SUCCEEDED
        active_work.result_message_id = success_message.id
        active_work.execution_state = {
            **active_work.execution_state,
            "response": {"content": "completed reply", "history": []},
        }
        await chat_session_database.commit()

        completed_list = await client.get("/api/v1/chat/sessions/list")
        assert completed_list.status_code == 200
        completed_session = completed_list.json()["data"][0]
        assert completed_session["is_loading"] is False
        assert completed_session["latest_message_id"] == success_message.id
        assert completed_session["reply_works"][0]["work_id"] == active_work_id
        assert completed_session["reply_works"][0]["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert completed_session["reply_works"][0]["result_message_id"] == success_message.id

        succeeded = await client.get(f"/api/v1/chat/reply-works/{active_work_id}")
        assert succeeded.status_code == 200
        succeeded_data = succeeded.json()["data"]
        assert succeeded_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert succeeded_data["result_message_id"] == success_message.id
        assert succeeded_data["response"]["content"] == "completed reply"
        assert succeeded_data["response"]["work_id"] == active_work_id
        assert succeeded_data["response"]["session_todo"] == {"revision": 0, "todos": []}
        assert succeeded_data["error"] is None

        failed_submission = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "failed work",
                "session_id": session_id,
                "request_id": "failed-request",
            },
        )
        failed_work_id = failed_submission.json()["data"]["work_id"]
        failed_work = await chat_session_database.get(SessionReplyWorkItem, failed_work_id)
        assert failed_work is not None
        failure_message = Message(
            session_id=session_id,
            uid="user-1",
            profile_id=primary_profile.id,
            role=MessageRole.ERR,
            type=MessageType.TEXT,
            content="worker failed",
            is_processed=True,
        )
        chat_session_database.add(failure_message)
        await chat_session_database.flush()
        assert failure_message.id is not None
        failed_work.status = SessionReplyWorkStatus.FAILED
        failed_work.result_message_id = failure_message.id
        await chat_session_database.commit()

        failed = await client.get(f"/api/v1/chat/reply-works/{failed_work_id}")
        assert failed.status_code == 200
        failed_data = failed.json()["data"]
        assert failed_data["status"] == SessionReplyWorkStatus.FAILED.value
        assert failed_data["result_message_id"] == failure_message.id
        assert failed_data["response"] is None
        assert failed_data["error"] == "worker failed"

        auth_state["uid"] = "user-2"
        other_owner = await client.get("/api/v1/chat/sessions/list")
        assert other_owner.status_code == 200
        assert other_owner.json()["data"] == []

        forbidden = await client.get(f"/api/v1/chat/reply-works/{active_work_id}")
        assert forbidden.status_code == 404
        assert forbidden.json()["code"] == 404
        assert forbidden.json()["message"] == t("ERR_SESSION_REPLY_WORK_NOT_FOUND")


@pytest.mark.asyncio
async def test_http_reply_work_status_resolves_merged_work_and_enforces_owner(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session = ChatSession(
        session_id="merged-status-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    target_work = SessionReplyWorkItem(
        uid="user-1",
        session_id=session.session_id,
        profile_id=primary_profile.id,
        sequence_no=2,
        work_type="foreground_reply",
        source_type="user_message",
        source_id="target-source",
        dedupe_key="merged-target-work",
        status=SessionReplyWorkStatus.SUCCEEDED,
        execution_state={
            "message_source": "http",
            "request_ids": ["one", "two"],
            "response": {"content": "joined", "history": []},
        },
    )
    chat_session_database.add_all([session, target_work])
    await chat_session_database.flush()
    assert target_work.id is not None
    original_work = SessionReplyWorkItem(
        uid="user-1",
        session_id=session.session_id,
        profile_id=primary_profile.id,
        sequence_no=1,
        work_type="foreground_reply",
        source_type="user_message",
        source_id="original-source",
        dedupe_key="merged-original-work",
        status=SessionReplyWorkStatus.MERGED,
        merged_into_id=target_work.id,
    )
    chat_session_database.add(original_work)
    await chat_session_database.commit()
    await chat_session_database.refresh(original_work)
    assert original_work.id is not None

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        resolved = await client.get(f"/api/v1/chat/reply-works/{original_work.id}")
        assert resolved.status_code == 200
        resolved_data = resolved.json()["data"]
        assert resolved_data["work_id"] == original_work.id
        assert resolved_data["resolved_work_id"] == target_work.id
        assert resolved_data["status"] == SessionReplyWorkStatus.SUCCEEDED.value
        assert resolved_data["request_ids"] == ["one", "two"]
        assert resolved_data["response"]["content"] == "joined"
        assert resolved_data["response"]["history"] == []
        assert resolved_data["response"]["work_id"] == target_work.id

        auth_state["uid"] = "user-2"
        forbidden = await client.get(f"/api/v1/chat/reply-works/{original_work.id}")
        assert forbidden.status_code == 404
        assert forbidden.json()["code"] == 404
        assert forbidden.json()["message"] == t("ERR_SESSION_REPLY_WORK_NOT_FOUND")


@pytest.mark.asyncio
async def test_session_history_supports_forward_id_cursor_for_incremental_recovery(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session = ChatSession(
        session_id="incremental-history-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
        show_tool_calls=True,
    )
    chat_session_database.add(session)
    await chat_session_database.flush()

    messages = []
    for index in range(45):
        message = Message(
            session_id=session.session_id,
            uid="user-1",
            role=MessageRole.ASSISTANT,
            type=MessageType.TEXT,
            content=f"message-{index + 1}",
            profile_id=primary_profile.id,
            is_processed=True,
        )
        chat_session_database.add(message)
        messages.append(message)
    await chat_session_database.commit()
    first_message_id = messages[0].id
    assert first_message_id is not None

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        legacy_page = await client.get(
            "/api/v1/chat/sessions/history",
            params={
                "session_id": session.session_id,
                "page": 1,
                "size": 20,
            },
        )
        assert legacy_page.status_code == 200
        assert [item["id"] for item in legacy_page.json()["data"]] == [message.id for message in messages[25:45]]

        first_page = await client.get(
            "/api/v1/chat/sessions/history",
            params={
                "session_id": session.session_id,
                "after_id": first_message_id,
                "size": 20,
            },
        )
        assert first_page.status_code == 200
        first_page_data = first_page.json()["data"]
        assert [item["id"] for item in first_page_data] == [message.id for message in messages[1:21]]

        second_page = await client.get(
            "/api/v1/chat/sessions/history",
            params={
                "session_id": session.session_id,
                "after_id": first_page_data[-1]["id"],
                "size": 20,
            },
        )
        assert [item["id"] for item in second_page.json()["data"]] == [message.id for message in messages[21:41]]

        third_page = await client.get(
            "/api/v1/chat/sessions/history",
            params={
                "session_id": session.session_id,
                "after_id": second_page.json()["data"][-1]["id"],
                "size": 20,
            },
        )
        assert [item["id"] for item in third_page.json()["data"]] == [message.id for message in messages[41:45]]


@pytest.mark.asyncio
async def test_background_task_pending_activity_is_not_limited_by_task_history_page(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session = ChatSession(
        session_id="background-activity-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="http",
        reply_target_source="http",
    )
    chat_session_database.add(session)
    base_time = get_local_time() - timedelta(minutes=5)
    pending_task = BackgroundTask(
        uid="user-1",
        session_id=session.session_id,
        profile_id=primary_profile.id,
        tool_call_id="pending-task",
        tool_name="test_tool",
        status=BackgroundTaskStatus.RUNNING,
        arguments={},
        auto_reply=True,
        reply_status=BackgroundTaskReplyStatus.PENDING,
        created_at=base_time,
    )
    chat_session_database.add(pending_task)
    for index in range(25):
        chat_session_database.add(
            BackgroundTask(
                uid="user-1",
                session_id=session.session_id,
                profile_id=primary_profile.id,
                tool_call_id=f"finished-{index}",
                tool_name="test_tool",
                status=BackgroundTaskStatus.SUCCEEDED,
                arguments={},
                result={"ok": True},
                auto_reply=True,
                reply_status=BackgroundTaskReplyStatus.SUCCEEDED,
                created_at=base_time + timedelta(seconds=index + 1),
            )
        )
    await chat_session_database.commit()
    await chat_session_database.refresh(pending_task)

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        first_page = await client.get(
            "/api/v1/chat/background-tasks",
            params={"session_id": session.session_id, "page": 1, "size": 20},
        )
        assert first_page.status_code == 200
        assert pending_task.id not in {item["id"] for item in first_page.json()["data"]}

        active = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": session.session_id},
        )
        assert active.status_code == 200
        assert active.json()["data"] == {"has_pending_activity": True}

        pending_task.status = BackgroundTaskStatus.SUCCEEDED
        pending_task.reply_status = BackgroundTaskReplyStatus.RUNNING
        chat_session_database.add(pending_task)
        await chat_session_database.commit()

        pending_reply = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": session.session_id},
        )
        assert pending_reply.json()["data"] == {"has_pending_activity": True}

        pending_task.reply_status = BackgroundTaskReplyStatus.SUCCEEDED
        chat_session_database.add(pending_task)
        await chat_session_database.commit()

        completed = await client.get(
            "/api/v1/chat/background-tasks/pending-activity",
            params={"session_id": session.session_id},
        )
        assert completed.json()["data"] == {"has_pending_activity": False}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "ws"])
@pytest.mark.parametrize("work_type", list(SessionReplyWorkType))
@pytest.mark.parametrize("status", list(SessionReplyWorkStatus))
async def test_session_list_loading_and_reply_running_follow_work_type_and_status(
    chat_session_database: AsyncSession,
    source: str,
    work_type: SessionReplyWorkType,
    status: SessionReplyWorkStatus,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    source_type_by_work_type = {
        SessionReplyWorkType.FOREGROUND_REPLY: SessionReplySourceType.USER_MESSAGE,
        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION: SessionReplySourceType.AUDIT_RECORD,
        SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY: SessionReplySourceType.BACKGROUND_TASK,
        SessionReplyWorkType.SCHEDULED_TASK_SUMMARY: SessionReplySourceType.SCHEDULED_TASK_RUN,
    }
    interactive_work_types = {
        SessionReplyWorkType.FOREGROUND_REPLY,
        SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
    }
    session_id = f"session-list-state-{source}-{work_type.value}-{status.value}"
    work = SessionReplyWorkItem(
        uid="user-1",
        session_id=session_id,
        profile_id=primary_profile.id,
        sequence_no=1,
        work_type=work_type,
        source_type=source_type_by_work_type[work_type],
        source_id="source-1",
        dedupe_key=f"{session_id}-work",
        status=status,
        execution_state={"message_source": source, "request_ids": [f"{session_id}-request"]},
    )
    chat_session_database.add_all(
        [
            ChatSession(
                session_id=session_id,
                uid="user-1",
                profile_id=primary_profile.id,
                source=source,
                reply_target_source=source,
            ),
            work,
        ]
    )
    await chat_session_database.commit()

    is_active = status in SESSION_REPLY_ACTIVE_STATUSES
    is_interactive = work_type in interactive_work_types
    should_cancel = is_active and is_interactive
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session_id)
        assert listed_session["is_loading"] is is_active
        assert listed_session["is_reply_running"] is (is_active and is_interactive)

        stopped = await client.post(
            "/api/v1/chat/sessions/stop",
            params={"session_id": session_id},
        )
        assert stopped.status_code == 200
        assert stopped.json()["code"] == 200
        assert stopped.json()["data"] == {
            "session_id": session_id,
            "cancelled_count": int(should_cancel),
        }

        await chat_session_database.refresh(work)
        assert work.status == (SessionReplyWorkStatus.CANCELLED if should_cancel else status)

        listed_after_stop = await client.get("/api/v1/chat/sessions/list")
        assert listed_after_stop.status_code == 200
        listed_session_after_stop = next(item for item in listed_after_stop.json()["data"] if item["session_id"] == session_id)
        assert listed_session_after_stop["is_reply_running"] is False
        assert listed_session_after_stop["is_loading"] is (is_active and not should_cancel)


@pytest.mark.asyncio
async def test_session_reasoning_options_filter_candidates_and_enforce_owner_scope(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, alternate_profile, other_profile = await _seed_profiles(chat_session_database)
    primary_profile_id = primary_profile.id
    alternate_profile_id = alternate_profile.id
    other_profile_id = other_profile.id
    assert primary_profile_id is not None
    assert alternate_profile_id is not None
    assert other_profile_id is not None

    channel_id = primary_profile.configs["channel"]["chat_channel"]["rules"][0]["channel_id"]
    channel = await chat_session_database.get(ModelChannel, channel_id)
    assert channel is not None

    def chat_model(
        model_id: str,
        *,
        reasoning_efforts: list[str] | None = None,
        legacy_reasoning_effort: str | None = None,
        is_enabled: bool = True,
    ) -> dict[str, object]:
        entry: dict[str, object] = {
            "model_id": model_id,
            "usage": "CHAT",
            "protocol": "OPENAI",
            "context_window_k": 64,
            "max_tokens": 4096,
            "is_enabled": is_enabled,
        }
        if reasoning_efforts is not None:
            entry["reasoning_efforts"] = reasoning_efforts
        if legacy_reasoning_effort is not None:
            entry["reasoning_effort"] = legacy_reasoning_effort
        return entry

    channel.model_ids = [
        chat_model("chat-model", reasoning_efforts=["low", " custom-tier ", "low"]),
        chat_model("legacy-model", legacy_reasoning_effort=" legacy-tier "),
        chat_model("disabled-model", reasoning_efforts=["disabled-model"], is_enabled=False),
        chat_model("rule-disabled-model", reasoning_efforts=["disabled-rule"]),
        chat_model("zero-weight-model", reasoning_efforts=["zero-weight"]),
    ]
    inactive_channel = ModelChannel(
        name="chat-session-inactive-channel",
        api_key="enc:v1:chat-session-inactive-key",
        base_url="https://chat-session-inactive.example.com/v1",
        is_active=False,
        model_ids=[chat_model("inactive-model", reasoning_efforts=["inactive-channel"])],
    )
    chat_session_database.add(inactive_channel)
    await chat_session_database.flush()
    assert inactive_channel.id is not None

    primary_config = _profile_config(channel_id)
    primary_config["channel"]["chat_channel"]["rules"] = [
        {
            "channel_id": channel_id,
            "model_id": "chat-model",
            "priority": 1,
            "weight": 1,
            "is_enabled": True,
        },
        {
            "channel_id": channel_id,
            "model_id": "disabled-model",
            "priority": 2,
            "weight": 1,
            "is_enabled": True,
        },
        {
            "channel_id": channel_id,
            "model_id": "rule-disabled-model",
            "priority": 3,
            "weight": 1,
            "is_enabled": False,
        },
        {
            "channel_id": channel_id,
            "model_id": "zero-weight-model",
            "priority": 4,
            "weight": 0,
            "is_enabled": True,
        },
        {
            "channel_id": inactive_channel.id,
            "model_id": "inactive-model",
            "priority": 5,
            "weight": 1,
            "is_enabled": True,
        },
    ]
    alternate_config = _profile_config(channel_id)
    alternate_config["channel"]["chat_channel"]["rules"] = [
        {
            "channel_id": channel_id,
            "model_id": "legacy-model",
            "reasoning_effort": " explicit-default ",
            "priority": 1,
            "weight": 1,
            "is_enabled": True,
        }
    ]
    primary_profile.configs = primary_config
    alternate_profile.configs = alternate_config
    chat_session_database.add_all([channel, primary_profile, alternate_profile])
    await chat_session_database.commit()

    owner_session = ChatSession(
        session_id="reasoning-options-owner-session",
        uid="user-1",
        profile_id=alternate_profile_id,
        source="weixin-openclaw",
        reply_target_source="weixin-openclaw",
    )
    chat_session_database.add(owner_session)
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        primary_options = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"profile_override_id": primary_profile_id},
        )
        assert primary_options.status_code == 200
        assert primary_options.json()["data"] == {
            "profile_id": primary_profile_id,
            "options": ["low", "custom-tier"],
            "defaults": [None],
        }

        alternate_options = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"profile_override_id": alternate_profile_id},
        )
        assert alternate_options.status_code == 200
        assert alternate_options.json()["data"] == {
            "profile_id": alternate_profile_id,
            "options": ["legacy-tier", "explicit-default"],
            "defaults": ["explicit-default"],
        }

        forbidden_profile = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"profile_override_id": other_profile_id},
        )
        assert forbidden_profile.status_code == 404
        assert forbidden_profile.json()["code"] == 404

        owner_options = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"session_id": owner_session.session_id},
        )
        assert owner_options.status_code == 200
        assert owner_options.json()["data"]["profile_id"] == alternate_profile_id

        overridden = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": owner_session.session_id,
                "profile_override_id": primary_profile_id,
            },
        )
        assert overridden.status_code == 200
        assert overridden.json()["code"] == 200

        overridden_options = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"session_id": owner_session.session_id},
        )
        assert overridden_options.status_code == 200
        assert overridden_options.json()["data"] == {
            "profile_id": primary_profile_id,
            "options": ["low", "custom-tier"],
            "defaults": [None],
        }

        cleared_override = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": owner_session.session_id,
                "profile_override_id": None,
            },
        )
        assert cleared_override.status_code == 200
        assert cleared_override.json()["code"] == 200

        restored_options = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"session_id": owner_session.session_id},
        )
        assert restored_options.status_code == 200
        assert restored_options.json()["data"] == {
            "profile_id": alternate_profile_id,
            "options": ["legacy-tier", "explicit-default"],
            "defaults": ["explicit-default"],
        }

        auth_state["uid"] = "user-2"
        forbidden_session = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"session_id": owner_session.session_id},
        )
        assert forbidden_session.status_code == 200
        assert forbidden_session.json()["code"] == 403
        assert forbidden_session.json()["message"] == t(ERR_SESSION_NO_PERMISSION)

        auth_state.update({"uid": "admin", "is_superuser": True})
        admin_options = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={
                "session_id": owner_session.session_id,
                "profile_override_id": alternate_profile_id,
            },
        )
        assert admin_options.status_code == 200
        assert admin_options.json()["data"] == {
            "profile_id": alternate_profile_id,
            "options": ["legacy-tier", "explicit-default"],
            "defaults": ["explicit-default"],
        }


@pytest.mark.asyncio
async def test_session_reasoning_effort_persists_through_http_settings_and_external_sessions(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "reasoning session",
                "profile_override_id": primary_profile.id,
                "reasoning_effort": " custom-tier ",
            },
        )
        assert created.status_code == 200
        session_id = created.json()["choices"][0]["message"]["content"]

        persisted = await chat_session_database.get(ChatSession, session_id)
        assert persisted is not None
        assert persisted.reasoning_effort == "custom-tier"

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session_id)
        assert listed_session["reasoning_effort"] == "custom-tier"

        omitted = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session_id, "show_reasoning": False},
        )
        assert omitted.status_code == 200
        assert omitted.json()["code"] == 200
        await chat_session_database.refresh(persisted)
        assert persisted.reasoning_effort == "custom-tier"
        assert persisted.show_reasoning is False

        cleared = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session_id, "reasoning_effort": None},
        )
        assert cleared.status_code == 200
        assert cleared.json()["code"] == 200
        await chat_session_database.refresh(persisted)
        assert persisted.reasoning_effort is None

        listed_after_clear = await client.get("/api/v1/chat/sessions/list")
        assert listed_after_clear.status_code == 200
        listed_after_clear_session = next(item for item in listed_after_clear.json()["data"] if item["session_id"] == session_id)
        assert listed_after_clear_session["reasoning_effort"] is None

        auth_state["uid"] = "user-2"
        forbidden = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session_id, "reasoning_effort": "other-tier"},
        )
        assert forbidden.status_code == 200
        assert forbidden.json()["code"] == 500
        assert forbidden.json()["message"] == t(ERR_SESSION_NO_PERMISSION)
        await chat_session_database.refresh(persisted)
        assert persisted.reasoning_effort is None

        auth_state["uid"] = "user-1"
        external_session = ChatSession(
            session_id="reasoning-external-session",
            uid="user-1",
            profile_id=primary_profile.id,
            source="weixin-openclaw",
            reply_target_source="weixin-openclaw",
        )
        chat_session_database.add(external_session)
        await chat_session_database.commit()

        external_updated = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": external_session.session_id,
                "reasoning_effort": " external-tier ",
            },
        )
        assert external_updated.status_code == 200
        assert external_updated.json()["code"] == 200
        await chat_session_database.refresh(external_session)
        assert external_session.reasoning_effort == "external-tier"


@pytest.mark.asyncio
async def test_reasoning_options_use_default_and_external_profile_or_return_empty(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    channel_id = primary_profile.configs["channel"]["chat_channel"]["rules"][0]["channel_id"]
    channel = await chat_session_database.get(ModelChannel, channel_id)
    assert channel is not None
    channel.model_ids = [
        {
            **channel.model_ids[0],
            "reasoning_efforts": ["default-tier"],
        }
    ]
    primary_profile.is_default = True
    chat_session_database.add_all([channel, primary_profile])

    empty_profile_session = ChatSession(
        session_id="reasoning-empty-profile-session",
        uid="user-2",
        source="weixin-openclaw",
        reply_target_source="weixin-openclaw",
    )
    external_profile_session = ChatSession(
        session_id="reasoning-external-profile-session",
        uid="user-1",
        profile_id=primary_profile.id,
        source="other-message-platform",
        reply_target_source="other-message-platform",
    )
    chat_session_database.add_all([empty_profile_session, external_profile_session])
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        default_without_context = await client.get("/api/v1/chat/sessions/reasoning-options")
        assert default_without_context.status_code == 200
        assert default_without_context.json()["data"] == {
            "profile_id": primary_profile.id,
            "options": ["default-tier"],
            "defaults": [None],
        }

        created = await client.post(
            "/api/v1/chat/completions",
            json={"message": "default profile session"},
        )
        assert created.status_code == 200
        new_session_id = created.json()["choices"][0]["message"]["content"]
        new_session = await chat_session_database.get(ChatSession, new_session_id)
        assert new_session is not None
        assert new_session.profile_override_id is None

        default_options = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"session_id": new_session_id},
        )
        assert default_options.status_code == 200
        assert default_options.json()["data"] == {
            "profile_id": primary_profile.id,
            "options": ["default-tier"],
            "defaults": [None],
        }

        external_options = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"session_id": external_profile_session.session_id},
        )
        assert external_options.status_code == 200
        assert external_options.json()["data"]["profile_id"] == primary_profile.id
        assert external_options.json()["data"]["options"] == ["default-tier"]

        auth_state["uid"] = "user-2"
        empty_options = await client.get(
            "/api/v1/chat/sessions/reasoning-options",
            params={"session_id": empty_profile_session.session_id},
        )
        assert empty_options.status_code == 200
        assert empty_options.json()["data"] == {
            "profile_id": None,
            "options": [],
            "defaults": [],
        }


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("invalid_value", "case_id"),
    [
        pytest.param("x" * 65, "too-long", id="too-long"),
        pytest.param(123, "integer", id="integer"),
        pytest.param(True, "boolean", id="boolean"),
    ],
)
async def test_reasoning_effort_validation_rejects_oversized_or_non_string_values(
    chat_session_database: AsyncSession,
    invalid_value: object,
    case_id: str,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        created = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "validation baseline",
                "profile_override_id": primary_profile.id,
                "reasoning_effort": "valid-tier",
            },
        )
        assert created.status_code == 200
        session_id = created.json()["choices"][0]["message"]["content"]
        persisted = await chat_session_database.get(ChatSession, session_id)
        assert persisted is not None
        assert persisted.reasoning_effort == "valid-tier"

        invalid_setting = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session_id, "reasoning_effort": invalid_value},
        )
        assert invalid_setting.status_code == 422
        assert invalid_setting.json()["code"] == 422
        await chat_session_database.refresh(persisted)
        assert persisted.reasoning_effort == "valid-tier"

        request_id = f"invalid-reasoning-{case_id}"
        invalid_new = await client.post(
            "/api/v1/chat/completions",
            json={
                "message": "invalid new reasoning session",
                "request_id": request_id,
                "profile_override_id": primary_profile.id,
                "reasoning_effort": invalid_value,
            },
        )
        assert invalid_new.status_code == 422
        assert invalid_new.json()["code"] == 422
        expected_new_session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"monolight:http:user-1:{request_id}"))
        assert await chat_session_database.get(ChatSession, expected_new_session_id) is None
