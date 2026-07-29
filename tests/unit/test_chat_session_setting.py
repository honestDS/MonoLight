from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.adapters import chat_web as chat_web_adapter
from app.api.v1 import chat as chat_api
from app.core.constants import (
    ERR_SESSION_NO_PERMISSION,
    ERR_SESSION_READ_ONLY,
    GUIDANCE_MESSAGE_PREFIX,
    GUIDANCE_MESSAGE_SUFFIX,
)
from app.core.exceptions import ForbiddenException
from app.core.i18n import t
from app.core.utils import session as session_utils
from app.models.message import ChatCompletionRequest, Message, MessageRole, MessageType


class FakeDb:
    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0
        self.added = []

    def add(self, item) -> None:
        self.added.append(item)

    async def commit(self) -> None:
        self.commit_count += 1

    async def rollback(self) -> None:
        self.rollback_count += 1


@pytest.mark.asyncio
async def test_update_web_session_setting_changes_markdown(monkeypatch):
    db = FakeDb()
    session = SimpleNamespace(
        uid="user-1",
        source="ws",
        enable_markdown=True,
    )

    async def get_by_session_id(db_arg, session_id: str):
        assert db_arg is db
        assert session_id == "session-1"
        return session

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)

    response = await chat_api.update_session_setting(
        chat_api.SessionSettingRequest(
            session_id="session-1",
            enable_markdown=False,
        ),
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert session.enable_markdown is False
    assert db.commit_count == 1
    assert response.code == 200


@pytest.mark.asyncio
async def test_delete_external_session_uses_standard_owner_cleanup(monkeypatch):
    db = FakeDb()
    cleanup_calls = []

    async def cleanup_session(db_arg, *, session_id, uid, is_admin):
        cleanup_calls.append((db_arg, session_id, uid, is_admin))
        return True

    monkeypatch.setattr(chat_api, "delete_session_data", cleanup_session)

    response = await chat_api.delete_session(
        "weixin-openclaw:user-1",
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert response.code == 200
    assert cleanup_calls == [(db, "weixin-openclaw:user-1", "user-1", False)]
    assert db.commit_count == 1
    assert db.rollback_count == 0


@pytest.mark.asyncio
async def test_update_external_session_setting_is_read_only(monkeypatch):
    db = FakeDb()
    session = SimpleNamespace(
        uid="user-1",
        source="weixin-openclaw",
        enable_markdown=True,
    )

    async def get_by_session_id(db_arg, session_id: str):
        return session

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)

    response = await chat_api.update_session_setting(
        chat_api.SessionSettingRequest(
            session_id="session-1",
            enable_markdown=False,
        ),
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert response.code == 403
    assert response.message == "该会话来自外部消息平台，网页端仅允许查看"
    assert session.enable_markdown is True
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_update_external_session_setting_assigns_valid_profile_override(monkeypatch):
    db = FakeDb()
    profile_calls = []
    session = SimpleNamespace(
        uid="user-1",
        source="weixin-openclaw",
        enable_markdown=True,
        profile_override_id=None,
    )

    async def get_by_session_id(db_arg, session_id: str):
        assert db_arg is db
        assert session_id == "session-1"
        return session

    async def get_validated_profile(db_arg, *, profile_id, uid):
        assert db_arg is db
        profile_calls.append((profile_id, uid))
        return SimpleNamespace(id=17, uid=uid)

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)
    monkeypatch.setattr(chat_api, "get_validated_profile_for_assignment", get_validated_profile)

    response = await chat_api.update_session_setting(
        chat_api.SessionSettingRequest(
            session_id="session-1",
            profile_override_id=17,
        ),
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert response.code == 200
    assert profile_calls == [(17, "user-1")]
    assert session.profile_override_id == 17
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_update_session_setting_can_clear_profile_override_without_validation(monkeypatch):
    db = FakeDb()
    session = SimpleNamespace(
        uid="user-1",
        source="weixin-openclaw",
        enable_markdown=True,
        profile_override_id=17,
    )

    async def get_by_session_id(db_arg, session_id: str):
        assert db_arg is db
        assert session_id == "session-1"
        return session

    async def get_validated_profile(*args, **kwargs):
        raise AssertionError("clearing a profile override must not validate a profile")

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)
    monkeypatch.setattr(chat_api, "get_validated_profile_for_assignment", get_validated_profile)

    response = await chat_api.update_session_setting(
        chat_api.SessionSettingRequest(
            session_id="session-1",
            profile_override_id=None,
        ),
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert response.code == 200
    assert session.profile_override_id is None
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_non_admin_cannot_update_another_users_profile_override(monkeypatch):
    db = FakeDb()
    session = SimpleNamespace(
        uid="user-2",
        source="weixin-openclaw",
        enable_markdown=True,
        profile_override_id=17,
    )

    async def get_by_session_id(db_arg, session_id: str):
        assert db_arg is db
        assert session_id == "session-1"
        return session

    async def get_validated_profile(*args, **kwargs):
        raise AssertionError("an unauthorized update must not validate a profile")

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)
    monkeypatch.setattr(chat_api, "get_validated_profile_for_assignment", get_validated_profile)

    response = await chat_api.update_session_setting(
        chat_api.SessionSettingRequest(
            session_id="session-1",
            profile_override_id=19,
        ),
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert response.message == t(ERR_SESSION_NO_PERMISSION)
    assert session.profile_override_id == 17
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_create_external_session_guidance_wraps_and_persists_content(monkeypatch):
    db = FakeDb()
    session = SimpleNamespace(
        session_id="weixin-openclaw:user-1",
        uid="user-1",
        source="weixin-openclaw",
        profile_id=7,
    )
    create_calls = []

    async def get_by_session_id(db_arg, session_id: str):
        assert db_arg is db
        assert session_id == session.session_id
        return session

    async def create_guidance(db_arg, **kwargs):
        assert db_arg is db
        create_calls.append(kwargs)
        return Message(
            id=11,
            session_id=kwargs["session_id"],
            uid=kwargs["uid"],
            profile_id=kwargs["profile_id"],
            role=MessageRole.SYSTEM,
            type=MessageType.GUIDANCE,
            content=kwargs["content"],
            is_processed=False,
        )

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)
    monkeypatch.setattr(chat_api.message_crud, "create_guidance", create_guidance)

    response = await chat_api.create_session_guidance(
        chat_api.SessionGuidanceRequest(
            session_id=session.session_id,
            content="  请先回答重点  ",
        ),
        db=db,
        current_user=SimpleNamespace(uid="user-1"),
    )

    wrapped = f"{GUIDANCE_MESSAGE_PREFIX}请先回答重点{GUIDANCE_MESSAGE_SUFFIX}"
    assert response.code == 200
    assert response.data.type == MessageType.GUIDANCE
    assert response.data.content == wrapped
    assert create_calls == [
        {
            "session_id": session.session_id,
            "uid": "user-1",
            "profile_id": 7,
            "content": wrapped,
        }
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("source", [None, "", "http", "ws"])
async def test_create_guidance_rejects_non_external_session_sources(monkeypatch, source):
    db = FakeDb()
    session = SimpleNamespace(
        session_id="session-1",
        uid="user-1",
        source=source,
        profile_id=1,
    )

    async def get_by_session_id(db_arg, session_id: str):
        return session

    async def create_guidance(*args, **kwargs):
        raise AssertionError("non-external session must not persist guidance")

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)
    monkeypatch.setattr(chat_api.message_crud, "create_guidance", create_guidance)

    response = await chat_api.create_session_guidance(
        chat_api.SessionGuidanceRequest(session_id="session-1", content="guide"),
        db=db,
        current_user=SimpleNamespace(uid="user-1"),
    )

    assert response.code == 403
    assert response.message == "只有外部消息平台会话可添加引导"


def test_session_setting_rejects_reserved_reply_target_source():
    with pytest.raises(ValidationError):
        chat_api.SessionSettingRequest(
            session_id="session-1",
            reply_target_source="ws",
        )


@pytest.mark.asyncio
async def test_http_adapter_rejects_external_session_before_llm_work(monkeypatch):
    profile_calls = []
    enqueue_calls = []

    async def reject_external_session(db, *, session_id, uid):
        raise ForbiddenException(message=ERR_SESSION_READ_ONLY)

    async def resolve_profile(db, *, uid, session_id):
        profile_calls.append(uid)
        return SimpleNamespace(id=1)

    async def enqueue_foreground_message(*args, **kwargs):
        enqueue_calls.append((args, kwargs))
        raise AssertionError("external session must not be queued")

    monkeypatch.setattr(
        chat_web_adapter,
        "ensure_web_session_writable",
        reject_external_session,
    )
    monkeypatch.setattr(
        chat_web_adapter,
        "resolve_profile_for_session",
        resolve_profile,
    )
    monkeypatch.setattr(
        chat_web_adapter.session_reply_queue_manager,
        "enqueue_foreground_message",
        enqueue_foreground_message,
    )

    response = await chat_web_adapter.web_chat_adapter.chat(
        db=SimpleNamespace(),
        message="web message",
        uid="user-1",
        session_id="weixin-openclaw:user-1",
    )

    assert response["choices"][0]["finish_reason"] == "error"
    assert response["choices"][0]["message"]["content"] == "该会话来自外部消息平台，网页端仅允许查看"
    assert profile_calls == []
    assert enqueue_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["http", "ws"])
