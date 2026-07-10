from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260711_add_session_event_dedupe_key"
SESSION_EVENT_TABLE = "session_event"
DEDUPE_KEY_COLUMN = "dedupe_key"
DEDUPE_KEY_INDEX = "uq_session_event_dedupe_key"


def _get_column_names(sync_connection) -> set[str]:
    return {column["name"] for column in inspect(sync_connection).get_columns(SESSION_EVENT_TABLE)}


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    column_names = await connection.run_sync(_get_column_names)
    if DEDUPE_KEY_COLUMN not in column_names:
        await session.execute(text(f"ALTER TABLE {SESSION_EVENT_TABLE} ADD COLUMN {DEDUPE_KEY_COLUMN} VARCHAR(64)"))
        await session.execute(
            text(
                f"""
                UPDATE {SESSION_EVENT_TABLE}
                SET {DEDUPE_KEY_COLUMN} = printf('legacy-session-event-%044d', id)
                WHERE {DEDUPE_KEY_COLUMN} IS NULL
                """
            )
        )

    await session.execute(
        text(
            f"""
            CREATE UNIQUE INDEX IF NOT EXISTS {DEDUPE_KEY_INDEX}
            ON {SESSION_EVENT_TABLE} ({DEDUPE_KEY_COLUMN})
            """
        )
    )
