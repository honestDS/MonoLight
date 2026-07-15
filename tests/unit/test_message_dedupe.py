from importlib import import_module

import pytest
from sqlalchemy import delete, func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

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
    message = InternalMessage(role=MessageRole.ERR, content="reply failed", environment_prompt="internal notice")
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
        persisted = await db.scalar(select(Message).where(Message.dedupe_key == dedupe_key))

    assert first.id == repeated.id
    assert first.environment_prompt == "internal notice"
    assert persisted.environment_prompt == "internal notice"
    assert count == 1


@pytest.mark.asyncio
async def test_message_environment_prompt_migration_upgrades_legacy_schema():
    migration = import_module("scripts.migration_20260715_migrate_message_environment_prompt")
    migration_engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(migration_engine, expire_on_commit=False)

    async with session_factory() as session:
        await session.execute(text("CREATE TABLE message (id INTEGER PRIMARY KEY, content TEXT, system_prompt TEXT)"))
        await session.execute(text("INSERT INTO message (id, content, system_prompt) VALUES (1, 'hello', 'legacy environment')"))
        await migration.migrate(session)
        await migration.migrate(session)
        await session.commit()

        columns = await session.execute(text("PRAGMA table_info(message)"))
        column_names = {str(row[1]) for row in columns.fetchall()}
        environment_prompt = await session.scalar(text("SELECT environment_prompt FROM message WHERE id = 1"))

    await migration_engine.dispose()
    assert "environment_prompt" in column_names
    assert environment_prompt == "legacy environment"
