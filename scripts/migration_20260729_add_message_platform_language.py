from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260729_add_message_platform_language"


async def _table_columns(session: AsyncSession, table_name: str) -> set[str] | None:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str] | None:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return None
        return {str(column["name"]) for column in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


async def migrate(session: AsyncSession) -> None:
    columns = await _table_columns(session, "message_platform")
    if columns is not None and "language" not in columns:
        await session.execute(text("ALTER TABLE message_platform ADD COLUMN language VARCHAR(20) NOT NULL DEFAULT 'zh'"))
