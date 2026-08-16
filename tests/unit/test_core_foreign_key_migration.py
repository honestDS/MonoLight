from collections.abc import AsyncGenerator

import pytest
import pytest_asyncio
from sqlalchemy import event, inspect, text
from sqlalchemy.dialects import mysql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.schema import AddConstraint, CreateTable
from sqlmodel import SQLModel

from app.core.terminal.schemas import TerminalAction, TerminalSessionStatus
from app.models.channel import ModelChannel
from app.models.memory import LongTermMemoryStore
from app.models.profile import Profile
from app.models.prompt import PromptLibrary
from app.models.session import ChatSession
from app.models.session_event import SessionEvent
from app.models.terminal_session import TerminalControlCommand, TerminalSession
from scripts import migration_20260816_add_core_foreign_keys as migration

PARENT_TABLES = (
    PromptLibrary.__table__,
    Profile.__table__,
    ChatSession.__table__,
    ModelChannel.__table__,
)
LEGACY_TABLES = (
    SessionEvent.__table__,
    TerminalSession.__table__,
    TerminalControlCommand.__table__,
    LongTermMemoryStore.__table__,
)


@pytest_asyncio.fixture
async def legacy_database() -> AsyncGenerator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")

    @event.listens_for(engine.sync_engine, "connect")
    def enable_foreign_keys(dbapi_connection, _connection_record) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
        finally:
            cursor.close()

    async with engine.begin() as connection:
        await connection.run_sync(_create_legacy_schema)

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        yield session_factory
    finally:
        await engine.dispose()


def _create_legacy_schema(sync_connection) -> None:
    SQLModel.metadata.create_all(sync_connection, tables=PARENT_TABLES)
    for target_table in LEGACY_TABLES:
        sync_connection.execute(CreateTable(target_table, include_foreign_key_constraints=set()))


@pytest.mark.asyncio
async def test_core_foreign_key_migration_repairs_data_is_idempotent_and_adds_constraints(
    legacy_database: async_sessionmaker[AsyncSession],
) -> None:
    async with legacy_database() as session:
        session.add_all(
            [
                ChatSession(session_id="valid-session", uid="user-1"),
                SessionEvent(
                    id=1,
                    dedupe_key="valid-event",
                    uid="user-1",
                    session_id="valid-session",
                    event={"type": "message"},
                ),
                SessionEvent(
                    id=2,
                    dedupe_key="orphan-event",
                    uid="user-1",
                    session_id="missing-session",
                    event={"type": "message"},
                ),
                TerminalSession(
                    terminal_session_id="orphan-terminal",
                    uid="user-1",
                    session_id="missing-session",
                    original_tool_call_id="orphan-tool-call",
                    profile_id=1,
                    command="python -i",
                    working_directory=".",
                    status=TerminalSessionStatus.EXITED,
                    allowed_actions=[TerminalAction.STATUS.value],
                ),
                TerminalControlCommand(
                    id=1,
                    terminal_session_id="orphan-terminal",
                    request_id="orphan-request",
                    action=TerminalAction.CLOSE,
                    payload={},
                    payload_hash="orphan-payload-hash",
                ),
                LongTermMemoryStore(id=1, uid="memory-user"),
            ]
        )
        await session.commit()

        await migration.migrate(session)
        await session.commit()
        assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        await migration.migrate(session)
        await session.commit()
        assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1

    async with legacy_database() as session:
        assert await session.get(SessionEvent, 1) is not None
        assert await session.get(SessionEvent, 2) is None
        assert await session.get(TerminalSession, "orphan-terminal") is None
        assert await session.get(TerminalControlCommand, 1) is None
        assert await session.get(LongTermMemoryStore, 1) is not None

        connection = await session.connection()
        foreign_keys = await connection.run_sync(_inspect_foreign_keys)
        matches_target = await connection.run_sync(lambda sync_connection: {table_name: migration._sqlite_table_matches_target(sync_connection, table_name) for table_name in ("session_event", "terminal_session", "terminal_control_command", "long_term_memory_store")})
        violations = (await session.execute(text("PRAGMA foreign_key_check"))).all()
        assert violations == []

    assert foreign_keys == {
        "session_event": {
            (("session_id", "uid"), "chat_session", ("session_id", "uid"), "CASCADE"),
        },
        "terminal_session": {
            (("session_id", "uid"), "chat_session", ("session_id", "uid"), "CASCADE"),
        },
        "terminal_control_command": {
            (("terminal_session_id",), "terminal_session", ("terminal_session_id",), "CASCADE"),
        },
        "long_term_memory_store": {
            (("active_embedding_channel_id",), "channel", ("id",), "RESTRICT"),
            (("target_embedding_channel_id",), "channel", ("id",), "RESTRICT"),
            (("organization_channel_id",), "channel", ("id",), "RESTRICT"),
        },
    }
    assert matches_target == {
        "session_event": True,
        "terminal_session": True,
        "terminal_control_command": True,
        "long_term_memory_store": True,
    }


