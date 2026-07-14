import pytest

from app.core.exceptions import LLMException
from app.core.session_reply_queue import consumer as consumer_module


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

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "recover_expired", recover_expired)
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

    async def fail_work(work_id: int, worker_id: str, error: str) -> None:
        failure_calls.append((work_id, worker_id, error))

    async def release_for_retry(*args, **kwargs) -> None:
        retry_calls.append((args, kwargs))

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(consumer_module, "execute_session_reply_work", execute_work)
    monkeypatch.setattr(consumer_module.session_reply_stream_event_crud, "has_events", has_events)
    monkeypatch.setattr(consumer_module, "fail_session_reply_work", fail_work)
    monkeypatch.setattr(consumer_module.session_reply_work_item_crud, "release_for_retry", release_for_retry)

    await consumer._run_claimed(work_id=7, worker_id="worker-1", attempt_count=1, max_attempts=3)

    assert len(failure_calls) == 1
    assert failure_calls[0][:2] == (7, "worker-1")
    assert retry_calls == []
