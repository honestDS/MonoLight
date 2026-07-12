from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.adapters import chat_web as chat_web_adapter
from app.api.v1 import chat as chat_api
from app.core import constants
from app.core.exceptions import ForbiddenException
from app.core.utils import session as session_utils


class FakeDb:
    def __init__(self) -> None:
        self.commit_count = 0
        self.rollback_count = 0

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
    remove_calls = []
    cancel_calls = []

    async def remove_session(db_arg, *, session_id, uid, is_admin, commit):
        remove_calls.append((db_arg, session_id, uid, is_admin, commit))
        return 2

    async def cancel_session(db_arg, *, session_id, uid, is_admin, commit):
        cancel_calls.append((db_arg, session_id, uid, is_admin, commit))
        return 1

    monkeypatch.setattr(chat_api.message_crud, "remove_session", remove_session)
    monkeypatch.setattr(chat_api.session_reply_work_item_crud, "cancel_session", cancel_session)

    response = await chat_api.delete_session(
        "weixin-openclaw:user-1",
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert response.code == 200
    assert remove_calls == [(db, "weixin-openclaw:user-1", "user-1", False, False)]
    assert cancel_calls == [(db, "weixin-openclaw:user-1", "user-1", False, False)]
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
        raise ForbiddenException(message=constants.ERR_SESSION_READ_ONLY)

    async def get_active(db, *, uid):
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
        chat_web_adapter.profile_crud,
        "get_active",
        get_active,
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

    assert exc_info.value.message == constants.ERR_SESSION_READ_ONLY


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

    assert exc_info.value.message == constants.ERR_SESSION_NO_PERMISSION


def test_websocket_event_matches_only_its_session():
    assert chat_api._event_matches_session(
        {"type": "content", "session_id": "session-a"},
        "session-a",
    )
    assert not chat_api._event_matches_session(
        {"type": "content", "session_id": "session-b"},
        "session-a",
    )


def test_websocket_notification_requires_session_id():
    assert not chat_api._event_matches_session(
        {"type": "proactive_reply"},
        "session-a",
        require_session_id=True,
    )
    assert not chat_api._event_matches_session(
        {"type": "proactive_reply", "session_id": "session-a"},
        None,
        require_session_id=True,
    )


def test_websocket_direct_response_can_omit_session_id_for_compatibility():
    assert chat_api._event_matches_session(
        {"type": "content"},
        "session-a",
    )
