from types import SimpleNamespace

import pytest

from app.core.background_tasks import reply_trigger
from app.core.dispatcher import ChatDispatcher
from app.models.background_task import BackgroundTaskReplyStatus


class FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_success_event_failure_does_not_mark_reply_succeeded(monkeypatch):
    completed_statuses = []
    notified_errors = []

    class FakeBackgroundTaskCrud:
        async def get(self, db, task_id):
            return SimpleNamespace(id=task_id, session_id="session-1")

        async def complete_reply_claim(self, db, **kwargs):
            completed_statuses.append(kwargs["status"])
            return True

    class FakeSessionCrud:
        async def get_by_session_id(self, db, session_id):
            return SimpleNamespace(id=1)

    async def fake_save_background_task_result_message(db, task):
        return None

    async def fake_dispatch_proactive_reply(task_id):
        return {
            "uid": "user-1",
            "session_id": "session-1",
            "content": "reply",
        }

    async def fake_send_session_event(uid, session_id, event):
        raise RuntimeError("session event write failed")

    async def fake_save_and_notify_reply_error(task_id, worker_id, error_message):
        notified_errors.append(error_message)

    monkeypatch.setattr(reply_trigger, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(reply_trigger, "background_task_crud", FakeBackgroundTaskCrud())
    monkeypatch.setattr(reply_trigger, "session_crud", FakeSessionCrud())
    monkeypatch.setattr(reply_trigger, "_save_background_task_result_message", fake_save_background_task_result_message)
    monkeypatch.setattr(reply_trigger, "_send_session_event", fake_send_session_event)
    monkeypatch.setattr(reply_trigger, "_save_and_notify_reply_error", fake_save_and_notify_reply_error)
    monkeypatch.setattr(ChatDispatcher, "dispatch_proactive_reply", fake_dispatch_proactive_reply)

    await reply_trigger._execute_claimed_reply(task_id=1, worker_id="worker-1")

    assert BackgroundTaskReplyStatus.SUCCEEDED not in completed_statuses
    assert notified_errors == ["session event write failed"]


@pytest.mark.asyncio
async def test_success_state_commit_failure_retries_without_user_visible_error(monkeypatch):
    completed_statuses = []
    sent_events = []
    notified_errors = []
    sleep_delays = []

    class FakeBackgroundTaskCrud:
        def __init__(self):
            self.complete_attempts = 0

        async def get(self, db, task_id):
            return SimpleNamespace(id=task_id, session_id="session-1")

        async def complete_reply_claim(self, db, **kwargs):
            self.complete_attempts += 1
            if self.complete_attempts == 1:
                raise RuntimeError("state commit failed")
            completed_statuses.append(kwargs["status"])
            return True

    class FakeSessionCrud:
        async def get_by_session_id(self, db, session_id):
            return SimpleNamespace(id=1)

    async def fake_save_background_task_result_message(db, task):
        return None

    async def fake_dispatch_proactive_reply(task_id):
        return {
            "uid": "user-1",
            "session_id": "session-1",
            "content": "reply",
        }

    async def fake_send_session_event(uid, session_id, event):
        sent_events.append(event["type"])

    async def fake_save_and_notify_reply_error(task_id, worker_id, error_message):
        notified_errors.append(error_message)

    async def fake_sleep(delay):
        sleep_delays.append(delay)

    monkeypatch.setattr(reply_trigger, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(reply_trigger, "background_task_crud", FakeBackgroundTaskCrud())
    monkeypatch.setattr(reply_trigger, "session_crud", FakeSessionCrud())
    monkeypatch.setattr(reply_trigger, "_save_background_task_result_message", fake_save_background_task_result_message)
    monkeypatch.setattr(reply_trigger, "_send_session_event", fake_send_session_event)
    monkeypatch.setattr(reply_trigger, "_save_and_notify_reply_error", fake_save_and_notify_reply_error)
    monkeypatch.setattr(reply_trigger.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(ChatDispatcher, "dispatch_proactive_reply", fake_dispatch_proactive_reply)

    await reply_trigger._execute_claimed_reply(task_id=1, worker_id="worker-1")

    assert sent_events == ["proactive_reply"]
    assert notified_errors == []
    assert completed_statuses == [BackgroundTaskReplyStatus.SUCCEEDED]
    assert sleep_delays == [1.0]


@pytest.mark.asyncio
async def test_error_event_failure_does_not_mark_reply_failed(monkeypatch):
    completed_statuses = []

    class FakeBackgroundTaskCrud:
        async def get(self, db, task_id):
            return SimpleNamespace(
                id=task_id,
                uid="user-1",
                session_id="session-1",
                profile_id=1,
            )

        async def complete_reply_claim(self, db, **kwargs):
            completed_statuses.append(kwargs["status"])
            return True

    async def fake_save_message(*args, **kwargs):
        return None

    async def fake_send_session_event(uid, session_id, event):
        raise RuntimeError("error event write failed")

    monkeypatch.setattr(reply_trigger, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(reply_trigger, "background_task_crud", FakeBackgroundTaskCrud())
    monkeypatch.setattr(reply_trigger, "save_message", fake_save_message)
    monkeypatch.setattr(reply_trigger, "_send_session_event", fake_send_session_event)

    with pytest.raises(RuntimeError, match="error event write failed"):
        await reply_trigger._save_and_notify_reply_error(
            task_id=1,
            worker_id="worker-1",
            error_message="reply failed",
        )

    assert BackgroundTaskReplyStatus.FAILED not in completed_statuses


@pytest.mark.asyncio
async def test_error_reply_is_completed_after_event_is_persisted(monkeypatch):
    operations = []

    class FakeBackgroundTaskCrud:
        async def get(self, db, task_id):
            return SimpleNamespace(
                id=task_id,
                uid="user-1",
                session_id="session-1",
                profile_id=1,
            )

        async def complete_reply_claim(self, db, **kwargs):
            operations.append(("complete", kwargs["status"]))
            return True

    async def fake_save_message(*args, **kwargs):
        operations.append(("save_message", None))
        return None

    async def fake_send_session_event(uid, session_id, event):
        operations.append(("send_event", event["type"]))

    monkeypatch.setattr(reply_trigger, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(reply_trigger, "background_task_crud", FakeBackgroundTaskCrud())
    monkeypatch.setattr(reply_trigger, "save_message", fake_save_message)
    monkeypatch.setattr(reply_trigger, "_send_session_event", fake_send_session_event)

    await reply_trigger._save_and_notify_reply_error(
        task_id=1,
        worker_id="worker-1",
        error_message="reply failed",
    )

    assert operations == [
        ("save_message", None),
        ("send_event", "proactive_reply_error"),
        ("complete", BackgroundTaskReplyStatus.FAILED),
    ]
