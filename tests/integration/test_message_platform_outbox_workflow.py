from collections.abc import AsyncGenerator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel, select

from app.core.message_platforms import manager as manager_module
from app.core.message_platforms import notifier as notifier_module
from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.manager import MessagePlatformPollingManager
from app.models.message_platform import MessagePlatform, MessagePlatformType
from app.models.message_platform_outbox import MessagePlatformOutbox, MessagePlatformOutboxStatus
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession


class RecordingExternalHandler(MessagePlatformHandler):
    platform_type = MessagePlatformType.WEIXIN_OPENCLAW
    sources = frozenset({"outbox-workflow"})

    def __init__(self) -> None:
        self.sent_events: list[dict[str, Any]] = []

    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        return False

    async def run(self, platform_id: int) -> None:
        return None

    async def send_session_event(
        self,
        uid: str,
        session_id: str,
        source: str,
        event: dict[str, Any],
    ) -> bool:
        assert uid == "outbox-user"
        assert session_id == "outbox-session"
        assert source == "outbox-workflow"
        self.sent_events.append(event)
        return True


@pytest_asyncio.fixture
async def outbox_session_factory(tmp_path) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'message-platform-outbox-workflow.db'}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
        finally:
            cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    PromptLibrary.__table__,
                    Profile.__table__,
                    ChatSession.__table__,
                    MessagePlatform.__table__,
                    MessagePlatformOutbox.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as db:
        db.add(
            ChatSession(
                session_id="outbox-session",
                uid="outbox-user",
                source="outbox-workflow",
                reply_target_source="outbox-workflow",
            )
        )
        await db.commit()
    try:
        yield session_factory
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_external_session_event_is_idempotently_queued_claimed_delivered_and_marked_sent(
    outbox_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(notifier_module, "AsyncSessionLocal", outbox_session_factory)
    monkeypatch.setattr(manager_module, "AsyncSessionLocal", outbox_session_factory)

    handler = RecordingExternalHandler()
    manager = MessagePlatformPollingManager((handler,))
    event_payload = {
        "event_id": "reply-work:41:event",
        "type": "proactive_reply",
        "source": "background_task",
        "session_id": "outbox-session",
        "work_id": 41,
        "background_task_id": 9,
        "content": "delivery completed",
        "history": [],
        "files": [],
    }

    await notifier_module.send_session_event(
        "outbox-user",
        "outbox-session",
        event_payload,
    )
    await notifier_module.send_session_event(
        "outbox-user",
        "outbox-session",
        event_payload,
    )

    async with outbox_session_factory() as db:
        queued_items = list((await db.execute(select(MessagePlatformOutbox).order_by(MessagePlatformOutbox.id))).scalars().all())
    assert len(queued_items) == 1
    queued = queued_items[0]
    assert queued.status == MessagePlatformOutboxStatus.PENDING
    assert queued.attempt_count == 0
    assert queued.source == "outbox-workflow"
    assert queued.event == event_payload

    processed_count = await manager.process_outbox_batch()
    assert processed_count == 1
    assert handler.sent_events == [event_payload]

    async with outbox_session_factory() as db:
        delivered = await db.get(MessagePlatformOutbox, queued.id)
    assert delivered is not None
    assert delivered.status == MessagePlatformOutboxStatus.SENT
    assert delivered.attempt_count == 1
    assert delivered.sent_at is not None
    assert delivered.locked_by is None
    assert delivered.lock_until is None

    assert await manager.process_outbox_batch() == 0
    assert handler.sent_events == [event_payload]