@pytest.mark.asyncio
async def test_core_foreign_key_migration_rejects_active_terminal_orphan(
    legacy_database: async_sessionmaker[AsyncSession],
) -> None:
    async with legacy_database() as session:
        session.add(
            TerminalSession(
                terminal_session_id="active-orphan-terminal",
                uid="user-1",
                session_id="missing-session",
                original_tool_call_id="active-orphan-tool-call",
                profile_id=1,
                command="python -i",
                working_directory=".",
                status=TerminalSessionStatus.RUNNING,
                allowed_actions=[TerminalAction.STATUS.value],
            )
        )
        await session.commit()

        with pytest.raises(RuntimeError) as exc_info:
            await migration.migrate(session)
        assert (await session.execute(text("PRAGMA foreign_keys"))).scalar_one() == 1
        await session.rollback()

    assert "terminal_session.session_owner" in str(exc_info.value)

    async with legacy_database() as session:
        assert await session.get(TerminalSession, "active-orphan-terminal") is not None


@pytest.mark.asyncio
async def test_sqlite_table_matches_target_after_core_foreign_key_migration(
    legacy_database: async_sessionmaker[AsyncSession],
) -> None:
    async with legacy_database() as session:
        await migration.migrate(session)
        await session.commit()

        connection = await session.connection()
        matches_target = await connection.run_sync(lambda sync_connection: {table_name: migration._sqlite_table_matches_target(sync_connection, table_name) for table_name in ("session_event", "long_term_memory_store")})

    assert matches_target == {
        "session_event": True,
        "long_term_memory_store": True,
    }


def _inspect_foreign_keys(sync_connection) -> dict[str, set[tuple[tuple[str, ...], str, tuple[str, ...], str | None]]]:
    inspector = inspect(sync_connection)
    return {table_name: {migration._normalize_foreign_key_signature(foreign_key) for foreign_key in inspector.get_foreign_keys(table_name)} for table_name in ("session_event", "terminal_session", "terminal_control_command", "long_term_memory_store")}


def test_core_foreign_key_migration_mysql_constraints_compile() -> None:
    compiled_constraint_count = 0
    for table_name in migration._TARGET_TABLE_ORDER:
        target_table = migration._target_table(table_name)
        assert target_table is not None
        constraints = (
            *migration._target_new_unique_constraints(target_table),
            *migration._target_foreign_key_constraints(target_table),
        )
        for constraint in constraints:
            compiled = str(AddConstraint(constraint, isolate_from_table=False).compile(dialect=mysql.dialect()))
            assert "ALTER TABLE" in compiled.upper()
            compiled_constraint_count += 1

    assert compiled_constraint_count > 0


@pytest.mark.parametrize("ondelete", ["cascade", "CaScAdE", "CASCADE"])
def test_normalize_foreign_key_signature_normalizes_ondelete_case(ondelete: str) -> None:
    assert migration._normalize_foreign_key_signature(
        {
            "constrained_columns": ["session_id", "uid"],
            "referred_table": "chat_session",
            "referred_columns": ["session_id", "uid"],
            "options": {"ondelete": ondelete},
        }
    ) == (("session_id", "uid"), "chat_session", ("session_id", "uid"), "CASCADE")


def test_core_foreign_key_migration_id() -> None:
    assert migration.MIGRATION_ID == "20260816_add_core_foreign_keys_v1"
