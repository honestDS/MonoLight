import asyncio
from types import SimpleNamespace

import pytest

from app.core.constants import SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY
from app.core.exceptions import LLMException
from app.core.session_reply_queue import consumer as consumer_module
from app.core.session_reply_queue import executor as executor_module
from app.models.session_reply_work_item import SessionReplyWorkType


@pytest.mark.asyncio
async def test_consumer_recovery_runs_full_failure_flow_for_exhausted_claims(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    failure_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def recover_expired(db):
        return 1, [
            (
                7,
                "lost-worker",
                "Maximum retry attempts reached after worker interruption",
            )
        ]

    async def fail_work(work_id: int, worker_id: str, error: str) -> None:
        failure_calls.append((work_id, worker_id, error))

    async def mark_unknown(work_id: int, worker_id: str, error: str) -> None:
        return None

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "recover_expired", recover_expired)
    monkeypatch.setattr(consumer_module, "mark_work_audit_execution_unknown", mark_unknown)
    monkeypatch.setattr(consumer_module, "fail_session_reply_work", fail_work)

    await consumer._recover_expired()

    assert failure_calls == [
        (
            7,
            "lost-worker",
            "Maximum retry attempts reached after worker interruption",
        )
    ]


@pytest.mark.asyncio
async def test_consumer_does_not_retry_business_failure_after_channel_fallback_is_exhausted(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    failure_calls = []
    retry_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise LLMException(message="ERR_LLM_CONNECTION_FAILED", detail="provider unavailable")

    async def has_events(db, *, work_id: int) -> bool:
        return False

    async def get_work(db, work_id: int):
        return type("Work", (), {"work_type": SessionReplyWorkType.FOREGROUND_REPLY})()

    async def fail_work(
        work_id: int,
        worker_id: str,
        error: str,
        *,
        user_error: str | None = None,
    ) -> None:
        failure_calls.append((work_id, worker_id, error, user_error))

    async def release_for_retry(*args, **kwargs) -> None:
        retry_calls.append((args, kwargs))

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(consumer_module.session_reply_stream_event_crud, "has_events", has_events)
    monkeypatch.setattr(consumer_module, "fail_session_reply_work", fail_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "release_for_retry", release_for_retry)

    await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=5)

    assert len(failure_calls) == 1
    assert failure_calls[0][:2] == (7, "worker-1")
    assert failure_calls[0][3] == LLMException(message="ERR_LLM_CONNECTION_FAILED", detail="provider unavailable").render_message()
    assert retry_calls == []


@pytest.mark.asyncio
async def test_consumer_does_not_retry_after_stream_content_was_emitted(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    failure_calls = []
    retry_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise RuntimeError("stream interrupted")

    async def has_events(db, *, work_id: int) -> bool:
        return True

    async def get_work(db, work_id: int):
        return type("Work", (), {"work_type": SessionReplyWorkType.FOREGROUND_REPLY})()

    async def fail_work(work_id: int, worker_id: str, error: str) -> None:
        failure_calls.append((work_id, worker_id, error))

    async def release_for_retry(*args, **kwargs) -> None:
        retry_calls.append((args, kwargs))

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(consumer_module.session_reply_stream_event_crud, "has_events", has_events)
    monkeypatch.setattr(consumer_module, "fail_session_reply_work", fail_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "release_for_retry", release_for_retry)

    await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=3)

    assert len(failure_calls) == 1
    assert failure_calls[0][:2] == (7, "worker-1")
    assert retry_calls == []


@pytest.mark.asyncio
async def test_consumer_marks_confirmed_execution_unknown_and_does_not_retry(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    unknown_calls = []
    failure_calls = []
    retry_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise RuntimeError("tool execution interrupted")

    async def get_work(db, work_id: int):
        return type("Work", (), {"work_type": SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION})()

    async def has_events(db, *, work_id: int) -> bool:
        return False

    async def mark_unknown(work_id: int, worker_id: str, error: str) -> None:
        unknown_calls.append((work_id, worker_id, error))

    async def fail_work(work_id: int, worker_id: str, error: str) -> None:
        failure_calls.append((work_id, worker_id, error))

    async def release_for_retry(*args, **kwargs) -> None:
        retry_calls.append((args, kwargs))

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(consumer_module.session_reply_stream_event_crud, "has_events", has_events)
    monkeypatch.setattr(consumer_module, "mark_work_audit_execution_unknown", mark_unknown)
    monkeypatch.setattr(consumer_module, "fail_session_reply_work", fail_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "release_for_retry", release_for_retry)

    await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=3)

    assert len(unknown_calls) == 1
    assert unknown_calls[0][:2] == (7, "worker-1")
    assert len(failure_calls) == 1
    assert retry_calls == []


@pytest.mark.asyncio
async def test_consumer_marks_confirmed_execution_unknown_when_cancelled(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    unknown_calls = []

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise asyncio.CancelledError

    async def mark_unknown(work_id: int, worker_id: str, error: str) -> None:
        unknown_calls.append((work_id, worker_id, error))

    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module, "mark_work_audit_execution_unknown", mark_unknown)

    with pytest.raises(asyncio.CancelledError):
        await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=3)

    assert len(unknown_calls) == 1
    assert unknown_calls[0][:2] == (7, "worker-1")


