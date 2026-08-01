from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260801_terminal_process_identity"


async def _table_exists(session: AsyncSession) -> bool:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: inspect(sync_connection).has_table("terminal_session"))


async def _column_exists(session: AsyncSession) -> bool:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: any(column["name"] == "process_identity" for column in inspect(sync_connection).get_columns("terminal_session")))


async def migrate(session: AsyncSession) -> None:
    if not await _table_exists(session) or await _column_exists(session):
        return

    dialect_name = session.get_bind().dialect.name
    column_type = "JSONB" if dialect_name == "postgresql" else "JSON"
    await session.execute(text(f"ALTER TABLE terminal_session ADD COLUMN process_identity {column_type} NULL"))
