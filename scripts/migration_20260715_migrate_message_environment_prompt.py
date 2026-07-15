from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260715_migrate_message_environment_prompt"
MESSAGE_TABLE = "message"
ENVIRONMENT_PROMPT_COLUMN = "environment_prompt"
LEGACY_SYSTEM_PROMPT_COLUMN = "system_prompt"


def _get_column_names(sync_connection) -> set[str]:
    return {column["name"] for column in inspect(sync_connection).get_columns(MESSAGE_TABLE)}


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    column_names = await connection.run_sync(_get_column_names)
    if ENVIRONMENT_PROMPT_COLUMN not in column_names:
        await session.execute(text(f"ALTER TABLE {MESSAGE_TABLE} ADD COLUMN {ENVIRONMENT_PROMPT_COLUMN} TEXT"))

    if LEGACY_SYSTEM_PROMPT_COLUMN in column_names:
        await session.execute(text(f"UPDATE {MESSAGE_TABLE} SET {ENVIRONMENT_PROMPT_COLUMN} = {LEGACY_SYSTEM_PROMPT_COLUMN} WHERE {ENVIRONMENT_PROMPT_COLUMN} IS NULL AND {LEGACY_SYSTEM_PROMPT_COLUMN} IS NOT NULL"))
