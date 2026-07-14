from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.core.session_reply_queue import executor as executor_module
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)


@pytest.mark.asyncio
async def test_foreground_executor_resumes_dispatcher_checkpoint(monkeypatch):
    checkpoint = {
        "messages": [
            {"role": "user", "content": "original"},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [{"id": "tool-1", "name": "execute_shell", "arguments": {"command": "echo 1"}}],
            },
            {"role": "tool", "content": "1", "tool_call_id": "tool-1"},
        ],
        "turn_messages": [],
        "files_to_user": [],
        "current_turn": 1,
    }
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
        input_message_ids=[1],
        execution_state={"stream_requested": False, "dispatcher_checkpoint": checkpoint},
    )
    dispatch_kwargs = {}
    checkpoint_updates = []

    class FakeDb:
        async def refresh(self, instance) -> None:
            return None

    class EventDb:
        pass

    class SessionContext:
        async def __aenter__(self):
            return EventDb()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def freeze_foreground_input(db, *, work, worker_id):
        return "original", [], [1]

    async def latest_sequence(db, *, work_id):
        return 0

    async def dispatch(**kwargs):
        dispatch_kwargs.update(kwargs)
        await kwargs["execution_checkpoint_callback"]({"messages": [{"role": "user", "content": "updated"}]})
        return {"choices": []}

    async def update_claimed(db, **kwargs):
        checkpoint_updates.append(kwargs)
        return True

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_queue_manager, "freeze_foreground_input", freeze_foreground_input)
    monkeypatch.setattr(executor_module.session_reply_stream_event_crud, "get_latest_sequence", latest_sequence)
    monkeypatch.setattr(executor_module.ChatDispatcher, "dispatch", dispatch)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)

    response = await executor_module._execute_foreground(FakeDb(), work, "worker-1")

    assert response == {"choices": []}
    assert dispatch_kwargs["execution_resume_state"] == checkpoint
    assert dispatch_kwargs["message"] == "original"
    assert checkpoint_updates[0]["values"]["execution_state"]["stream_requested"] is False
    assert checkpoint_updates[0]["values"]["execution_state"]["dispatcher_checkpoint"]["messages"][0]["content"] == "updated"


@pytest.mark.asyncio
async def test_executor_resumes_from_persisted_result_without_calling_llm(monkeypatch):
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:1",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
    )
    persisted_result = SimpleNamespace(
        id=9,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        content="saved response",
        created_at=work.created_at + timedelta(seconds=1),
    )
    sent_events = []
    update_calls = []
    terminal_calls = []
    llm_calls = []

    class FakeSession:
        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_work(db, work_id: int):
        return work

    async def get_result(db, dedupe_key: str):
        return persisted_result

    async def execute_foreground(db, current_work, worker_id: str):
        llm_calls.append((current_work.id, worker_id))
        return {}

    async def update_claimed(db, **kwargs):
        update_calls.append(kwargs)
        return True

    async def mark_terminal(db, **kwargs):
        terminal_calls.append(kwargs)
        return True

    async def send_event(uid: str, session_id: str, event: dict):
        sent_events.append((uid, session_id, event))

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module.message_crud, "get_by_dedupe_key", get_result)
    monkeypatch.setattr(executor_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_module, "send_session_event", send_event)

    await executor_module.execute_session_reply_work(work_id=7, worker_id="worker-1")

    assert llm_calls == []
    assert update_calls[0]["values"]["result_message_id"] == 9
    assert update_calls[0]["values"]["execution_state"]["response"]["choices"][0]["message"]["content"] == "saved response"
    assert sent_events[0][2]["event_id"].startswith("session-reply-work:")
    assert sent_events[0][2]["event_id"].endswith(":event")
    assert sent_events[0][2]["event_id"] != "session-reply-work:7:event"
    assert terminal_calls[0]["status"] == SessionReplyWorkStatus.SUCCEEDED
    assert terminal_calls[0]["result_message_id"] == 9


