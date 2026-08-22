import asyncio
from datetime import UTC, datetime

import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.dispatcher.save_message import save_message
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.providers.database import AsyncSessionLocal, engine
from app.providers.database.client import CancellationSafeAsyncSession

TEST_SESSION_ID = "message-dedupe-session"
TEST_UID = "message-dedupe-user"


@pytest.fixture(autouse=True)
async def clean_message_table():
    async with engine.begin() as connection:
        await connection.run_sync(lambda sync_connection: PromptLibrary.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: Profile.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: ChatSession.__table__.create(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: Message.__table__.drop(sync_connection, checkfirst=True))
        await connection.run_sync(lambda sync_connection: Message.__table__.create(sync_connection))

    async with AsyncSessionLocal() as db:
        await db.execute(delete(ChatSession).where(ChatSession.session_id == TEST_SESSION_ID))
        db.add(ChatSession(session_id=TEST_SESSION_ID, uid=TEST_UID))
        await db.commit()

    try:
        yield
    finally:
        async with AsyncSessionLocal() as db:
            await db.execute(delete(Message))
            await db.execute(delete(ChatSession).where(ChatSession.session_id == TEST_SESSION_ID))
            await db.commit()


@pytest.mark.asyncio
async def test_save_message_is_idempotent_by_dedupe_key():
    message = InternalMessage(role=MessageRole.ERR, content="reply failed", environment_prompt="internal notice")
    dedupe_key = "background-task:1:reply-error"

    async with AsyncSessionLocal() as db:
        first = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.ERR,
            MessageType.TEXT,
            message,
            1,
            dedupe_key=dedupe_key,
        )
        repeated = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.ERR,
            MessageType.TEXT,
            message,
            1,
            dedupe_key=dedupe_key,
        )
        count = await db.scalar(select(func.count()).select_from(Message).where(Message.dedupe_key == dedupe_key))
        persisted = await db.scalar(select(Message).where(Message.dedupe_key == dedupe_key))

    assert first.id == repeated.id
    assert first.environment_prompt == "internal notice"
    assert persisted.environment_prompt == "internal notice"
    assert count == 1


@pytest.mark.asyncio
async def test_save_message_persists_outbound_text_refinement_as_plain_text():
    async with AsyncSessionLocal() as db:
        saved = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.USER,
            MessageType.OUTBOUND_TEXT_REFINEMENT,
            InternalMessage(role=MessageRole.USER, content="refinement prompt"),
            1,
        )
        persisted = await db.get(Message, saved.id)

    assert persisted is not None
    assert persisted.type == MessageType.OUTBOUND_TEXT_REFINEMENT
    assert persisted.content == "refinement prompt"


@pytest.mark.asyncio
async def test_save_message_persists_explicit_created_at():
    created_at = datetime(2026, 7, 21, 6, 0, tzinfo=UTC)

    async with AsyncSessionLocal() as db:
        saved = await save_message(
            db,
            TEST_SESSION_ID,
            TEST_UID,
            MessageRole.ASSISTANT,
            MessageType.TEXT,
            InternalMessage(role=MessageRole.ASSISTANT, content="reply"),
            1,
            created_at=created_at,
        )
        persisted = await db.get(Message, saved.id)

    assert persisted is not None
    assert persisted.created_at.replace(tzinfo=created_at.tzinfo) == created_at


@pytest.mark.asyncio
@pytest.mark.parametrize("method_name", ["commit", "rollback", "close"])
async def test_session_finishes_database_cleanup_before_propagating_cancellation(monkeypatch, method_name):
    started = asyncio.Event()
    release = asyncio.Event()
    completed = asyncio.Event()

    async def delayed_operation(_session):
        started.set()
        await release.wait()
        completed.set()

    monkeypatch.setattr(AsyncSession, method_name, delayed_operation)
    session = CancellationSafeAsyncSession()
    operation = asyncio.create_task(getattr(session, method_name)())
    await started.wait()

    operation.cancel()
    await asyncio.sleep(0)

    assert not operation.done()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await operation
    assert completed.is_set()
