import copy

import httpx
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

import app.core.crud.channel.cursor as channel_cursor
import app.core.utils.session as session_utils
from app.core.constants import (
    ERR_AUXILIARY_REQUEST_OUTPUT_BUDGET_EXHAUSTED,
    ERR_NO_VALID_CHANNEL,
    ERR_SESSION_NO_PERMISSION,
    ERR_SESSION_READ_ONLY,
)
from app.core.exceptions import LLMException
from app.core.i18n import t
from app.models.channel import ModelChannel
from app.models.channel_cursor import ChannelCursor
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


@pytest_asyncio.fixture(autouse=True)
async def _use_test_database_for_channel_cursors(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = chat_session_database.bind
    assert engine is not None
    async with engine.begin() as connection:
        await connection.run_sync(ChannelCursor.__table__.create, checkfirst=True)
    monkeypatch.setattr(
        channel_cursor,
        "AsyncSessionLocal",
        async_sessionmaker(bind=engine, expire_on_commit=False),
    )


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "auxiliary_config",
    [
        pytest.param("configured", id="auxiliary-configured"),
        pytest.param("missing", id="auxiliary-missing"),
        pytest.param("empty", id="auxiliary-rules-empty"),
        pytest.param("zero-weight", id="auxiliary-zero-weight-unavailable"),
    ],
)
async def test_generate_title_routes_auxiliary_channel_without_mutating_profile_config(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    auxiliary_config: str,
) -> None:
    profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert profile.id is not None

    profile_config = copy.deepcopy(profile.configs)
    chat_config = profile_config["channel"]["chat_channel"]
    chat_config["chat_timeout"] = 17.0
    chat_rule = chat_config["rules"][0]
    chat_rule["reasoning_effort"] = "low"
    chat_channel = await chat_session_database.get(ModelChannel, chat_rule["channel_id"])
    assert chat_channel is not None
    chat_channel.model_ids = [{**model_entry, "max_tokens": 211} if model_entry.get("model_id") == "chat-model" else model_entry for model_entry in chat_channel.model_ids]

    if auxiliary_config in {"configured", "zero-weight"}:
        auxiliary_model = ModelChannel(
            name=f"title-auxiliary-channel-{auxiliary_config}",
            api_key="enc:v1:title-auxiliary-key",
            base_url="https://title-auxiliary.example.com/v1",
            model_ids=[
                {
                    "model_id": "title-model",
                    "usage": "CHAT",
                    "protocol": "OPENAI",
                    "context_window_k": 64,
                    "max_tokens": 73,
                    "is_enabled": True,
                }
            ],
        )
        chat_session_database.add(auxiliary_model)
        await chat_session_database.flush()
        assert auxiliary_model.id is not None
        profile_config["channel"]["auxiliary_channel"] = {
            "chat_timeout": 53.0,
            "rules": [
                {
                    "channel_id": auxiliary_model.id,
                    "model_id": "title-model",
                    "reasoning_effort": "high",
                    "priority": 1,
                    "weight": 0 if auxiliary_config == "zero-weight" else 1,
                    "is_enabled": True,
                }
            ],
        }
    elif auxiliary_config == "empty":
        profile_config["channel"]["auxiliary_channel"] = {
            "chat_timeout": 53.0,
            "rules": [],
        }

    profile.configs = profile_config
    session = ChatSession(
        session_id=f"title-auxiliary-route-{auxiliary_config}",
        uid="user-1",
        profile_override_id=profile.id,
        source="http",
        reply_target_source="http",
        title="before-title",
    )
    chat_session_database.add(session)
    await chat_session_database.commit()
    original_profile_config = copy.deepcopy(profile.configs)

    auth_state: dict[str, object] = {"uid": "user-1", "is_superuser": False}
    app = _build_app(chat_session_database, auth_state)
    model_calls = _patch_title_generation(monkeypatch, chat_session_database)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        response = await client.post(
            "/api/v1/chat/sessions/generate-title",
            json={
                "session_id": session.session_id,
                "first_message": "generate a title",
            },
        )

    if auxiliary_config == "zero-weight":
        assert response.status_code == 200
        assert response.json()["message"] == t(ERR_NO_VALID_CHANNEL)
        assert model_calls == []
    else:
        assert response.status_code == 200
        assert response.json()["code"] == 200
        assert response.json()["data"] == {"title": "owner generated title"}
        assert len(model_calls) == 1

        if auxiliary_config == "configured":
            expected_model_id = "title-model"
            expected_base_url = "https://title-auxiliary.example.com/v1"
            expected_max_tokens = 73
            expected_reasoning_effort = "high"
            expected_timeout = 53.0
        else:
            expected_model_id = "chat-model"
            expected_base_url = "https://chat-session.example.com/v1"
            expected_max_tokens = 211
            expected_reasoning_effort = "low"
            expected_timeout = 17.0

        assert model_calls[0]["api_key"] == "test-api-key"
        assert model_calls[0]["base_url"] == expected_base_url
        assert model_calls[0]["model_id"] == expected_model_id
        assert model_calls[0]["max_tokens"] == expected_max_tokens
        assert model_calls[0]["reasoning_effort"] == expected_reasoning_effort
        assert model_calls[0]["timeout"] == expected_timeout

    await chat_session_database.refresh(session)
    await chat_session_database.refresh(profile)
    assert session.title == ("before-title" if auxiliary_config == "zero-weight" else "owner generated title")
    assert profile.configs == original_profile_config


