from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260726_add_chat_session_llm_request_metadata_order"


async def _existing_columns(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: {str(item["name"]) for item in inspect(sync_connection).get_columns(table_name)})


async def migrate(session: AsyncSession) -> None:
    session_columns = await _existing_columns(session, "chat_session")
    if "llm_request_metadata_work_sequence_no" not in session_columns:
        await session.execute(text("ALTER TABLE chat_session ADD COLUMN llm_request_metadata_work_sequence_no INTEGER"))
    if "llm_request_metadata_event_sequence_no" not in session_columns:
        await session.execute(text("ALTER TABLE chat_session ADD COLUMN llm_request_metadata_event_sequence_no INTEGER"))
