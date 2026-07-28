import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from scripts import migration_20260728_add_profile_selection_priority as migration


@pytest.mark.asyncio
async def test_profile_selection_migration_upgrades_legacy_sqlite_schema(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'legacy-profile-selection.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE profile (id INTEGER PRIMARY KEY, is_active BOOLEAN NOT NULL)"))
            await connection.execute(text("CREATE TABLE chat_session (session_id TEXT PRIMARY KEY)"))
            await connection.execute(text("CREATE TABLE message_platform (id INTEGER PRIMARY KEY)"))
            await connection.execute(text("INSERT INTO profile (id, is_active) VALUES (1, 1), (2, 0)"))

        async with session_factory() as session:
            await migration.migrate(session)
            await session.commit()
            await migration.migrate(session)
            await session.commit()

        async with engine.connect() as connection:
            profile_columns, session_columns, platform_columns, session_indexes, platform_indexes = await connection.run_sync(
                lambda sync_connection: (
                    {column["name"] for column in inspect(sync_connection).get_columns("profile")},
                    {column["name"] for column in inspect(sync_connection).get_columns("chat_session")},
                    {column["name"] for column in inspect(sync_connection).get_columns("message_platform")},
                    {index["name"] for index in inspect(sync_connection).get_indexes("chat_session")},
                    {index["name"] for index in inspect(sync_connection).get_indexes("message_platform")},
                )
            )
            default_values = (await connection.execute(text("SELECT is_default FROM profile ORDER BY id"))).scalars().all()

        assert profile_columns == {"id", "is_default"}
        assert default_values == [1, 0]
        assert "profile_override_id" in session_columns
        assert "profile_id" in platform_columns
        assert "ix_chat_session_profile_override_id" in session_indexes
        assert "ix_message_platform_profile_id" in platform_indexes
    finally:
        await engine.dispose()


@pytest.mark.asyncio
async def test_profile_selection_migration_is_idempotent_for_current_sqlite_schema(tmp_path):
    engine = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'current-profile-selection.db'}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        async with engine.begin() as connection:
            await connection.execute(text("CREATE TABLE profile (id INTEGER PRIMARY KEY, is_default BOOLEAN NOT NULL DEFAULT FALSE)"))
            await connection.execute(text("CREATE TABLE chat_session (session_id TEXT PRIMARY KEY, profile_override_id INTEGER)"))
            await connection.execute(text("CREATE TABLE message_platform (id INTEGER PRIMARY KEY, profile_id INTEGER)"))
            await connection.execute(text("CREATE INDEX ix_chat_session_profile_override_id ON chat_session (profile_override_id)"))
            await connection.execute(text("CREATE INDEX ix_message_platform_profile_id ON message_platform (profile_id)"))

        async with session_factory() as session:
            await migration.migrate(session)
            await session.commit()
            await migration.migrate(session)
            await session.commit()

        async with engine.connect() as connection:
            profile_columns, session_columns, platform_columns, session_indexes, platform_indexes = await connection.run_sync(
                lambda sync_connection: (
                    {column["name"] for column in inspect(sync_connection).get_columns("profile")},
                    {column["name"] for column in inspect(sync_connection).get_columns("chat_session")},
                    {column["name"] for column in inspect(sync_connection).get_columns("message_platform")},
                    [index["name"] for index in inspect(sync_connection).get_indexes("chat_session")],
                    [index["name"] for index in inspect(sync_connection).get_indexes("message_platform")],
                )
            )

        assert profile_columns == {"id", "is_default"}
        assert session_columns == {"session_id", "profile_override_id"}
        assert platform_columns == {"id", "profile_id"}
        assert session_indexes.count("ix_chat_session_profile_override_id") == 1
        assert platform_indexes.count("ix_message_platform_profile_id") == 1
    finally:
        await engine.dispose()
