import asyncio
import secrets
import time
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlmodel import select

from app.core.crud.message_platform import outbox as outbox_crud_module
from app.core.crud.message_platform.outbox import message_platform_outbox_crud
from app.core.message_platforms import manager as manager_module
from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.manager import MessagePlatformPollingManager
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig
from app.core.notifications.inbox import NotificationInbox
from app.core.notifications.protocol import NotificationTopic
from app.core.notifications.runtime import NotificationRuntime
from app.core.notifications.server import NotificationServer
from app.core.utils.time import get_local_time
from app.models.message_platform import MessagePlatform, MessagePlatformType
from app.models.message_platform_outbox import MessagePlatformOutbox, MessagePlatformOutboxStatus
from app.models.session import ChatSession


class RecordingHandler(MessagePlatformHandler):
    platform_type = MessagePlatformType.WEIXIN_OPENCLAW
    sources = frozenset({"notification-outbox"})

    def __init__(self) -> None:
        self.sent: list[tuple[float, dict[str, Any]]] = []
        self.fail_first = False

    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        return False

    async def run(self, platform_id: int) -> None:
        raise AssertionError("non-pollable handler must not run")

    async def send_session_event(
        self,
        uid: str,
        session_id: str,
        source: str,
        event: dict[str, Any],
    ) -> bool:
        assert uid == "outbox-user"
        assert session_id == "outbox-session"
        assert source == "notification-outbox"
        self.sent.append((time.monotonic(), dict(event)))
        if self.fail_first and len(self.sent) == 1:
            raise RuntimeError("planned first delivery failure")
        return True


@dataclass(slots=True)
class OutboxHarness:
    factory: async_sessionmaker[AsyncSession]
    manager: MessagePlatformPollingManager
    handler: RecordingHandler
    inbox: NotificationInbox
    scan_completed: asyncio.Event
    runtime: NotificationRuntime


