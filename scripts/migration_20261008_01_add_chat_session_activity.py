from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261008_01_add_chat_session_activity"

CHAT_SESSION_TABLE = "chat_session"
MESSAGE_TABLE = "message"

ACTIVITY_COLUMNS = {
    "latest_message_id": "INTEGER NULL",
    "last_message_at": "DATETIME NULL",
    "last_read_message_id": "INTEGER NULL",
    "last_read_at": "DATETIME NULL",
}


def _table_columns(sync_connection) -> dict[str, set[str]]:
    inspector = inspect(sync_connection)
    table_names = set(inspector.get_table_names())
    return {table_name: {str(column["name"]) for column in inspector.get_columns(table_name)} for table_name in (CHAT_SESSION_TABLE, MESSAGE_TABLE) if table_name in table_names}


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    tables = await connection.run_sync(_table_columns)
    chat_session_columns = tables.get(CHAT_SESSION_TABLE)
    if chat_session_columns is None:
        return

    for column_name, column_definition in ACTIVITY_COLUMNS.items():
        if column_name not in chat_session_columns:
            await session.execute(text(f"ALTER TABLE {CHAT_SESSION_TABLE} ADD COLUMN {column_name} {column_definition}"))

    message_columns = tables.get(MESSAGE_TABLE)
    if message_columns is None or not {
        "id",
        "session_id",
        "uid",
        "created_at",
    }.issubset(message_columns):
        return

    await session.execute(
        text(
            f"UPDATE {CHAT_SESSION_TABLE} "
            "SET latest_message_id = COALESCE("
            "latest_message_id, (SELECT MAX(m.id) FROM message m "
            "WHERE m.session_id = chat_session.session_id AND m.uid = chat_session.uid)), "
            "last_message_at = COALESCE("
            "last_message_at, (SELECT MAX(m.created_at) FROM message m "
            "WHERE m.session_id = chat_session.session_id AND m.uid = chat_session.uid)) "
            "WHERE latest_message_id IS NULL OR last_message_at IS NULL"
        )
    )