async def test_web_session_sources_are_writable(monkeypatch, source):
    session = SimpleNamespace(uid="user-1", source=source)

    async def get_by_session_id(db, session_id: str):
        return session

    monkeypatch.setattr(
        session_utils.session_crud,
        "get_by_session_id",
        get_by_session_id,
    )

    await session_utils.ensure_web_session_writable(
        SimpleNamespace(),
        session_id="session-1",
        uid="user-1",
    )


@pytest.mark.asyncio
async def test_external_session_source_is_read_only(monkeypatch):
    session = SimpleNamespace(uid="user-1", source="weixin-openclaw")

    async def get_by_session_id(db, session_id: str):
        return session

    monkeypatch.setattr(
        session_utils.session_crud,
        "get_by_session_id",
        get_by_session_id,
    )

    with pytest.raises(ForbiddenException) as exc_info:
        await session_utils.ensure_web_session_writable(
            SimpleNamespace(),
            session_id="session-1",
            uid="user-1",
        )

    assert exc_info.value.message == ERR_SESSION_READ_ONLY


@pytest.mark.asyncio
async def test_other_users_session_is_not_writable(monkeypatch):
    session = SimpleNamespace(uid="user-2", source="http")

    async def get_by_session_id(db, session_id: str):
        return session

    monkeypatch.setattr(
        session_utils.session_crud,
        "get_by_session_id",
        get_by_session_id,
    )

    with pytest.raises(ForbiddenException) as exc_info:
        await session_utils.ensure_web_session_writable(
            SimpleNamespace(),
            session_id="session-1",
            uid="user-1",
        )

    assert exc_info.value.message == ERR_SESSION_NO_PERMISSION


