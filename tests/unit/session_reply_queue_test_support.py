from collections.abc import AsyncGenerator

import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.session_reply_work_item import CRUDSessionReplyWorkItem
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkType,
)


@pytest_asyncio.fixture
async def db_session() -> AsyncGenerator[AsyncSession]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(
            lambda sync_connection: SQLModel.metadata.create_all(
                sync_connection,
                tables=[
                    Message.__table__,
                    ChatSession.__table__,
                    SessionReplySequence.__table__,
                    SessionReplyWorkItem.__table__,
                    SessionReplyStreamEvent.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


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
