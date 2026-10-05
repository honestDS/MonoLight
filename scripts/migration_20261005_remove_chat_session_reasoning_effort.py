from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261005_remove_chat_session_reasoning_effort"


async def _table_columns(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str]:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return set()
        return {str(item["name"]) for item in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


async def migrate(session: AsyncSession) -> None:
    columns = await _table_columns(session, "chat_session")
    if "reasoning_effort" not in columns:
        return
    await session.execute(text("ALTER TABLE chat_session DROP COLUMN reasoning_effort"))
