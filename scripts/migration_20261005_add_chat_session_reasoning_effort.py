from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261005_add_chat_session_reasoning_effort"


async def _table_columns(session: AsyncSession, table_name: str) -> set[str] | None:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str] | None:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return None
        return {str(column["name"]) for column in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


async def migrate(session: AsyncSession) -> None:
    columns = await _table_columns(session, "chat_session")
    if columns is not None and "reasoning_effort" not in columns:
        await session.execute(text("ALTER TABLE chat_session ADD COLUMN reasoning_effort VARCHAR(64) NULL"))
