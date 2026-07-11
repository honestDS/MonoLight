from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.v1 import chat as chat_api


class FakeDb:
    def __init__(self) -> None:
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1


@pytest.mark.asyncio
async def test_update_session_setting_changes_only_reply_target_source(monkeypatch):
    db = FakeDb()
    session = SimpleNamespace(
        uid="user-1",
        enable_markdown=True,
        reply_target_source="http",
    )

    async def get_by_session_id(db_arg, session_id: str):
        assert db_arg is db
        assert session_id == "session-1"
        return session

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)

    await chat_api.update_session_setting(
        chat_api.SessionSettingRequest(
            session_id="session-1",
            reply_target_source="ws",
        ),
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert session.reply_target_source == "ws"
    assert session.enable_markdown is True
    assert db.commit_count == 1


@pytest.mark.asyncio
async def test_update_session_setting_changes_only_markdown(monkeypatch):
    db = FakeDb()
    session = SimpleNamespace(
        uid="user-1",
        enable_markdown=True,
        reply_target_source="ws",
    )

    async def get_by_session_id(db_arg, session_id: str):
        return session

    monkeypatch.setattr(chat_api.session_crud, "get_by_session_id", get_by_session_id)

    await chat_api.update_session_setting(
        chat_api.SessionSettingRequest(
            session_id="session-1",
            enable_markdown=False,
        ),
        db=db,
        current_user=SimpleNamespace(uid="user-1", is_superuser=False),
    )

    assert session.enable_markdown is False
    assert session.reply_target_source == "ws"
    assert db.commit_count == 1


def test_session_setting_rejects_unsupported_reply_target_source():
    with pytest.raises(ValidationError):
        chat_api.SessionSettingRequest(
            session_id="session-1",
            reply_target_source="weixin-openclaw",
        )


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

