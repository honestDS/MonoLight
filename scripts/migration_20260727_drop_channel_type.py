from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260727_drop_channel_type"


async def _table_columns(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str]:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return set()
        return {str(item["name"]) for item in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


async def migrate(session: AsyncSession) -> None:
    columns = await _table_columns(session, "channel")
    if "channel_type" not in columns:
        return
    await session.execute(text("ALTER TABLE channel DROP COLUMN channel_type"))
