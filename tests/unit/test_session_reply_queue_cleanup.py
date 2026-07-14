from datetime import timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.session_reply_work_item import CRUDSessionReplyWorkItem, SessionReplyCleanupResult
from app.core.session_reply_queue import consumer as consumer_module
from app.core.utils.time import get_local_time
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from tests.unit.session_reply_queue_test_support import enqueue

pytest_plugins = ("tests.unit.session_reply_queue_fixture",)


@pytest.mark.asyncio
async def test_cleanup_terminal_items_removes_only_expired_terminal_work_and_stream_events(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    old_time = get_local_time() - timedelta(hours=25)

    expired_terminal = []
    for index, status in enumerate(
        [
            SessionReplyWorkStatus.MERGED,
            SessionReplyWorkStatus.SUCCEEDED,
            SessionReplyWorkStatus.FAILED,
            SessionReplyWorkStatus.CANCELLED,
        ],
        start=1,
    ):
        work = await enqueue(
            crud,
            db_session,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=index,
            dedupe_key=f"foreground-message:{index}",
        )
        work.status = status
        work.updated_at = old_time
        expired_terminal.append(work)

    recent_terminal = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=10,
        dedupe_key="foreground-message:10",
    )
    recent_terminal.status = SessionReplyWorkStatus.SUCCEEDED

    old_ready = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=11,
        dedupe_key="foreground-message:11",
    )
    old_ready.updated_at = old_time

    old_running = await enqueue(
        crud,
        db_session,
        work_type=SessionReplyWorkType.FOREGROUND_REPLY,
        source_id=12,
        dedupe_key="foreground-message:12",
    )
    old_running.status = SessionReplyWorkStatus.RUNNING
    old_running.updated_at = old_time

    for work in [*expired_terminal, recent_terminal, old_ready, old_running]:
        db_session.add(
            SessionReplyStreamEvent(
                work_id=work.id,
                sequence_no=1,
                event={"type": "content", "content": str(work.id)},
            )
        )
    db_session.add(
        SessionReplyStreamEvent(
            work_id=999999,
            sequence_no=1,
            event={"type": "content", "content": "orphan"},
        )
    )
    db_session.add(
        SessionReplySequence(
            session_id="orphan-session",
            next_sequence_no=2,
        )
    )
    db_session.add(
        ChatSession(
            session_id="active-empty-session",
            uid="user-1",
        )
    )
    db_session.add(
        SessionReplySequence(
            session_id="active-empty-session",
            next_sequence_no=2,
        )
    )
    await db_session.commit()

    cleanup_result = await crud.cleanup_terminal_items(db_session)

    remaining_work = list((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    remaining_events = list((await db_session.execute(select(SessionReplyStreamEvent))).scalars().all())
    remaining_sequences = list((await db_session.execute(select(SessionReplySequence))).scalars().all())
    expired_ids = {work.id for work in expired_terminal}

    assert cleanup_result == SessionReplyCleanupResult(
        work_items=4,
        stream_events=5,
        sequences=1,
    )
    assert cleanup_result.total == 10
    assert {work.id for work in remaining_work} == {
        recent_terminal.id,
        old_ready.id,
        old_running.id,
    }
    assert {event.work_id for event in remaining_events} == {
        recent_terminal.id,
        old_ready.id,
        old_running.id,
    }
    assert not expired_ids.intersection(event.work_id for event in remaining_events)
    assert {sequence.session_id for sequence in remaining_sequences} == {
        "session-1",
        "active-empty-session",
    }


@pytest.mark.asyncio
async def test_cleanup_terminal_items_limits_each_delete_batch(db_session: AsyncSession):
    crud = CRUDSessionReplyWorkItem()
    old_time = get_local_time() - timedelta(hours=25)
    expired_work = []
    for index in range(3):
        work = await enqueue(
            crud,
            db_session,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_id=index + 1,
            dedupe_key=f"foreground-message:{index + 1}",
        )
        work.status = SessionReplyWorkStatus.SUCCEEDED
        work.updated_at = old_time
        expired_work.append(work)
        for sequence_no in range(2):
            db_session.add(
                SessionReplyStreamEvent(
                    work_id=work.id,
                    sequence_no=sequence_no + 1,
                    event={"type": "content", "content": str(work.id)},
                )
            )
    await db_session.commit()

    first_result = await crud.cleanup_terminal_items(db_session, batch_size=2)

    remaining_work_count = len((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    remaining_event_count = len((await db_session.execute(select(SessionReplyStreamEvent))).scalars().all())
    assert first_result == SessionReplyCleanupResult(
        work_items=0,
        stream_events=2,
        sequences=0,
    )
    assert first_result.total == 2
    assert remaining_work_count == 3
    assert remaining_event_count == 4

    second_result = await crud.cleanup_terminal_items(db_session, batch_size=2)

    remaining_work_count = len((await db_session.execute(select(SessionReplyWorkItem))).scalars().all())
    remaining_event_count = len((await db_session.execute(select(SessionReplyStreamEvent))).scalars().all())
    assert second_result == SessionReplyCleanupResult(
        work_items=0,
        stream_events=2,
        sequences=0,
    )
    assert second_result.total == 2
    assert remaining_work_count == 3
    assert remaining_event_count == 2


@pytest.mark.asyncio
async def test_consumer_cleanup_processes_multiple_batches_and_yields(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    cleanup_calls = []
    sleep_calls = []
    results = iter(
        [
            SessionReplyCleanupResult(work_items=100, stream_events=400),
            SessionReplyCleanupResult(work_items=50, stream_events=25, sequences=5),
        ]
    )

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def cleanup_terminal_items(db, *, batch_size):
        cleanup_calls.append((db, batch_size))
        return next(results)

    async def sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        consumer_module.session_reply_work_item_crud,
        "cleanup_terminal_items",
        cleanup_terminal_items,
    )
    monkeypatch.setattr(consumer_module.asyncio, "sleep", sleep)

    await consumer._cleanup_terminal_items()

    assert [batch_size for _db, batch_size in cleanup_calls] == [500, 500]
    assert sleep_calls == [0]


@pytest.mark.asyncio
async def test_consumer_cleanup_stops_at_per_run_limit(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    cleanup_batch_sizes = []
    sleep_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def cleanup_terminal_items(db, *, batch_size):
        cleanup_batch_sizes.append(batch_size)
        return SessionReplyCleanupResult(stream_events=batch_size)

    async def sleep(delay):
        sleep_calls.append(delay)

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        consumer_module.session_reply_work_item_crud,
        "cleanup_terminal_items",
        cleanup_terminal_items,
    )
    monkeypatch.setattr(consumer_module.asyncio, "sleep", sleep)

    await consumer._cleanup_terminal_items()

    assert sum(cleanup_batch_sizes) == consumer_module.SESSION_REPLY_CLEANUP_MAX_ITEMS_PER_RUN
    assert cleanup_batch_sizes == [consumer_module.SESSION_REPLY_CLEANUP_BATCH_SIZE] * 10
    assert sleep_calls == [0] * 9


@pytest.mark.asyncio
async def test_consumer_cleanup_runs_immediately_then_once_per_hour(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    cleanup_times = []

    async def cleanup_terminal_items():
        cleanup_times.append(consumer._next_cleanup_at)

    monkeypatch.setattr(consumer, "_cleanup_terminal_items", cleanup_terminal_items)

    await consumer._cleanup_terminal_items_if_due(5.0)
    await consumer._cleanup_terminal_items_if_due(3604.9)
    await consumer._cleanup_terminal_items_if_due(3605.0)

    assert cleanup_times == [0.0, 3605.0]
    assert consumer._next_cleanup_at == 7205.0


@pytest.mark.asyncio
async def test_consumer_cleanup_delegates_to_terminal_item_cleanup(monkeypatch):
    consumer = consumer_module.SessionReplyConsumer()
    cleanup_calls = []

    class FakeSession:
        pass

    class SessionContext:
        async def __aenter__(self):
            return FakeSession()

        async def __aexit__(self, exc_type, exc, traceback):
            return False

    async def cleanup_terminal_items(db, *, batch_size):
        cleanup_calls.append((db, batch_size))
        return SessionReplyCleanupResult()

    monkeypatch.setattr(consumer_module, "AsyncSessionLocal", SessionContext)
    monkeypatch.setattr(
        consumer_module.session_reply_work_item_crud,
        "cleanup_terminal_items",
        cleanup_terminal_items,
    )

    await consumer._cleanup_terminal_items()

    assert len(cleanup_calls) == 1
