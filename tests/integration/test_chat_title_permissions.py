import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.utils.session as session_utils
from app.core.constants import ERR_SESSION_NO_PERMISSION, ERR_SESSION_READ_ONLY
from app.core.i18n import t
from app.models.channel import ModelChannel
from app.models.message import InternalMessage, InternalResponse, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.providers.llm.client import LLMClient
from tests.integration.test_chat_session_workflow import (
    _build_app,
    _seed_profiles,
    chat_session_database,
)

__all__ = ["chat_session_database"]


def _patch_title_generation(
    monkeypatch: pytest.MonkeyPatch,
    db: AsyncSession,
) -> list[dict[str, object]]:
    calls: list[dict[str, object]] = []

    async def fake_generate(*_args: object, **kwargs: object) -> InternalResponse:
        calls.append(kwargs)
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="owner generated title"),
            model="chat-model",
        )

    monkeypatch.setattr(LLMClient, "generate", fake_generate)
    monkeypatch.setattr(ModelChannel, "get_decrypted_api_key", lambda _channel: "test-api-key")
    monkeypatch.setattr(
        session_utils,
        "AsyncSessionLocal",
        async_sessionmaker(bind=db.bind, expire_on_commit=False),
    )
    return calls


async def _message_snapshot(db: AsyncSession, session_id: str) -> list[tuple[int | None, str, str | None, int]]:
    messages = list((await db.execute(select(Message).where(Message.session_id == session_id).order_by(Message.id))).scalars().all())
    return [(message.id, message.uid, message.content, message.profile_id) for message in messages]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "ws"])
async def test_superuser_generates_title_for_session_owner(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    owner_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert owner_profile.id is not None

    chat_session = ChatSession(
        session_id=f"title-permission-{source}",
        uid="user-1",
        profile_override_id=owner_profile.id,
        source=source,
        reply_target_source=source,
        title="before-title",
    )
    initial_message = Message(
        session_id=chat_session.session_id,
        uid="user-1",
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="before-message",
        profile_id=owner_profile.id,
        is_processed=True,
    )
    admin_default_profile = Profile(
        uid="admin",
        name=f"admin-title-default-{source}",
        configs={"channel": {"chat_channel": {"rules": []}}},
        is_default=True,
    )
    chat_session_database.add(chat_session)
    await chat_session_database.flush()
    chat_session_database.add_all([initial_message, admin_default_profile])
    await chat_session_database.commit()
    messages_before = await _message_snapshot(chat_session_database, chat_session.session_id)

    auth_state: dict[str, object] = {"uid": "admin", "is_superuser": True}
    app = _build_app(chat_session_database, auth_state)
    model_calls = _patch_title_generation(monkeypatch, chat_session_database)
    request = {
        "session_id": chat_session.session_id,
        "first_message": "owner first message",
    }

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        generated = await client.post("/api/v1/chat/sessions/generate-title", json=request)
        assert generated.status_code == 200
        assert generated.json()["code"] == 200
        assert generated.json()["data"] == {"title": "owner generated title"}
        assert len(model_calls) == 1
        assert model_calls[0]["api_key"] == "test-api-key"
        assert model_calls[0]["model_id"] == "chat-model"

        await chat_session_database.refresh(chat_session)
        assert chat_session.title == "owner generated title"
        assert chat_session.uid == "user-1"
        assert chat_session.profile_id is None
        assert chat_session.profile_override_id == owner_profile.id

        auth_state.update(uid="user-2", is_superuser=False)
        forbidden = await client.post("/api/v1/chat/sessions/generate-title", json=request)
        assert forbidden.status_code == 200
        assert forbidden.json()["code"] == 403
        assert forbidden.json()["message"] == t(ERR_SESSION_NO_PERMISSION)
        assert len(model_calls) == 1

        await chat_session_database.refresh(chat_session)
        assert chat_session.title == "owner generated title"
        assert await _message_snapshot(chat_session_database, chat_session.session_id) == messages_before

        auth_state.update(uid="user-1", is_superuser=False)
        owner_generated = await client.post("/api/v1/chat/sessions/generate-title", json=request)
        assert owner_generated.status_code == 200
        assert owner_generated.json()["code"] == 200
        assert owner_generated.json()["data"] == {"title": "owner generated title"}
        assert len(model_calls) == 2

    await chat_session_database.refresh(chat_session)
    assert chat_session.uid == "user-1"
    assert chat_session.profile_override_id == owner_profile.id
    assert chat_session.title == "owner generated title"


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["weixin-openclaw", "other-message-platform"])
async def test_superuser_cannot_generate_title_for_external_session(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    owner_profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert owner_profile.id is not None

    chat_session = ChatSession(
        session_id=f"external-title-permission-{source}",
        uid="user-1",
        profile_override_id=owner_profile.id,
        source=source,
        reply_target_source=source,
        title="external-before-title",
    )
    initial_message = Message(
        session_id=chat_session.session_id,
        uid="user-1",
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content="external-before-message",
        profile_id=owner_profile.id,
        is_processed=True,
    )
    chat_session_database.add(chat_session)
    await chat_session_database.flush()
    chat_session_database.add(initial_message)
    await chat_session_database.commit()
    messages_before = await _message_snapshot(chat_session_database, chat_session.session_id)

    auth_state: dict[str, object] = {"uid": "admin", "is_superuser": True}
    app = _build_app(chat_session_database, auth_state)
    model_calls = _patch_title_generation(monkeypatch, chat_session_database)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        forbidden = await client.post(
            "/api/v1/chat/sessions/generate-title",
            json={
                "session_id": chat_session.session_id,
                "first_message": "external first message",
            },
        )

    assert forbidden.status_code == 200
    assert forbidden.json()["code"] == 403
    assert forbidden.json()["message"] == t(ERR_SESSION_READ_ONLY)
    assert model_calls == []

    await chat_session_database.refresh(chat_session)
    assert chat_session.uid == "user-1"
    assert chat_session.profile_override_id == owner_profile.id
    assert chat_session.source == source
    assert chat_session.title == "external-before-title"
    assert await _message_snapshot(chat_session_database, chat_session.session_id) == messages_before