@pytest.mark.asyncio
async def test_executor_rejects_legacy_result_from_reused_work_id(monkeypatch):
    work = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="current-session",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="35",
        dedupe_key="foreground-message:current:35",
        status=SessionReplyWorkStatus.RUNNING,
        locked_by="worker-1",
    )
    stale_result = SimpleNamespace(
        id=2,
        uid="user-1",
        session_id="old-session",
        profile_id=1,
        content="old session response",
        created_at=work.created_at - timedelta(days=1),
    )
    generated_result = SimpleNamespace(
        id=10,
        uid=work.uid,
        session_id=work.session_id,
        profile_id=work.profile_id,
        content="current response",
        created_at=work.created_at + timedelta(seconds=1),
    )
    llm_calls = []
    update_calls = []
    terminal_calls = []
    sent_events = []

    class FakeSession:
        async def commit(self) -> None:
            return None

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def get_work(db, work_id: int):
        return work

    async def get_result(db, dedupe_key: str):
        if dedupe_key == "session-reply-work:7:result":
            return stale_result
        if llm_calls and dedupe_key == executor_module._result_message_dedupe_key(work):
            return generated_result
        return None

    async def execute_foreground(db, current_work, worker_id: str):
        llm_calls.append((current_work.id, worker_id))
        return {
            "choices": [
                {
                    "message": {"role": "assistant", "content": "current response"},
                    "finish_reason": True,
                }
            ]
        }

    async def update_claimed(db, **kwargs):
        update_calls.append(kwargs)
        return True

    async def mark_terminal(db, **kwargs):
        terminal_calls.append(kwargs)
        return True

    async def send_event(uid: str, session_id: str, event: dict):
        sent_events.append((uid, session_id, event))

    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module.message_crud, "get_by_dedupe_key", get_result)
    monkeypatch.setattr(executor_module, "_execute_foreground", execute_foreground)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "update_claimed", update_claimed)
    monkeypatch.setattr(executor_module.session_reply_work_item_crud, "mark_terminal", mark_terminal)
    monkeypatch.setattr(executor_module, "send_session_event", send_event)

    await executor_module.execute_session_reply_work(work_id=7, worker_id="worker-1")

    assert llm_calls == [(7, "worker-1")]
    assert update_calls[0]["values"]["result_message_id"] == 10
    assert update_calls[0]["values"]["execution_state"]["response"]["choices"][0]["message"]["content"] == "current response"
    assert sent_events[0][1] == "current-session"
    assert sent_events[0][2]["content"] == "current response"
    assert terminal_calls[0]["result_message_id"] == 10


def test_work_message_keys_do_not_depend_on_reusable_work_id():
    first = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="1",
        dedupe_key="foreground-message:first:1",
    )
    second = SessionReplyWorkItem(
        id=7,
        uid="user-1",
        session_id="session-2",
        profile_id=1,
        sequence_no=1,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_type=SessionReplySourceType.USER_MESSAGE,
        source_id="2",
        dedupe_key="foreground-message:second:2",
        created_at=first.created_at + timedelta(days=1),
    )

    round_tripped = SessionReplyWorkItem(
        id=first.id,
        uid=first.uid,
        session_id=first.session_id,
        profile_id=first.profile_id,
        sequence_no=first.sequence_no,
        work_type=first.work_type,
        source_type=first.source_type,
        source_id=first.source_id,
        dedupe_key=first.dedupe_key,
        created_at=first.created_at.replace(tzinfo=None),
    )

    first_key = executor_module._result_message_dedupe_key(first)
    second_key = executor_module._result_message_dedupe_key(second)

    assert first_key != second_key
    assert first_key == executor_module._result_message_dedupe_key(round_tripped)
    assert len(first_key) <= 64
    assert len(second_key) <= 64
    assert executor_module._event_for_work(first, {"content": "first"})["event_id"] != executor_module._event_for_work(second, {"content": "second"})["event_id"]
