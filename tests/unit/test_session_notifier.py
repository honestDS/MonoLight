import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import delete
from sqlmodel import select

from app.core.crud.session_event import session_event_crud
from app.core.session_notifier import SessionNotifier, build_session_event_dedupe_key
from app.core.utils.time import get_local_time
from app.models.session_event import SessionEvent
from app.providers.database import AsyncSessionLocal, engine


@pytest.fixture(autouse=True)
async def clean_session_event_table():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: SessionEvent.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: SessionEvent.__table__.create(sync_connection))
    async with AsyncSessionLocal() as db:
        await db.execute(delete(SessionEvent))
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(SessionEvent))
        await db.commit()


@pytest.mark.asyncio
async def test_database_event_is_delivered_to_each_notifier_instance():
    first_notifier = SessionNotifier()
    second_notifier = SessionNotifier()
    first_queue = asyncio.Queue()
    second_queue = asyncio.Queue()
    event = {"type": "proactive_reply", "content": "done"}

    await first_notifier.register("uid", "session", first_queue)
    await second_notifier.register("uid", "session", second_queue)
    await first_notifier.start()
    await second_notifier.start()
    try:
        await first_notifier.notify("uid", "session", event)

        first_received = await asyncio.wait_for(first_queue.get(), timeout=2)
        second_received = await asyncio.wait_for(second_queue.get(), timeout=2)
    finally:
        await first_notifier.stop()
        await second_notifier.stop()

    assert {key: value for key, value in first_received.items() if key != "event_sequence_no"} == event
    assert {key: value for key, value in second_received.items() if key != "event_sequence_no"} == event
    assert first_received["event_sequence_no"] == second_received["event_sequence_no"]
    assert first_received["event_sequence_no"] > 0


@pytest.mark.asyncio
async def test_notify_normalizes_non_json_event_values():
    notifier = SessionNotifier()
    timestamp = datetime(2026, 7, 10, tzinfo=UTC)

    await notifier.notify("uid", "session", {"type": "proactive_reply", "created_at": timestamp})

    async with AsyncSessionLocal() as db:
        saved = list((await db.execute(select(SessionEvent))).scalars().all())

    assert len(saved) == 1
    assert saved[0].event["created_at"] == "2026-07-10 00:00:00+00:00"


@pytest.mark.asyncio
async def test_notifier_start_skips_events_published_before_worker_started():
    async with AsyncSessionLocal() as db:
        await session_event_crud.publish(
            db,
            dedupe_key="old-event",
            uid="uid",
            session_id="session",
            event={"type": "old"},
        )

    notifier = SessionNotifier()
    queue = asyncio.Queue()
    await notifier.register("uid", "session", queue)
    await notifier.start()
    try:
        await asyncio.sleep(0.4)
    finally:
        await notifier.stop()

    assert queue.empty()


@pytest.mark.asyncio
async def test_cleanup_removes_only_expired_session_events():
    now = get_local_time()
    expired = SessionEvent(dedupe_key="expired", uid="uid", session_id="old", event={"type": "old"}, created_at=now - timedelta(hours=25))
    recent = SessionEvent(dedupe_key="recent", uid="uid", session_id="new", event={"type": "new"}, created_at=now)
    async with AsyncSessionLocal() as db:
        db.add_all([expired, recent])
        await db.commit()
        deleted_count = await session_event_crud.cleanup_expired(db)

    async with AsyncSessionLocal() as db:
        remaining = list((await db.execute(select(SessionEvent))).scalars().all())

    assert deleted_count == 1
    assert [item.session_id for item in remaining] == ["new"]


@pytest.mark.asyncio
async def test_duplicate_notify_persists_only_one_session_event():
    notifier = SessionNotifier()
    event = {
        "type": "proactive_reply",
        "source": "background_task",
        "background_task_id": 42,
        "content": "done",
    }

    first_created = await notifier.notify("uid", "session", event)
    second_created = await notifier.notify("uid", "session", {**event, "content": "regenerated"})

    async with AsyncSessionLocal() as db:
        saved = list((await db.execute(select(SessionEvent))).scalars().all())

    assert first_created is True
    assert second_created is False
    assert len(saved) == 1
    assert saved[0].event["content"] == "done"


def test_background_task_session_event_dedupe_key_is_stable_across_regeneration():
    first_event = {
        "type": "proactive_reply",
        "source": "background_task",
        "background_task_id": 42,
        "content": "first",
    }
    regenerated_event = {**first_event, "content": "second", "history": [{"role": "assistant"}]}

    first_key = build_session_event_dedupe_key("uid", "session", "http", first_event)
    regenerated_key = build_session_event_dedupe_key("uid", "session", "http", regenerated_event)
    error_key = build_session_event_dedupe_key("uid", "session", "http", {**first_event, "type": "proactive_reply_error"})

    assert first_key == regenerated_key
    assert error_key != first_key
