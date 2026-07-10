from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import delete, update
from sqlmodel import select

from app.core.crud.message_platform_outbox import OUTBOX_LEASE_SECONDS, calculate_retry_delay_seconds, message_platform_outbox_crud
from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.manager import OUTBOX_DELIVERY_TIMEOUT_SECONDS, MessagePlatformPollingManager
from app.core.message_platforms.notifier import build_outbox_dedupe_key, normalize_outbox_event
from app.core.utils.time import get_local_time
from app.models.message_platform import MessagePlatform, MessagePlatformType
from app.models.message_platform_outbox import MessagePlatformOutbox, MessagePlatformOutboxStatus
from app.providers.database import AsyncSessionLocal, engine


class DeliveringHandler(MessagePlatformHandler):
    platform_type = MessagePlatformType.WEIXIN_OPENCLAW
    sources = frozenset({"outbox-test"})
    sent_events: list[dict[str, Any]]

    def __init__(self, *, send_result: bool = True) -> None:
        self.send_result = send_result
        self.sent_events = []

    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        return False

    async def run(self, platform_id: int) -> None:
        return None

    async def send_session_event(self, uid: str, session_id: str, source: str, event: dict[str, Any]) -> bool:
        self.sent_events.append(event)
        return self.send_result


@pytest.fixture(autouse=True)
async def clean_outbox_table():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: MessagePlatformOutbox.__table__.create(sync_connection, checkfirst=True))
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MessagePlatformOutbox))
        await db.commit()
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(MessagePlatformOutbox))
        await db.commit()


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_by_dedupe_key():
    event = {"type": "proactive_reply", "task_id": 1, "content": "done"}
    dedupe_key = build_outbox_dedupe_key("uid", "session", "outbox-test", event)

    async with AsyncSessionLocal() as db:
        first, first_created = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key=dedupe_key,
            uid="uid",
            session_id="session",
            source="outbox-test",
            event=event,
        )
        second, second_created = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key=dedupe_key,
            uid="uid",
            session_id="session",
            source="outbox-test",
            event=event,
        )

    assert first_created is True
    assert second_created is False
    assert second.id == first.id


@pytest.mark.asyncio
async def test_claim_is_atomic_and_requires_matching_owner_to_complete():
    async with AsyncSessionLocal() as db:
        item, _ = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key="claim-key",
            uid="uid",
            session_id="session",
            source="outbox-test",
            event={"type": "proactive_reply"},
        )

    async with AsyncSessionLocal() as db:
        claimed = await message_platform_outbox_crud.try_claim(db, item_id=item.id, worker_id="worker-a")
    async with AsyncSessionLocal() as db:
        duplicate_claim = await message_platform_outbox_crud.try_claim(db, item_id=item.id, worker_id="worker-b")
        wrong_owner_marked = await message_platform_outbox_crud.mark_sent(db, item_id=item.id, worker_id="worker-b")
        correct_owner_marked = await message_platform_outbox_crud.mark_sent(db, item_id=item.id, worker_id="worker-a")

    assert claimed is not None
    assert claimed.status == MessagePlatformOutboxStatus.PROCESSING
    assert claimed.attempt_count == 1
    assert duplicate_claim is None
    assert wrong_owner_marked is False
    assert correct_owner_marked is True


@pytest.mark.asyncio
async def test_expired_processing_item_can_be_reclaimed():
    async with AsyncSessionLocal() as db:
        item, _ = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key="expired-key",
            uid="uid",
            session_id="session",
            source="outbox-test",
            event={"type": "proactive_reply"},
        )
        first_claim = await message_platform_outbox_crud.try_claim(db, item_id=item.id, worker_id="worker-a")
        assert first_claim is not None
        await db.execute(update(MessagePlatformOutbox).where(MessagePlatformOutbox.id == item.id).values(lock_until=get_local_time() - timedelta(seconds=1)))
        await db.commit()

    async with AsyncSessionLocal() as db:
        second_claim = await message_platform_outbox_crud.try_claim(db, item_id=item.id, worker_id="worker-b")

    assert second_claim is not None
    assert second_claim.locked_by == "worker-b"
    assert second_claim.attempt_count == 2


@pytest.mark.asyncio
async def test_manager_sends_and_marks_outbox_item_sent():
    handler = DeliveringHandler()
    manager = MessagePlatformPollingManager((handler,))
    event = {"type": "proactive_reply", "content": "done"}

    async with AsyncSessionLocal() as db:
        item, _ = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key="delivery-key",
            uid="uid",
            session_id="session",
            source="outbox-test",
            event=event,
        )

    processed_count = await manager.process_outbox_batch()

    async with AsyncSessionLocal() as db:
        saved_item = await message_platform_outbox_crud.get(db, item.id)

    assert processed_count == 1
    assert handler.sent_events == [event]
    assert saved_item is not None
    assert saved_item.status == MessagePlatformOutboxStatus.SENT
    assert saved_item.sent_at is not None


