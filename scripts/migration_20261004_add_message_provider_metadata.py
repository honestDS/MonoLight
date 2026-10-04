from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261004_add_message_provider_metadata"

MESSAGE_TABLE = "message"
COLUMN_NAME = "provider_metadata"


def _get_column_names(sync_connection) -> set[str]:
    return {column["name"] for column in inspect(sync_connection).get_columns(MESSAGE_TABLE)}


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    column_names = await connection.run_sync(_get_column_names)
    if COLUMN_NAME in column_names:
        return
    await session.execute(text(f"ALTER TABLE {MESSAGE_TABLE} ADD COLUMN {COLUMN_NAME} JSON"))