@pytest.mark.parametrize(
    ("event", "session_id", "require_session_id", "expected"),
    [
        ({"type": "content", "session_id": "session-a"}, "session-a", False, True),
        ({"type": "content", "session_id": "session-b"}, "session-a", False, False),
        ({"type": "proactive_reply"}, "session-a", True, False),
        ({"type": "proactive_reply", "session_id": "session-a"}, None, True, False),
        ({"type": "content"}, "session-a", False, True),
    ],
)
def test_websocket_event_matches_session(event, session_id, require_session_id, expected):
    assert (
        chat_api._event_matches_session(
            event,
            session_id,
            require_session_id=require_session_id,
        )
        is expected
    )


@pytest.mark.asyncio
async def test_create_new_web_session_with_profile_override_persists_valid_override(monkeypatch):
    db = FakeDb()
    profile_calls = []

    async def get_by_session_id(db_arg, session_id: str):
        assert db_arg is db
        assert session_id == "session-1"
        return None

    async def get_validated_profile(db_arg, *, profile_id, uid):
        assert db_arg is db
        profile_calls.append((profile_id, uid))
        return SimpleNamespace(id=17, uid=uid)

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)
    monkeypatch.setattr(chat_api, "get_validated_profile_for_assignment", get_validated_profile)

    await chat_api._create_new_web_session_with_profile_override(
        db,
        session_id="session-1",
        uid="user-1",
        source="http",
        profile_override_id=17,
    )

    assert profile_calls == [(17, "user-1")]
    assert len(db.added) == 1
    session = db.added[0]
    assert session.uid == "user-1"
    assert session.source == "http"
    assert session.reply_target_source == "http"
    assert session.profile_override_id == 17
    assert session.profile_id is None
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_create_new_web_session_without_profile_override_does_not_persist(monkeypatch):
    db = FakeDb()

    async def get_by_session_id(*args, **kwargs):
        raise AssertionError("an empty override must not query sessions")

    async def get_validated_profile(*args, **kwargs):
        raise AssertionError("an empty override must not validate profiles")

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)
    monkeypatch.setattr(chat_api, "get_validated_profile_for_assignment", get_validated_profile)

    await chat_api._create_new_web_session_with_profile_override(
        db,
        session_id="session-1",
        uid="user-1",
        source="ws",
        profile_override_id=None,
    )

    assert db.added == []
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_create_new_web_session_does_not_overwrite_existing_owner_session(monkeypatch):
    db = FakeDb()
    existing_session = SimpleNamespace(uid="user-1", profile_override_id=7)

    async def get_by_session_id(db_arg, session_id: str):
        assert db_arg is db
        assert session_id == "session-1"
        return existing_session

    async def get_validated_profile(*args, **kwargs):
        raise AssertionError("an existing session must not validate a replacement profile")

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)
    monkeypatch.setattr(chat_api, "get_validated_profile_for_assignment", get_validated_profile)

    await chat_api._create_new_web_session_with_profile_override(
        db,
        session_id="session-1",
        uid="user-1",
        source="http",
        profile_override_id=17,
    )

    assert existing_session.profile_override_id == 7
    assert db.added == []
    assert db.commit_count == 0


@pytest.mark.asyncio
async def test_chat_completions_creates_new_session_with_profile_override(monkeypatch):
    db = FakeDb()
    helper_calls = []

    async def create_new_session(db_arg, *, session_id, uid, source, profile_override_id, show_tool_calls):
        assert db_arg is db
        helper_calls.append((session_id, uid, source, profile_override_id, show_tool_calls))

    monkeypatch.setattr(chat_api, "_create_new_web_session_with_profile_override", create_new_session)

    response = await chat_api.chat_completions(
        ChatCompletionRequest(message="hello", profile_override_id=17),
        db=db,
        current_user=SimpleNamespace(uid="user-1"),
    )

    assert len(helper_calls) == 1
    session_id, uid, source, profile_override_id, show_tool_calls = helper_calls[0]
    assert session_id
    assert uid == "user-1"
    assert source == "http"
    assert profile_override_id == 17
    assert show_tool_calls is None
    assert response["choices"][0]["finish_reason"] == "new_session"


@pytest.mark.parametrize("profile_override_id", [0, False])
def test_chat_completion_request_rejects_invalid_profile_override(profile_override_id):
    with pytest.raises(ValidationError):
        ChatCompletionRequest(
            message="hello",
            profile_override_id=profile_override_id,
        )
