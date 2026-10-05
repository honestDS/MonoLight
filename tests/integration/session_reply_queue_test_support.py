import asyncio
from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.crud.session.reply_work_item import CRUDSessionReplyWorkItem
from app.models.message import Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkType,
)


@pytest_asyncio.fixture
async def db_session(database_factory) -> AsyncGenerator[AsyncSession]:
    async with database_factory(
        tables=[
            Profile.__table__,
            Message.__table__,
            ChatSession.__table__,
            SessionReplySequence.__table__,
            SessionReplyWorkItem.__table__,
            SessionReplyStreamEvent.__table__,
        ],
    ) as session_factory:
        async with session_factory() as session:
            session.add(Profile(id=1, uid="user-1", name="queue-test", configs={}))
            await session.commit()
            yield session


@pytest_asyncio.fixture
async def concurrent_session_factory(
    database_factory,
) -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    async with database_factory(
        tables=[
            Profile.__table__,
            Message.__table__,
            ChatSession.__table__,
            SessionReplySequence.__table__,
            SessionReplyWorkItem.__table__,
            SessionReplyStreamEvent.__table__,
        ],
        wal=True,
        name="session-reply-queue.sqlite3",
    ) as session_factory:
        async with session_factory() as setup_session:
            setup_session.add(Profile(id=1, uid="user-1", name="queue-test", configs={}))
            await setup_session.commit()
        yield session_factory


class AsyncBarrier:
    def __init__(self, parties: int) -> None:
        self._parties = parties
        self._arrived = 0
        self._lock = asyncio.Lock()
        self._released = asyncio.Event()

    async def wait(self) -> None:
        async with self._lock:
            self._arrived += 1
            if self._arrived == self._parties:
                self._released.set()
        await self._released.wait()


async def add_message(
    db: AsyncSession,
    message_id: int,
    content: str,
) -> Message:
    message = Message(
        id=message_id,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        role=MessageRole.USER,
        type=MessageType.TEXT,
        content=content,
        is_processed=False,
    )
    db.add(message)
    await db.flush()
    return message


async def enqueue(
    crud: CRUDSessionReplyWorkItem,
    db: AsyncSession,
    *,
    work_type: SessionReplyWorkType,
    source_id: int,
    dedupe_key: str,
) -> SessionReplyWorkItem:
    work, _created = await crud.enqueue(
        db,
        uid="user-1",
        session_id="session-1",
        profile_id=1,
        work_type=work_type,
        source_type=SessionReplySourceType.USER_MESSAGE if work_type == SessionReplyWorkType.FOREGROUND_REPLY else SessionReplySourceType.BACKGROUND_TASK,
        source_id=source_id,
        dedupe_key=dedupe_key,
        commit=False,
    )
    return work