@pytest.mark.asyncio
@pytest.mark.parametrize("entrypoint", ["manual", "automatic"])
@pytest.mark.parametrize(
    "first_finish_reason",
    [pytest.param("length", id="finish-length"), pytest.param(None, id="finish-none-at-budget"), pytest.param("incomplete", id="finish-incomplete-at-budget")],
)
@pytest.mark.parametrize("fallback_outcome", ["success", "budget", "network"])
async def test_title_generation_falls_back_after_output_budget_without_saving_partial_title(
    chat_session_database: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    entrypoint: str,
    first_finish_reason: str | None,
    fallback_outcome: str,
) -> None:
    profile, _alternate_profile, _other_profile = await _seed_profiles(chat_session_database)
    assert profile.id is not None

    profile_config = copy.deepcopy(profile.configs)
    auxiliary_rules = []
    auxiliary_channels = [
        ("title-auxiliary-a", "title-model-a", "https://title-a.example.com/v1", 1024, 1, "high"),
        ("title-auxiliary-b", "title-model-b", "https://title-b.example.com/v1", 2048, 2, None),
    ]
    for name, model_id, base_url, max_tokens, priority, reasoning_effort in auxiliary_channels:
        channel = ModelChannel(
            name=f"{name}-{entrypoint}-{first_finish_reason}-{fallback_outcome}",
            api_key=f"enc:v1:{name}-key",
            base_url=base_url,
            model_ids=[
                {
                    "model_id": model_id,
                    "usage": "CHAT",
                    "protocol": "OPENAI",
                    "context_window_k": 64,
                    "max_tokens": max_tokens,
                    "is_enabled": True,
                }
            ],
        )
        chat_session_database.add(channel)
        await chat_session_database.flush()
        assert channel.id is not None
        auxiliary_rules.append(
            {
                "channel_id": channel.id,
                "model_id": model_id,
                "reasoning_effort": reasoning_effort,
                "priority": priority,
                "weight": 2,
                "is_enabled": True,
            }
        )
    profile_config["channel"]["auxiliary_channel"] = {
        "chat_timeout": 47.0,
        "rules": auxiliary_rules,
    }
    profile.configs = profile_config

    initial_title = "before-title" if entrypoint == "manual" else None
    session = ChatSession(
        session_id=f"title-budget-{entrypoint}-{first_finish_reason}-{fallback_outcome}",
        uid="user-1",
        profile_override_id=profile.id,
        source="http",
        reply_target_source="http",
        title=initial_title,
    )
    chat_session_database.add(session)
    await chat_session_database.commit()
    original_profile_config = copy.deepcopy(profile.configs)

    model_calls = _patch_title_generation(monkeypatch, chat_session_database)
    titles_before_second_call: list[str | None] = []

    async def fake_generate(*_args: object, **kwargs: object) -> InternalResponse:
        model_calls.append(kwargs)
        if len(model_calls) == 2:
            async with session_utils.AsyncSessionLocal() as title_check_db:
                persisted_session = await title_check_db.get(ChatSession, session.session_id)
                titles_before_second_call.append(persisted_session.title if persisted_session else None)

        if len(model_calls) == 1:
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="partial first title"),
                model="title-model-a",
                usage={"prompt_tokens": 10, "completion_tokens": kwargs["max_tokens"], "total_tokens": kwargs["max_tokens"] + 10},
                finish_reason=first_finish_reason,
            )

        if fallback_outcome == "network":
            raise LLMException()
        if fallback_outcome == "budget":
            return InternalResponse(
                message=InternalMessage(role=MessageRole.ASSISTANT, content="partial fallback title"),
                model="title-model-b",
                usage={"prompt_tokens": 10, "completion_tokens": kwargs["max_tokens"], "total_tokens": kwargs["max_tokens"] + 10},
                finish_reason="length",
            )
        return InternalResponse(
            message=InternalMessage(role=MessageRole.ASSISTANT, content="complete fallback title"),
            model="title-model-b",
            usage={"prompt_tokens": 10, "completion_tokens": 32, "total_tokens": 42},
            finish_reason="stop",
        )

    monkeypatch.setattr(LLMClient, "generate", fake_generate)

    if entrypoint == "manual":
        app = _build_app(chat_session_database, {"uid": "user-1", "is_superuser": False})
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/api/v1/chat/sessions/generate-title",
                json={"session_id": session.session_id, "first_message": "make a title"},
            )
        assert response.status_code == 200
        if fallback_outcome == "success":
            assert response.json()["code"] == 200
            assert response.json()["data"] == {"title": "complete fallback title"}
        else:
            assert response.json()["message"] == t(ERR_AUXILIARY_REQUEST_OUTPUT_BUDGET_EXHAUSTED)
        expected_title = "complete fallback title" if fallback_outcome == "success" else "before-title"
    else:
        result = await session_utils.generate_session_title_for_selected_profile(
            chat_session_database,
            uid="user-1",
            session_id=session.session_id,
            first_message="make a title",
        )
        expected_title = "complete fallback title" if fallback_outcome == "success" else None
        assert result == expected_title

    assert [call["model_id"] for call in model_calls] == ["title-model-a", "title-model-b"]
    assert [call["base_url"] for call in model_calls] == [
        "https://title-a.example.com/v1",
        "https://title-b.example.com/v1",
    ]
    assert [call["max_tokens"] for call in model_calls] == [1024, 2048]
    assert model_calls[0]["reasoning_effort"] == "high"
    assert "reasoning_effort" not in model_calls[1]
    assert [call["timeout"] for call in model_calls] == [47.0, 47.0]
    assert all(call["api_key"] == "test-api-key" for call in model_calls)
    assert titles_before_second_call == [initial_title]

    await chat_session_database.refresh(session)
    await chat_session_database.refresh(profile)
    assert session.title == expected_title
    assert profile.configs == original_profile_config

    cursor_rows = list((await chat_session_database.execute(select(ChannelCursor).order_by(ChannelCursor.cursor_key))).scalars().all())
    assert {row.cursor_key: row.position for row in cursor_rows} == {
        f"{profile.id}:CHAT:AUXILIARY:1": 1,
        f"{profile.id}:CHAT:AUXILIARY:2": 1,
    }
