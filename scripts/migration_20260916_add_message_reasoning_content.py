from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260916_add_message_reasoning_content"
MESSAGE_TABLE = "message"
REASONING_CONTENT_COLUMN = "reasoning_content"


def _get_column_names(sync_connection) -> set[str]:
    return {column["name"] for column in inspect(sync_connection).get_columns(MESSAGE_TABLE)}


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    column_names = await connection.run_sync(_get_column_names)
    if REASONING_CONTENT_COLUMN not in column_names:
        await session.execute(text(f"ALTER TABLE {MESSAGE_TABLE} ADD COLUMN {REASONING_CONTENT_COLUMN} TEXT"))
