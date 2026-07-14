from collections.abc import AsyncGenerator
from importlib import import_module

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.message import message_crud
from app.core.crud.session import session_crud
from app.models.context_summary_stage import ContextSummaryFragment, ContextSummaryStage
from app.models.message import Message, MessageRole, MessageType
from app.models.session import ChatSession


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
                    ContextSummaryStage.__table__,
                    ContextSummaryFragment.__table__,
                ],
            )
        )
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


@pytest.mark.asyncio
async def test_context_summary_update_compares_boundary_and_revision_atomically(
    db_session: AsyncSession,
):
    session = ChatSession(
        session_id="session-1",
        uid="user-1",
        context_summary="old summary",
        context_summary_message_id=8,
        context_summary_revision=3,
    )
    db_session.add(session)
    await db_session.commit()

    first_updated = await session_crud.update_context_summary(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_message_id=8,
        expected_revision=3,
        summary="first candidate",
        message_id=12,
    )
    await db_session.commit()

    stale_same_boundary_updated = await session_crud.update_context_summary(
        db_session,
        session_id="session-1",
        uid="user-1",
        expected_message_id=12,
        expected_revision=3,
        summary="stale candidate",
        message_id=12,
    )
    await db_session.rollback()

    await db_session.refresh(session)
    assert first_updated is True
    assert stale_same_boundary_updated is False
    assert session.context_summary == "first candidate"
    assert session.context_summary_message_id == 12
    assert session.context_summary_revision == 4


async def _seed_messages(db: AsyncSession) -> None:
    for message_id in range(1, 9):
        db.add(
            Message(
                id=message_id,
                uid="user-1" if message_id != 6 else "other-user",
                session_id="session-1" if message_id != 7 else "other-session",
                profile_id=1,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content=f"message-{message_id}",
                is_processed=True,
            )
        )
    await db.commit()


@pytest.mark.asyncio
async def test_message_id_forward_cursor_pages_fixed_open_range(db_session: AsyncSession):
    await _seed_messages(db_session)

    first_page = await message_crud.get_history_forward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        limit=2,
    )
    second_page = await message_crud.get_history_forward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        page_after_id=first_page[-1].id,
        limit=2,
    )
    third_page = await message_crud.get_history_forward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        page_after_id=second_page[-1].id,
        limit=2,
    )

    assert [message.id for message in first_page] == [2, 3]
    assert [message.id for message in second_page] == [4, 5]
    assert [message.id for message in third_page] == []


@pytest.mark.asyncio
async def test_message_id_backward_cursor_pages_fixed_open_range(db_session: AsyncSession):
    await _seed_messages(db_session)

    first_page = await message_crud.get_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        limit=2,
    )
    second_page = await message_crud.get_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        page_before_id=first_page[-1].id,
        limit=2,
    )
    third_page = await message_crud.get_history_backward_by_id(
        db_session,
        session_id="session-1",
        uid="user-1",
        after_id=1,
        before_id=8,
        page_before_id=second_page[-1].id,
        limit=2,
    )

    assert [message.id for message in first_page] == [5, 4]
    assert [message.id for message in second_page] == [3, 2]
    assert [message.id for message in third_page] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, 501])
async def test_message_id_cursor_rejects_unbounded_page_size(
    db_session: AsyncSession,
    limit: int,
):
    with pytest.raises(ValueError):
        await message_crud.get_history_forward_by_id(
            db_session,
            session_id="session-1",
            uid="user-1",
            limit=limit,
        )
    with pytest.raises(ValueError):
        await message_crud.get_history_backward_by_id(
            db_session,
            session_id="session-1",
            uid="user-1",
            limit=limit,
        )


@pytest.mark.asyncio
async def test_context_summary_storage_migration_upgrades_legacy_schema():
    migration = import_module("scripts.migration_20260714_add_context_summary_stages")
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with session_factory() as session:
        await session.execute(
            text(
                """
                CREATE TABLE chat_session (
                    session_id VARCHAR(100) NOT NULL PRIMARY KEY,
                    uid VARCHAR(100) NOT NULL,
                    context_summary TEXT,
                    context_summary_message_id INTEGER
                )
                """
            )
        )
        await session.execute(
            text(
                """
                INSERT INTO chat_session (
                    session_id,
                    uid,
                    context_summary,
                    context_summary_message_id
                ) VALUES (
                    'session-1',
                    'user-1',
                    'old summary',
                    12
                )
                """
            )
        )
        await migration.migrate(session)
        await migration.migrate(session)
        await session.commit()

        columns = await session.execute(text("PRAGMA table_info(chat_session)"))
        column_names = {str(row[1]) for row in columns.fetchall()}
        revision = await session.execute(text("SELECT context_summary_revision FROM chat_session WHERE session_id = 'session-1'"))
        tables = await session.execute(text("SELECT name FROM sqlite_master WHERE type = 'table' AND name IN ('context_summary_stage', 'context_summary_fragment')"))
        indexes = await session.execute(text("SELECT name FROM sqlite_master WHERE type = 'index' AND name IN ('ix_context_summary_stage_status', 'ix_context_summary_fragment_stage_key')"))

        assert "context_summary_revision" in column_names
        assert revision.scalar_one() == 0
        assert {str(row[0]) for row in tables.fetchall()} == {
            "context_summary_stage",
            "context_summary_fragment",
        }
        assert {str(row[0]) for row in indexes.fetchall()} == {
            "ix_context_summary_stage_status",
            "ix_context_summary_fragment_stage_key",
        }

    await engine.dispose()


def test_context_summary_storage_models_use_stable_work_and_stage_identity():
    stage_columns = ContextSummaryStage.__table__.columns
    fragment_columns = ContextSummaryFragment.__table__.columns

    assert "work_dedupe_key" in stage_columns
    assert "stage_key" in stage_columns
    assert "lower_stage_key" in stage_columns
    assert "expected_summary_revision" in stage_columns
    assert "work_dedupe_key" in fragment_columns
    assert "stage_key" in fragment_columns
    assert "dedupe_key" in fragment_columns
    assert ChatSession(session_id="session-1", uid="user-1").context_summary_revision == 0