@pytest.mark.asyncio
async def test_consumer_marks_foreground_audit_execution_unknown_and_does_not_retry(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    unknown_calls = []
    failure_calls = []
    retry_calls = []
    work = SimpleNamespace(
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        execution_state={
            "active_audit_execution": {
                "audit_record_id": 42,
                "claim_token": "claim-token",
            }
        },
    )

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise RuntimeError("foreground tool execution interrupted")

    async def get_work(db, work_id: int):
        return work

    async def has_events(db, *, work_id: int) -> bool:
        return False

    async def mark_unknown(work_id: int, worker_id: str, error: str) -> None:
        unknown_calls.append((work_id, worker_id, error))

    async def fail_work(work_id: int, worker_id: str, error: str) -> None:
        failure_calls.append((work_id, worker_id, error))

    async def release_for_retry(*args, **kwargs) -> None:
        retry_calls.append((args, kwargs))

    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(consumer_module.session_reply_stream_event_crud, "has_events", has_events)
    monkeypatch.setattr(consumer_module, "mark_work_audit_execution_unknown", mark_unknown)
    monkeypatch.setattr(consumer_module, "fail_session_reply_work", fail_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "release_for_retry", release_for_retry)

    await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=5)

    assert unknown_calls[0][:2] == (7, "worker-1")
    assert len(failure_calls) == 1
    assert retry_calls == []


@pytest.mark.asyncio
async def test_consumer_retries_foreground_exception_without_active_audit_execution(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    retry_calls = []
    work = SimpleNamespace(
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        execution_state={},
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise RuntimeError("provider unavailable")

    async def get_work(db, work_id: int):
        return work

    async def has_events(db, *, work_id: int) -> bool:
        return False

    async def release_for_retry(*args, **kwargs) -> None:
        retry_calls.append((args, kwargs))

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(consumer_module.session_reply_stream_event_crud, "has_events", has_events)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "release_for_retry", release_for_retry)

    await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=5)

    assert len(retry_calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_type",
    [SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.SCHEDULED_TASK_SUMMARY],
)
async def test_consumer_does_not_retry_background_or_scheduled_audit_exception(monkeypatch, work_type):
    """后台和定时回复的活动审计异常不得释放为自动重试。"""
    consumer = consumer_module.SessionReplyConsumer()
    unknown_calls = []
    failure_calls = []
    retry_calls = []
    work = SimpleNamespace(
        work_type=work_type,
        execution_state={
            SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
                "audit_record_id": 42,
                "claim_token": "claim-token",
            }
        },
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise RuntimeError("audited tool interrupted")

    async def get_work(db, work_id: int):
        return work

    async def has_events(db, *, work_id: int) -> bool:
        return False

    async def mark_unknown(work_id: int, worker_id: str, error: str) -> None:
        unknown_calls.append((work_id, worker_id, error))

    async def fail_work(work_id: int, worker_id: str, error: str) -> None:
        failure_calls.append((work_id, worker_id, error))

    async def release_for_retry(*args, **kwargs) -> None:
        retry_calls.append((args, kwargs))

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(consumer_module.session_reply_stream_event_crud, "has_events", has_events)
    monkeypatch.setattr(consumer_module, "mark_work_audit_execution_unknown", mark_unknown)
    monkeypatch.setattr(consumer_module, "fail_session_reply_work", fail_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "release_for_retry", release_for_retry)

    await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=5)

    assert unknown_calls[0][:2] == (7, "worker-1")
    assert len(failure_calls) == 1
    assert retry_calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "work_type",
    [SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY, SessionReplyWorkType.SCHEDULED_TASK_SUMMARY],
)
async def test_consumer_marks_background_or_scheduled_audit_unknown_when_cancelled(monkeypatch, work_type):
    """后台和定时回复取消时必须标记活动审计结果未知。"""
    consumer = consumer_module.SessionReplyConsumer()
    unknown_calls = []
    work = SimpleNamespace(
        work_type=work_type,
        execution_state={
            SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY: {
                "audit_record_id": 42,
                "claim_token": "claim-token",
            }
        },
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise asyncio.CancelledError

    async def get_work(db, work_id: int):
        return work

    async def list_background_tasks(db, audit_record_id: int):
        return []

    async def list_active_audit_execution_record_ids(*args, **kwargs):
        return set()

    async def mark_execution_unknown(db, **kwargs) -> bool:
        unknown_calls.append(kwargs)
        return True

    async def update_confirmation(db, *, audit_record_id: int) -> None:
        unknown_calls.append({"confirmation_audit_record_id": audit_record_id})

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(executor_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module.background_task_crud, "list_by_audit_record", list_background_tasks)
    monkeypatch.setattr(
        executor_module.terminal_session_crud,
        "list_active_audit_execution_record_ids",
        list_active_audit_execution_record_ids,
    )
    monkeypatch.setattr(executor_module.audit_crud, "mark_execution_unknown", mark_execution_unknown)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)

    with pytest.raises(asyncio.CancelledError):
        await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=3)

    assert unknown_calls[0]["audit_record_id"] == 42
    assert unknown_calls[0]["claim_token"] == "claim-token"


@pytest.mark.asyncio
async def test_consumer_marks_foreground_audit_execution_unknown_when_cancelled(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    unknown_calls = []
    work = SimpleNamespace(
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        execution_state={
            "active_audit_execution": {
                "audit_record_id": 42,
                "claim_token": "claim-token",
            }
        },
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def execute_work(work_id: int, worker_id: str) -> None:
        raise asyncio.CancelledError

    async def get_work(db, work_id: int):
        return work

    async def mark_execution_unknown(db, **kwargs) -> bool:
        unknown_calls.append(kwargs)
        return True

    async def update_confirmation(db, *, audit_record_id: int) -> None:
        unknown_calls.append({"confirmation_audit_record_id": audit_record_id})

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "get", get_work)
    monkeypatch.setattr(executor_module.audit_crud, "mark_execution_unknown", mark_execution_unknown)
    monkeypatch.setattr(executor_module, "update_confirmation_message_status", update_confirmation)

    with pytest.raises(asyncio.CancelledError):
        await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=3)

    assert unknown_calls[0]["audit_record_id"] == 42
    assert unknown_calls[0]["claim_token"] == "claim-token"
