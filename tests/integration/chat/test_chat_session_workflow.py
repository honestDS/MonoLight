import uuid
from collections.abc import AsyncGenerator
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
from app.handler import register_handlers
from app.models.audit import AuditConfirmationClaim, AuditRecord
from app.models.background_task import BackgroundTask
from app.models.channel import ModelChannel
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.session_todo import SessionTodoPlan
from app.models.terminal_session import TerminalControlCommand, TerminalSession
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
            SessionReplyStreamEvent.__table__,
            BackgroundTask.__table__,
            TerminalSession.__table__,
            TerminalControlCommand.__table__,
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
@pytest.mark.parametrize(
    ("uid", "is_superuser"),
    [
        pytest.param("user-1", False, id="owner"),
        pytest.param("user-2", True, id="superuser"),
    ],
)
async def test_external_session_lifecycle_persists_guidance_allows_profile_override_and_blocks_web_only_setting(
    chat_session_database: AsyncSession,
    uid: str,
    is_superuser: bool,
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

    auth_state: dict[str, object] = {"uid": uid, "is_superuser": is_superuser}
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
        assert guidance_payload["data"]["uid"] == "user-1"
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
        assert stored_guidance[0].uid == "user-1"
        assert stored_guidance[0].role == MessageRole.SYSTEM
        assert stored_guidance[0].profile_id == primary_profile.id
        assert external_session.uid == "user-1"

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

        setting = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": external_session.session_id,
                "enable_markdown": False,
                "stream_llm_request": True,
            },
        )
        assert setting.status_code == 200
        assert setting.json()["code"] == 200
        await chat_session_database.refresh(external_session)
        assert external_session.enable_markdown is False
        assert external_session.stream_llm_request is True
        assert external_session.source == "weixin-openclaw"
        assert external_session.reply_target_source == "weixin-openclaw"

        stream_disabled = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": external_session.session_id,
                "stream_llm_request": False,
            },
        )
        assert stream_disabled.status_code == 200
        assert stream_disabled.json()["code"] == 200
        await chat_session_database.refresh(external_session)
        assert external_session.stream_llm_request is False

        read_only = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": external_session.session_id,
                "enable_markdown": True,
                "transport_mode": "http",
            },
        )
        assert read_only.status_code == 200
        assert read_only.json()["code"] == 403
        await chat_session_database.refresh(external_session)
        assert external_session.enable_markdown is False
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
async def test_external_session_guidance_rejects_other_user_and_allows_superuser(
    chat_session_database: AsyncSession,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    external_session = ChatSession(
        session_id="external-guidance-permission-session",
        uid="user-1",
        profile_id=None,
        source="weixin-openclaw",
        reply_target_source="weixin-openclaw",
    )
    owner_history = Message(
        session_id=external_session.session_id,
        uid="user-1",
        profile_id=primary_profile.id,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="owner history",
        is_processed=True,
    )
    chat_session_database.add(external_session)
    await chat_session_database.flush()
    chat_session_database.add(owner_history)
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": "user-2", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    guidance_request = {
        "session_id": external_session.session_id,
        "content": "use the owner profile",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        messages_before = list((await chat_session_database.execute(select(Message).where(Message.session_id == external_session.session_id))).scalars().all())
        forbidden = await client.post(
            "/api/v1/chat/sessions/guidance",
            json=guidance_request,
        )
        assert forbidden.status_code == 200
        assert forbidden.json()["code"] == 403

        messages_after_forbidden = list((await chat_session_database.execute(select(Message).where(Message.session_id == external_session.session_id))).scalars().all())
        assert len(messages_after_forbidden) == len(messages_before)

        auth_state["is_superuser"] = True
        guidance = await client.post(
            "/api/v1/chat/sessions/guidance",
            json=guidance_request,
        )
        assert guidance.status_code == 200
        guidance_payload = guidance.json()
        assert guidance_payload["code"] == 200
        assert guidance_payload["data"]["uid"] == "user-1"
        assert guidance_payload["data"]["profile_id"] == primary_profile.id

        await chat_session_database.refresh(external_session)
        assert external_session.uid == "user-1"
        assert external_session.profile_id is None

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
        assert stored_guidance[0].uid == "user-1"
        assert stored_guidance[0].profile_id == primary_profile.id


@pytest.mark.asyncio
async def test_removed_reasoning_options_endpoint_returns_not_found(
    chat_session_database: AsyncSession,
) -> None:
    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        for params in (None, {"session_id": "missing-session"}, {"profile_override_id": 1}):
            response = await client.get("/api/v1/chat/sessions/reasoning-options", params=params)
            assert response.status_code == 404


@pytest.mark.asyncio
async def test_http_new_session_ignores_legacy_reasoning_effort(
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
                "message": "legacy reasoning field",
                "profile_override_id": primary_profile.id,
                "show_reasoning": False,
                "reasoning_effort": "custom-tier",
            },
        )
        assert created.status_code == 200
        session_id = created.json()["choices"][0]["message"]["content"]

        persisted = await chat_session_database.get(ChatSession, session_id)
        assert persisted is not None
        assert persisted.profile_override_id == primary_profile.id
        assert persisted.show_reasoning is False

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session_id)
        assert listed_session["show_reasoning"] is False
        assert "reasoning_effort" not in listed_session


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "ws", "weixin-openclaw"])
@pytest.mark.parametrize("legacy_value", ["high", None, "x" * 65, 123, True])
async def test_session_setting_rejects_legacy_reasoning_effort_and_persists_show_reasoning(
    chat_session_database: AsyncSession,
    source: str,
    legacy_value: object,
) -> None:
    primary_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert primary_profile.id is not None
    session = ChatSession(
        session_id=f"legacy-reasoning-setting-{source}",
        uid="user-1",
        profile_id=primary_profile.id,
        source=source,
        reply_target_source=source,
        show_reasoning=True,
    )
    chat_session_database.add(session)
    await chat_session_database.commit()

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        rejected = await client.post(
            "/api/v1/chat/sessions/setting",
            json={
                "session_id": session.session_id,
                "reasoning_effort": legacy_value,
                "show_reasoning": False,
            },
        )
        assert rejected.status_code == 422
        await chat_session_database.refresh(session)
        assert session.show_reasoning is True

        updated = await client.post(
            "/api/v1/chat/sessions/setting",
            json={"session_id": session.session_id, "show_reasoning": False},
        )
        assert updated.status_code == 200
        assert updated.json()["code"] == 200
        await chat_session_database.refresh(session)
        assert session.show_reasoning is False

        listed = await client.get("/api/v1/chat/sessions/list")
        assert listed.status_code == 200
        listed_session = next(item for item in listed.json()["data"] if item["session_id"] == session.session_id)
        assert listed_session["show_reasoning"] is False
        assert "reasoning_effort" not in listed_session
