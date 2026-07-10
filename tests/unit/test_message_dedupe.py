import pytest
from sqlalchemy import delete, func, select

from app.core.utils.dispatcher.save_message import save_message
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.providers.database import AsyncSessionLocal, engine


@pytest.fixture(autouse=True)
async def clean_message_table():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: Message.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: Message.__table__.create(sync_connection))
    yield
    async with AsyncSessionLocal() as db:
        await db.execute(delete(Message))
        await db.commit()


@pytest.mark.asyncio
async def test_save_message_is_idempotent_by_dedupe_key():
    message = InternalMessage(role=MessageRole.ERR, content="reply failed")
    dedupe_key = "background-task:1:reply-error"

    async with AsyncSessionLocal() as db:
        first = await save_message(
            db,
            "session-1",
            "user-1",
            MessageRole.ERR,
            MessageType.TEXT,
            message,
            1,
            dedupe_key=dedupe_key,
        )
        repeated = await save_message(
            db,
            "session-1",
            "user-1",
            MessageRole.ERR,
            MessageType.TEXT,
            message,
            1,
            dedupe_key=dedupe_key,
        )
        count = await db.scalar(select(func.count()).select_from(Message).where(Message.dedupe_key == dedupe_key))

    assert first.id == repeated.id
    assert count == 1
