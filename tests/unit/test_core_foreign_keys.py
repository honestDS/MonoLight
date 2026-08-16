from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import ForeignKeyConstraint, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlmodel import SQLModel

from app.core.crud.scheduled_task import scheduled_task_crud
from app.models import ChatSession, Profile, PromptLibrary, ScheduledTask, SessionEvent
from app.models.scheduled_task import ScheduledTaskStatus

EXPECTED_FOREIGN_KEY_CONSTRAINTS = {
    # audit
    ("audit_tool_detail", ("audit_record_id",), "audit_record", ("id",), "CASCADE"),
    (
        "audit_confirmation_claim",
        ("audit_record_id", "uid", "session_id"),
        "audit_record",
        ("id", "uid", "session_id"),
        "CASCADE",
    ),
    (
        "audit_execution_record",
        ("audit_tool_detail_id", "audit_record_id"),
        "audit_tool_detail",
        ("id", "audit_record_id"),
        "CASCADE",
    ),
    ("audit_execution_record", ("audit_record_id",), "audit_record", ("id",), "CASCADE"),
    # knowledge base
    ("knowledge_base", ("embedding_channel_id",), "channel", ("id",), "RESTRICT"),
    ("knowledge_base_profile_binding", ("knowledge_base_id",), "knowledge_base", ("id",), "CASCADE"),
    ("knowledge_base_profile_binding", ("profile_id",), "profile", ("id",), "CASCADE"),
    ("knowledge_base_document", ("knowledge_base_id",), "knowledge_base", ("id",), "CASCADE"),
    # session, profile, platform, and scheduled task
    ("profile", ("prompt_id",), "prompt", ("id",), "RESTRICT"),
    ("chat_session", ("profile_override_id",), "profile", ("id",), "RESTRICT"),
    ("message_platform", ("profile_id",), "profile", ("id",), "RESTRICT"),
    (
        "session_event",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    (
        "message",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    ("session_reply_stream_event", ("work_id",), "session_reply_work_item", ("id",), "CASCADE"),
    ("scheduled_task", ("profile_id",), "profile", ("id",), "RESTRICT"),
    (
        "scheduled_task",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    (
        "message_platform_outbox",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    # context summary and reply work
    (
        "context_summary_stage",
        ("work_id", "session_id", "uid"),
        "session_reply_work_item",
        ("id", "session_id", "uid"),
        "CASCADE",
    ),
    (
        "context_summary_fragment",
        ("work_id", "session_id", "uid"),
        "session_reply_work_item",
        ("id", "session_id", "uid"),
        "CASCADE",
    ),
    (
        "session_reply_work_item",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    ("session_reply_sequence", ("session_id",), "chat_session", ("session_id",), "CASCADE"),
    # terminal
    (
        "terminal_session",
        ("session_id", "uid"),
        "chat_session",
        ("session_id", "uid"),
        "CASCADE",
    ),
    ("terminal_control_command", ("terminal_session_id",), "terminal_session", ("terminal_session_id",), "CASCADE"),
    # long-term memory
    ("long_term_memory_store", ("active_embedding_channel_id",), "channel", ("id",), "RESTRICT"),
    ("long_term_memory_store", ("target_embedding_channel_id",), "channel", ("id",), "RESTRICT"),
    ("long_term_memory_store", ("organization_channel_id",), "channel", ("id",), "RESTRICT"),
    ("long_term_memory_embedding_selection_token", ("profile_id",), "profile", ("id",), "CASCADE"),
    (
        "long_term_memory_embedding_selection_token",
        ("target_embedding_channel_id",),
        "channel",
        ("id",),
        "CASCADE",
    ),
}


def _normalize_foreign_key_constraints() -> set[tuple[str, tuple[str, ...], str, tuple[str, ...], str | None]]:
    normalized = set()
    for table in SQLModel.metadata.tables.values():
        for constraint in table.constraints:
            if not isinstance(constraint, ForeignKeyConstraint):
                continue
            elements = tuple(constraint.elements)
            normalized.add(
                (
                    table.name,
                    tuple(element.parent.name for element in elements),
                    elements[0].column.table.name,
                    tuple(element.column.name for element in elements),
                    elements[0].ondelete,
                )
            )
    return normalized


def test_core_foreign_key_metadata_matches_design():
    assert _normalize_foreign_key_constraints() == EXPECTED_FOREIGN_KEY_CONSTRAINTS


@pytest_asyncio.fixture
async def db_session_factory() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_sqlite_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
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
                    SessionEvent.__table__,
                    ScheduledTask.__table__,
                ],
            )
        )
    try:
        yield async_sessionmaker(engine, expire_on_commit=False)
    finally:
        await engine.dispose()


async def _create_profile_with_scheduled_task(db: AsyncSession) -> tuple[Profile, ChatSession, ScheduledTask]:
    profile = Profile(uid="user-1", name="test-profile", configs={})
    chat_session = ChatSession(session_id="session-1", uid="user-1")
    db.add_all([profile, chat_session])
    await db.flush()
    assert profile.id is not None

    scheduled_task = ScheduledTask(
        name="test-task",
        uid="user-1",
        session_id="session-1",
        profile_id=profile.id,
        message="run",
        interval_seconds=60,
        status=ScheduledTaskStatus.ENABLED,
    )
    db.add(scheduled_task)
    await db.commit()
    return profile, chat_session, scheduled_task


@pytest.mark.asyncio
async def test_sqlite_enforces_session_owner_foreign_key_and_cascade(db_session_factory):
    async with db_session_factory() as db:
        chat_session = ChatSession(session_id="session-1", uid="user-1")
        session_event = SessionEvent(
            dedupe_key="event-1",
            uid="user-1",
            session_id="session-1",
            event={"type": "test"},
        )
        db.add(chat_session)
        await db.flush()
        db.add(session_event)
        await db.commit()
        session_event_id = session_event.id
        assert session_event_id is not None

        db.add(
            SessionEvent(
                dedupe_key="event-2",
                uid="wrong-user",
                session_id="session-1",
                event={"type": "test"},
            )
        )
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        await db.delete(chat_session)
        await db.commit()
        assert await db.get(SessionEvent, session_event_id) is None


@pytest.mark.asyncio
async def test_sqlite_restricts_profile_deletion_with_scheduled_task(db_session_factory):
    async with db_session_factory() as db:
        profile, _chat_session, scheduled_task = await _create_profile_with_scheduled_task(db)
        profile_id = profile.id
        scheduled_task_id = scheduled_task.id
        assert profile_id is not None
        assert scheduled_task_id is not None

        await db.delete(profile)
        with pytest.raises(IntegrityError):
            await db.commit()
        await db.rollback()

        assert await db.get(Profile, profile_id) is not None
        assert await db.get(ScheduledTask, scheduled_task_id) is not None


@pytest.mark.asyncio
async def test_scheduled_task_crud_has_profile_assignment(db_session_factory):
    async with db_session_factory() as db:
        profile, _chat_session, _scheduled_task = await _create_profile_with_scheduled_task(db)
        assert profile.id is not None

        assert await scheduled_task_crud.has_profile_assignment(db, profile.id) is True
        assert await scheduled_task_crud.has_profile_assignment(db, profile.id + 1) is False
