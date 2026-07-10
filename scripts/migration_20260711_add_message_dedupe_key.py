from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260711_add_message_dedupe_key"
MESSAGE_TABLE = "message"
DEDUPE_KEY_COLUMN = "dedupe_key"
DEDUPE_KEY_INDEX = "uq_message_dedupe_key"


def _get_column_names(sync_connection) -> set[str]:
    return {column["name"] for column in inspect(sync_connection).get_columns(MESSAGE_TABLE)}


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    column_names = await connection.run_sync(_get_column_names)
    if DEDUPE_KEY_COLUMN not in column_names:
        await session.execute(text(f"ALTER TABLE {MESSAGE_TABLE} ADD COLUMN {DEDUPE_KEY_COLUMN} VARCHAR(64)"))

    await session.execute(
        text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {DEDUPE_KEY_INDEX}
            ON {MESSAGE_TABLE} ({DEDUPE_KEY_COLUMN})
            """
        )
    )
