import asyncio
from collections.abc import AsyncGenerator
from datetime import timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

import app.core.session_notifier as session_notifier_module
from app.core.crud.session.event import session_event_crud
from app.core.session_notifier import SessionNotifier
from app.core.utils.time import get_local_time
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from tests.database_support import clone_sqlite_schema

UID = "session-event-delivery-user"
OLD_SESSION_ID = "session-event-delivery-old"
CURRENT_SESSION_ID = "session-event-delivery-current"


@pytest_asyncio.fixture
async def session_factory(tmp_path: Path) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    database_path = tmp_path / "session-event-delivery.sqlite3"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{database_path.as_posix()}",
        connect_args={"timeout": 30},
    )

    @event.listens_for(engine.sync_engine, "connect")
    def configure_sqlite_connection(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    await clone_sqlite_schema(
        database_path,
        tables=[
            PromptLibrary.__table__,
            Profile.__table__,
            ChatSession.__table__,
            SessionEvent.__table__,
        ],
    )
    factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_sessions_and_old_event(
    factory: async_sessionmaker[AsyncSession],
) -> int:
    async with factory() as db:
        db.add_all(
            [
                ChatSession(session_id=OLD_SESSION_ID, uid=UID),
                ChatSession(session_id=CURRENT_SESSION_ID, uid=UID),
            ]
        )
        await db.commit()
        old_event, created = await session_event_crud.publish(
            db,
            dedupe_key="session-event-delivery-old",
            uid=UID,
            session_id=OLD_SESSION_ID,
            event={"type": "old_notification"},
        )
        assert created is True
        assert old_event.id is not None
        return old_event.id


@pytest.mark.asyncio
@pytest.mark.parametrize("removal", ["delete_session", "expire"])
async def test_session_event_delivery_survives_old_notification_removal(
    session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    removal: str,
) -> None:
    monkeypatch.setattr(session_notifier_module, "AsyncSessionLocal", session_factory)
    old_event_id = await _seed_sessions_and_old_event(session_factory)
    notifiers = [SessionNotifier(), SessionNotifier()]
    queues = [asyncio.Queue(), asyncio.Queue()]
    payload = {
        "type": "proactive_reply",
        "source": "background_task",
        "background_task_id": 42,
        "session_id": CURRENT_SESSION_ID,
        "content": "图片已生成",
        "files": [
            {
                "id": "generated-image-42",
                "name": "generated.png",
                "mime_type": "image/png",
                "download_url": "/api/v1/files/generated-image-42",
                "previewable": True,
            }
        ],
    }

    try:
        for notifier, queue in zip(notifiers, queues, strict=True):
            await notifier.register(UID, CURRENT_SESSION_ID, queue)
            await notifier.start()

        async with session_factory() as db:
            old_event = await db.get(SessionEvent, old_event_id)
            assert old_event is not None
            if removal == "delete_session":
                assert (
                    await session_event_crud.delete_by_session(
                        db,
                        session_id=OLD_SESSION_ID,
                        uid=UID,
                    )
                    == 1
                )
            else:
                old_event.created_at = get_local_time() - timedelta(hours=25)
                await db.commit()
                await session_event_crud.cleanup_expired(db)

        assert await notifiers[0].notify(UID, CURRENT_SESSION_ID, payload) is True
        delivered = [await asyncio.wait_for(queue.get(), timeout=2) for queue in queues]
        assert delivered[0] == delivered[1]
        assert delivered[0]["type"] == payload["type"]
        assert delivered[0]["source"] == payload["source"]
        assert delivered[0]["background_task_id"] == payload["background_task_id"]
        assert delivered[0]["session_id"] == CURRENT_SESSION_ID
        assert delivered[0]["content"] == payload["content"]
        assert delivered[0]["files"] == payload["files"]
        assert delivered[0]["event_sequence_no"] > old_event_id

        assert await notifiers[0].notify(UID, CURRENT_SESSION_ID, payload) is False
        assert queues[0].empty()
        assert queues[1].empty()
    finally:
        await asyncio.gather(*(notifier.stop() for notifier in notifiers))
