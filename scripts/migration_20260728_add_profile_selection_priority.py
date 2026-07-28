from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260728_add_profile_selection_priority"


async def _table_columns(session: AsyncSession, table_name: str) -> set[str] | None:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str] | None:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return None
        return {str(column["name"]) for column in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


async def _index_names(session: AsyncSession, table_name: str) -> set[str] | None:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str] | None:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return None
        return {str(index["name"]) for index in inspector.get_indexes(table_name)}

    return await connection.run_sync(inspect_table)


async def _ensure_column(session: AsyncSession, table_name: str, column_name: str, definition: str) -> None:
    columns = await _table_columns(session, table_name)
    if columns is not None and column_name not in columns:
        await session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition}"))


async def _ensure_index(session: AsyncSession, table_name: str, index_name: str, column_name: str) -> None:
    index_names = await _index_names(session, table_name)
    if index_names is not None and index_name not in index_names:
        await session.execute(text(f"CREATE INDEX {index_name} ON {table_name} ({column_name})"))


async def migrate(session: AsyncSession) -> None:
    profile_columns = await _table_columns(session, "profile")
    if profile_columns is not None:
        if "is_default" not in profile_columns:
            await session.execute(text("ALTER TABLE profile ADD COLUMN is_default BOOLEAN NOT NULL DEFAULT FALSE"))
        if "is_active" in profile_columns:
            await session.execute(text("UPDATE profile SET is_default = TRUE WHERE is_active = TRUE"))
            await session.execute(text("ALTER TABLE profile DROP COLUMN is_active"))

    await _ensure_column(session, "chat_session", "profile_override_id", "INTEGER NULL")
    await _ensure_column(session, "message_platform", "profile_id", "INTEGER NULL")
    await _ensure_index(session, "chat_session", "ix_chat_session_profile_override_id", "profile_override_id")
    await _ensure_index(session, "message_platform", "ix_message_platform_profile_id", "profile_id")
