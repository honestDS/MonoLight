from sqlalchemy import Column, DateTime, Index, Integer, MetaData, String, Table, inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261017_01_add_chat_session_message_platform_binding"

CHAT_SESSION_TABLE = "chat_session"

PLATFORM_COLUMNS = {
    "message_platform_id": "INTEGER NULL",
    "message_platform_user_id": "VARCHAR(255) NULL",
}


def _chat_session_columns(sync_connection) -> set[str] | None:
    inspector = inspect(sync_connection)
    if CHAT_SESSION_TABLE not in set(inspector.get_table_names()):
        return None
    return {str(column["name"]) for column in inspector.get_columns(CHAT_SESSION_TABLE)}


def _create_platform_index(sync_connection) -> None:
    metadata = MetaData()
    chat_session = Table(
        CHAT_SESSION_TABLE,
        metadata,
        Column("uid", String(100), nullable=False),
        Column("message_platform_id", Integer, nullable=True),
        Column("message_platform_user_id", String(255), nullable=True),
        Column("created_at", DateTime(timezone=True), nullable=False),
    )
    Index(
        "ix_chat_session_platform_user",
        chat_session.c.uid,
        chat_session.c.message_platform_id,
        chat_session.c.message_platform_user_id,
        chat_session.c.created_at,
        unique=False,
    ).create(sync_connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    chat_session_columns = await connection.run_sync(_chat_session_columns)
    if chat_session_columns is None:
        return

    for column_name, column_definition in PLATFORM_COLUMNS.items():
        if column_name not in chat_session_columns:
            await session.execute(text(f"ALTER TABLE {CHAT_SESSION_TABLE} ADD COLUMN {column_name} {column_definition}"))

    await connection.run_sync(_create_platform_index)
