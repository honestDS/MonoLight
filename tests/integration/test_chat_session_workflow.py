import uuid
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
    ERR_SESSION_TRANSPORT_CHANGE_ACTIVE,
    GUIDANCE_MESSAGE_PREFIX,
    GUIDANCE_MESSAGE_SUFFIX,
)
from app.core.i18n import t
from app.core.security import get_current_user
from app.handler import register_handlers
from app.models.audit import AuditConfirmationClaim, AuditRecord
from app.models.channel import ModelChannel
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.models.session_todo import SessionTodoPlan
from app.models.user import User
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
                    SessionTodoPlan.__table__,
                    Message.__table__,
                    User.__table__,
                    AuditRecord.__table__,
                    AuditConfirmationClaim.__table__,
                    SessionReplySequence.__table__,
                    SessionReplyWorkItem.__table__,
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
        assert forbidden.json()["code"] == 403
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