@pytest.mark.asyncio
async def test_cleanup_removes_only_expired_terminal_items():
    now = get_local_time()
    expired_sent = MessagePlatformOutbox(
        dedupe_key="expired-sent",
        uid="uid",
        session_id="session",
        source="outbox-test",
        event={},
        status=MessagePlatformOutboxStatus.SENT,
        created_at=now - timedelta(days=10),
        sent_at=now - timedelta(days=8),
    )
    recent_sent = MessagePlatformOutbox(
        dedupe_key="recent-sent",
        uid="uid",
        session_id="session",
        source="outbox-test",
        event={},
        status=MessagePlatformOutboxStatus.SENT,
        sent_at=now,
    )
    expired_failed = MessagePlatformOutbox(
        dedupe_key="expired-failed",
        uid="uid",
        session_id="session",
        source="outbox-test",
        event={},
        status=MessagePlatformOutboxStatus.FAILED,
        created_at=now - timedelta(days=31),
    )
    pending = MessagePlatformOutbox(
        dedupe_key="old-pending",
        uid="uid",
        session_id="session",
        source="outbox-test",
        event={},
        status=MessagePlatformOutboxStatus.PENDING,
        created_at=now - timedelta(days=60),
    )
    async with AsyncSessionLocal() as db:
        db.add_all([expired_sent, recent_sent, expired_failed, pending])
        await db.commit()
        deleted_count = await message_platform_outbox_crud.cleanup_terminal_items(db)

    async with AsyncSessionLocal() as db:
        remaining_keys = {item.dedupe_key for item in (await db.execute(select(MessagePlatformOutbox))).scalars().all()}

    assert deleted_count == 2
    assert remaining_keys == {"recent-sent", "old-pending"}


@pytest.mark.asyncio
async def test_manager_requeues_failed_delivery_then_marks_terminal_failure():
    handler = DeliveringHandler(send_result=False)
    manager = MessagePlatformPollingManager((handler,))

    async with AsyncSessionLocal() as db:
        item, _ = await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key="retry-key",
            uid="uid",
            session_id="session",
            source="outbox-test",
            event={"type": "proactive_reply"},
        )

    await manager.process_outbox_batch()
    async with AsyncSessionLocal() as db:
        retried_item = await message_platform_outbox_crud.get(db, item.id)
        assert retried_item is not None
        assert retried_item.status == MessagePlatformOutboxStatus.PENDING
        assert retried_item.attempt_count == 1
        await db.execute(
            update(MessagePlatformOutbox)
            .where(MessagePlatformOutbox.id == item.id)
            .values(
                status=MessagePlatformOutboxStatus.PROCESSING,
                locked_by=manager._worker_id,
                attempt_count=5,
            )
        )
        await db.commit()
        terminal_item = await message_platform_outbox_crud.get(db, item.id)

    assert terminal_item is not None
    await manager._deliver_outbox_item(terminal_item)

    async with AsyncSessionLocal() as db:
        failed_item = await message_platform_outbox_crud.get(db, item.id)

    assert failed_item is not None
    assert failed_item.status == MessagePlatformOutboxStatus.FAILED
    assert failed_item.last_error


def test_retry_delay_uses_capped_exponential_backoff():
    assert [calculate_retry_delay_seconds(attempt) for attempt in (1, 2, 3, 4, 5, 10)] == [5, 10, 20, 40, 80, 300]


def test_outbox_lease_exceeds_delivery_timeout():
    assert OUTBOX_LEASE_SECONDS > OUTBOX_DELIVERY_TIMEOUT_SECONDS


def test_outbox_event_normalization_converts_non_json_values():
    timestamp = datetime(2026, 7, 10, tzinfo=UTC)

    normalized = normalize_outbox_event({"type": "proactive_reply", "created_at": timestamp})

    assert normalized == {"type": "proactive_reply", "created_at": "2026-07-10 00:00:00+00:00"}


def test_dedupe_key_is_stable_and_event_sensitive():
    event = {"type": "proactive_reply", "content": "done"}
    reordered_event = {"content": "done", "type": "proactive_reply"}
    changed_event = {**event, "content": "changed"}

    first = build_outbox_dedupe_key("uid", "session", "outbox-test", event)
    second = build_outbox_dedupe_key("uid", "session", "outbox-test", reordered_event)
    changed = build_outbox_dedupe_key("uid", "session", "outbox-test", changed_event)

    assert first == second
    assert first != changed


def test_dedupe_key_uses_stable_background_task_identity():
    first_event = {
        "type": "proactive_reply",
        "source": "background_task",
        "background_task_id": 42,
        "content": "first content",
    }
    repeated_event = {**first_event, "content": "regenerated content"}

    first = build_outbox_dedupe_key("uid", "session", "outbox-test", first_event)
    repeated = build_outbox_dedupe_key("uid", "session", "outbox-test", repeated_event)

    assert first == repeated


def test_dedupe_key_distinguishes_scheduled_task_runs():
    first_run = {
        "type": "proactive_reply",
        "source": "scheduled_task",
        "scheduled_task_id": 7,
        "trigger_message_id": 100,
    }
    second_run = {**first_run, "trigger_message_id": 101}

    first = build_outbox_dedupe_key("uid", "session", "outbox-test", first_run)
    second = build_outbox_dedupe_key("uid", "session", "outbox-test", second_run)

    assert first != second
