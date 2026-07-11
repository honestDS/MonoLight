from types import SimpleNamespace

import pytest

from app.core.constants import ERR_CHAT_CHANNEL_NOT_FOUND
from app.core.dispatcher import ChatDispatcher
from app.core.dispatchers import non_stream
from app.core.exceptions import LLMException
from app.models.message import InternalMessage, MessageRole


class FakeDb:
    pass


async def _configure_dispatch_start(monkeypatch, *, acquire_results, persisted_message=None):
    acquire_attempts = []
    release_calls = []
    sleep_delays = []
    persisted_queries = []
    cleanup_calls = []
    profile_reload_queries = []
    reloaded_profile = SimpleNamespace(id=1, source="reloaded")

    class FakeUserCrud:
        async def get_by_uid(self, db, uid):
            return SimpleNamespace(username="admin")

    class FakeProfileCrud:
        async def get_active(self, db, uid):
            return SimpleNamespace(id=1, source="initial")

        async def get_with_relations(self, db, profile_id):
            profile_reload_queries.append(profile_id)
            return reloaded_profile

    class FakeActiveSessionCrud:
        async def cleanup_expired_locks(self, db):
            cleanup_calls.append(db)

        async def acquire_lock(self, db, session_id):
            acquire_attempts.append(session_id)
            return acquire_results.pop(0)

        async def release_lock(self, db, session_id):
            release_calls.append(session_id)

    class FakeMessageCrud:
        async def get(self, db, message_id):
            persisted_queries.append(message_id)
            return persisted_message

    async def fake_validate_initial_message_before_save(*args, **kwargs):
        return None

    async def fake_save_initial_message(*args, **kwargs):
        return InternalMessage(
            id=7,
            role=MessageRole.USER,
            content="追加消息",
        )

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(non_stream, "user_crud", FakeUserCrud())
    monkeypatch.setattr(non_stream, "profile_crud", FakeProfileCrud())
    monkeypatch.setattr(non_stream, "active_session_crud", FakeActiveSessionCrud())
    monkeypatch.setattr(non_stream, "message_crud", FakeMessageCrud())
    monkeypatch.setattr(non_stream, "save_initial_message", fake_save_initial_message)
    monkeypatch.setattr(non_stream.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(
        ChatDispatcher,
        "validate_initial_message_before_save",
        fake_validate_initial_message_before_save,
    )

    return (
        acquire_attempts,
        release_calls,
        sleep_delays,
        persisted_queries,
        cleanup_calls,
        profile_reload_queries,
        reloaded_profile,
    )


@pytest.mark.asyncio
async def test_waiting_request_stops_when_original_dispatcher_processed_message(
    monkeypatch,
):
    persisted_message = SimpleNamespace(is_processed=True)
    (
        acquire_attempts,
        release_calls,
        sleep_delays,
        persisted_queries,
        cleanup_calls,
        profile_reload_queries,
        _reloaded_profile,
    ) = await _configure_dispatch_start(
        monkeypatch,
        acquire_results=[False, False, True],
        persisted_message=persisted_message,
    )

    response = await ChatDispatcher.dispatch(
        db=FakeDb(),
        message="追加消息",
        uid="user-1",
        session_id="session-1",
        session_source="weixin-openclaw",
        wait_for_session_lock=True,
    )

    assert response["choices"][0]["finish_reason"] == "queued"
    assert acquire_attempts == ["session-1", "session-1", "session-1"]
    assert release_calls == ["session-1"]
    assert sleep_delays == [
        non_stream.SESSION_LOCK_RETRY_INTERVAL_SECONDS,
        non_stream.SESSION_LOCK_RETRY_INTERVAL_SECONDS,
    ]
    assert persisted_queries == [7]
    assert cleanup_calls == []
    assert profile_reload_queries == []


@pytest.mark.asyncio
async def test_waiting_request_continues_when_message_is_still_unprocessed(
    monkeypatch,
):
    persisted_message = SimpleNamespace(is_processed=False)
    (
        acquire_attempts,
        release_calls,
        sleep_delays,
        persisted_queries,
        cleanup_calls,
        profile_reload_queries,
        reloaded_profile,
    ) = await _configure_dispatch_start(
        monkeypatch,
        acquire_results=[False, True],
        persisted_message=persisted_message,
    )
    validation_calls = []

    async def fake_validate_profile_and_cfg(db, profile):
        validation_calls.append(profile)
        raise LLMException(message=ERR_CHAT_CHANNEL_NOT_FOUND)

    monkeypatch.setattr(
        non_stream,
        "validate_profile_and_cfg",
        fake_validate_profile_and_cfg,
    )

    with pytest.raises(LLMException):
        await ChatDispatcher.dispatch(
            db=FakeDb(),
            message="追加消息",
            uid="user-1",
            session_id="session-1",
            session_source="weixin-openclaw",
            wait_for_session_lock=True,
        )

    assert acquire_attempts == ["session-1", "session-1"]
    assert release_calls == ["session-1"]
    assert sleep_delays == [non_stream.SESSION_LOCK_RETRY_INTERVAL_SECONDS]
    assert persisted_queries == [7]
    assert cleanup_calls == []
    assert profile_reload_queries == [1]
    assert validation_calls == [reloaded_profile]


@pytest.mark.asyncio
async def test_default_non_stream_request_still_returns_queued_without_waiting(
    monkeypatch,
):
    (
        acquire_attempts,
        release_calls,
        sleep_delays,
        persisted_queries,
        cleanup_calls,
        profile_reload_queries,
        _reloaded_profile,
    ) = await _configure_dispatch_start(
        monkeypatch,
        acquire_results=[False],
    )

    response = await ChatDispatcher.dispatch(
        db=FakeDb(),
        message="追加消息",
        uid="user-1",
        session_id="session-1",
    )

    assert response["choices"][0]["finish_reason"] == "queued"
    assert acquire_attempts == ["session-1"]
    assert release_calls == []
    assert sleep_delays == []
    assert persisted_queries == []
    assert len(cleanup_calls) == 1
    assert profile_reload_queries == []
