from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261017_02_add_chat_session_stream_llm_request"

CHAT_SESSION_TABLE = "chat_session"
STREAM_LLM_REQUEST_COLUMN = "stream_llm_request"


def _chat_session_columns(sync_connection) -> set[str] | None:
    inspector = inspect(sync_connection)
    if CHAT_SESSION_TABLE not in set(inspector.get_table_names()):
        return None
    return {str(column["name"]) for column in inspector.get_columns(CHAT_SESSION_TABLE)}


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    chat_session_columns = await connection.run_sync(_chat_session_columns)
    if chat_session_columns is None or STREAM_LLM_REQUEST_COLUMN in chat_session_columns:
        return

    await session.execute(text(f"ALTER TABLE {CHAT_SESSION_TABLE} ADD COLUMN {STREAM_LLM_REQUEST_COLUMN} BOOLEAN NULL"))