@pytest_asyncio.fixture
async def outbox_harness(
    database_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncGenerator[OutboxHarness]:
    async with database_factory(foreign_keys=True, wal=True) as factory:
        async with factory() as db:
            db.add(
                ChatSession(
                    uid="outbox-user",
                    session_id="outbox-session",
                    source="notification-outbox",
                )
            )
            await db.commit()

        monkeypatch.setattr(manager_module, "AsyncSessionLocal", factory)
        scan_completed = asyncio.Event()
        get_next_attempt_at = message_platform_outbox_crud.get_next_attempt_at

        async def observe_scan(db: AsyncSession):
            next_attempt_at = await get_next_attempt_at(db)
            scan_completed.set()
            return next_attempt_at

        monkeypatch.setattr(
            message_platform_outbox_crud,
            "get_next_attempt_at",
            observe_scan,
        )

        token = secrets.token_hex(32)
        server = NotificationServer(NotificationConfig(host="127.0.0.1", port=0, token=token))
        runtime: NotificationRuntime | None = None
        manager: MessagePlatformPollingManager | None = None
        try:
            await server.start()
            host, port = server.address
            client = NotificationClient(
                NotificationConfig(host=host, port=port, token=token),
                "notification-outbox-test",
            )
            engine = factory.kw["bind"]
            runtime = NotificationRuntime(client, bind=engine.sync_engine)
            inbox = runtime.subscribe((NotificationTopic.OUTBOX,))
            runtime.start()
            await asyncio.wait_for(runtime.wait_ready(), timeout=3)

            handler = RecordingHandler()
            manager = MessagePlatformPollingManager((handler,))
            manager.start(outbox_inbox=inbox)
            await asyncio.wait_for(scan_completed.wait(), timeout=3)
            yield OutboxHarness(
                factory=factory,
                manager=manager,
                handler=handler,
                inbox=inbox,
                scan_completed=scan_completed,
                runtime=runtime,
            )
        finally:
            try:
                if manager is not None:
                    await manager.stop()
            finally:
                try:
                    if runtime is not None:
                        await runtime.stop()
                finally:
                    await server.stop()


async def _wait_for_scan(harness: OutboxHarness) -> None:
    await asyncio.wait_for(harness.scan_completed.wait(), timeout=3)


async def _get_item(
    factory: async_sessionmaker[AsyncSession],
    item_id: int,
) -> MessagePlatformOutbox | None:
    async with factory() as db:
        return await db.get(MessagePlatformOutbox, item_id)


async def _wait_for_item(
    factory: async_sessionmaker[AsyncSession],
    item_id: int,
    condition: Callable[[MessagePlatformOutbox], bool],
    *,
    timeout: float = 3,
) -> MessagePlatformOutbox:
    async def wait_until_ready() -> MessagePlatformOutbox:
        while True:
            item = await _get_item(factory, item_id)
            if item is not None and condition(item):
                return item
            await asyncio.sleep(0.01)

    return await asyncio.wait_for(wait_until_ready(), timeout=timeout)


async def _enqueue(
    factory: async_sessionmaker[AsyncSession],
    *,
    dedupe_key: str,
    event: dict[str, Any],
) -> tuple[MessagePlatformOutbox, bool]:
    async with factory() as db:
        return await message_platform_outbox_crud.enqueue(
            db,
            dedupe_key=dedupe_key,
            uid="outbox-user",
            session_id="outbox-session",
            source="notification-outbox",
            event=event,
        )


async def _insert_item(
    factory: async_sessionmaker[AsyncSession],
    *,
    dedupe_key: str,
    status: MessagePlatformOutboxStatus = MessagePlatformOutboxStatus.PENDING,
    attempt_count: int = 0,
    due_in_seconds: float = 0,
    lock_owner: str | None = None,
) -> MessagePlatformOutbox:
    now = get_local_time()
    item = MessagePlatformOutbox(
        dedupe_key=dedupe_key,
        uid="outbox-user",
        session_id="outbox-session",
        source="notification-outbox",
        event={"type": "proactive_reply", "content": dedupe_key},
        status=status,
        attempt_count=attempt_count,
        next_attempt_at=now + timedelta(seconds=due_in_seconds),
        locked_by=lock_owner,
        lock_until=now + timedelta(seconds=due_in_seconds) if lock_owner else None,
    )
    async with factory() as db:
        db.add(item)
        await db.commit()
        await db.refresh(item)
    return item


async def _restart_manager(harness: OutboxHarness) -> None:
    harness.scan_completed.clear()
    harness.manager.start(outbox_inbox=harness.inbox)
    await _wait_for_scan(harness)


@pytest.mark.asyncio
async def test_tcp_notification_delivers_one_deduplicated_outbox_event(
    outbox_harness: OutboxHarness,
) -> None:
    payload = {
        "type": "proactive_reply",
        "event_id": "notification-outbox-event-1",
        "content": "delivered through the notification center",
    }
    first, first_created = await _enqueue(
        outbox_harness.factory,
        dedupe_key="notification-outbox-dedupe-1",
        event=payload,
    )
    second, second_created = await _enqueue(
        outbox_harness.factory,
        dedupe_key="notification-outbox-dedupe-1",
        event=payload,
    )
    await outbox_harness.runtime.flush()

    assert first_created is True
    assert second_created is False
    assert first.id == second.id
    assert first.id is not None
    delivered = await _wait_for_item(
        outbox_harness.factory,
        first.id,
        lambda item: item.status == MessagePlatformOutboxStatus.SENT,
    )

    async with outbox_harness.factory() as db:
        items = list((await db.execute(select(MessagePlatformOutbox).order_by(MessagePlatformOutbox.id))).scalars().all())
    assert len(items) == 1
    assert delivered.status == MessagePlatformOutboxStatus.SENT
    assert delivered.attempt_count == 1
    assert [sent_event for _, sent_event in outbox_harness.handler.sent] == [payload]


@pytest.mark.asyncio
async def test_failed_delivery_retries_after_its_due_time_without_another_publish(
    outbox_harness: OutboxHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_delay = 0.15
    monkeypatch.setattr(
        outbox_crud_module,
        "calculate_retry_delay_seconds",
        lambda _attempt: retry_delay,
    )
    outbox_harness.handler.fail_first = True
    payload = {"type": "proactive_reply", "content": "retry this event"}
    item, created = await _enqueue(
        outbox_harness.factory,
        dedupe_key="notification-outbox-retry-1",
        event=payload,
    )
    await outbox_harness.runtime.flush()
    assert created is True
    assert item.id is not None

    delivered = await _wait_for_item(
        outbox_harness.factory,
        item.id,
        lambda current: current.status == MessagePlatformOutboxStatus.SENT and current.attempt_count == 2,
    )
    assert delivered.status == MessagePlatformOutboxStatus.SENT
    assert delivered.attempt_count == 2
    assert [sent_event for _, sent_event in outbox_harness.handler.sent] == [
        payload,
        payload,
    ]
    first_sent_at, second_sent_at = (sent_at for sent_at, _ in outbox_harness.handler.sent)
    assert second_sent_at - first_sent_at >= retry_delay - 0.03


@pytest.mark.asyncio
async def test_future_pending_item_is_sent_when_its_due_time_arrives(
    outbox_harness: OutboxHarness,
) -> None:
    outbox_harness.handler.sent.clear()
    loop = asyncio.get_running_loop()
    await outbox_harness.manager.stop()
    due_not_before = loop.time() + 0.18
    item = await _insert_item(
        outbox_harness.factory,
        dedupe_key="notification-outbox-future-pending",
        due_in_seconds=0.2,
    )
    assert item.id is not None
    await _restart_manager(outbox_harness)

    delivered = await _wait_for_item(
        outbox_harness.factory,
        item.id,
        lambda current: current.status == MessagePlatformOutboxStatus.SENT,
    )
    assert delivered.status == MessagePlatformOutboxStatus.SENT
    assert delivered.attempt_count == 1
    assert len(outbox_harness.handler.sent) == 1
    assert outbox_harness.handler.sent[0][0] >= due_not_before


@pytest.mark.asyncio
async def test_expired_processing_lease_is_claimed_and_delivered(
    outbox_harness: OutboxHarness,
) -> None:
    loop = asyncio.get_running_loop()
    await outbox_harness.manager.stop()
    due_not_before = loop.time() + 0.18
    item = await _insert_item(
        outbox_harness.factory,
        dedupe_key="notification-outbox-expired-processing",
        status=MessagePlatformOutboxStatus.PROCESSING,
        attempt_count=1,
        due_in_seconds=0.2,
        lock_owner="old-worker",
    )
    assert item.id is not None
    await _restart_manager(outbox_harness)

    delivered = await _wait_for_item(
        outbox_harness.factory,
        item.id,
        lambda current: current.status == MessagePlatformOutboxStatus.SENT,
    )
    assert delivered.status == MessagePlatformOutboxStatus.SENT
    assert delivered.attempt_count == 2
    assert len(outbox_harness.handler.sent) == 1
    assert outbox_harness.handler.sent[0][0] >= due_not_before


@pytest.mark.asyncio
async def test_reconciliation_recovers_due_item_inserted_without_notification(
    outbox_harness: OutboxHarness,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(manager_module, "NOTIFICATION_RECONCILE_INTERVAL_SECONDS", 0.1)
    outbox_harness.scan_completed.clear()
    outbox_harness.inbox.wake()
    await _wait_for_scan(outbox_harness)

    item = await _insert_item(
        outbox_harness.factory,
        dedupe_key="notification-outbox-reconciliation",
    )
    assert item.id is not None
    delivered = await _wait_for_item(
        outbox_harness.factory,
        item.id,
        lambda current: current.status == MessagePlatformOutboxStatus.SENT,
        timeout=3,
    )

    assert delivered.status == MessagePlatformOutboxStatus.SENT
    assert delivered.attempt_count == 1
    assert [sent_event for _, sent_event in outbox_harness.handler.sent] == [item.event]


@pytest.mark.asyncio
async def test_idle_manager_does_not_poll_outbox_selects(
    outbox_harness: OutboxHarness,
) -> None:
    engine = outbox_harness.factory.kw["bind"].sync_engine
    outbox_selects: list[str] = []

    def count_outbox_selects(
        _connection,
        _cursor,
        statement: str,
        _parameters,
        _context,
        _executemany,
    ) -> None:
        normalized = statement.lstrip().lower()
        if normalized.startswith("select") and "message_platform_outbox" in normalized:
            outbox_selects.append(statement)

    sqlalchemy_event.listen(engine, "after_cursor_execute", count_outbox_selects)
    try:
        await asyncio.sleep(1.2)
    finally:
        sqlalchemy_event.remove(engine, "after_cursor_execute", count_outbox_selects)

    assert outbox_selects == []
