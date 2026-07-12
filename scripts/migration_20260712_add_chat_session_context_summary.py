from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260712_add_chat_session_context_summary"


async def _column_names(session: AsyncSession) -> set[str]:
    result = await session.execute(text("PRAGMA table_info(chat_session)"))
    return {str(row[1]) for row in result.fetchall()}


async def migrate(session: AsyncSession) -> None:
    columns = await _column_names(session)
    if "context_summary" not in columns:
        await session.execute(text("ALTER TABLE chat_session ADD COLUMN context_summary TEXT"))
    if "context_summary_message_id" not in columns:
        await session.execute(text("ALTER TABLE chat_session ADD COLUMN context_summary_message_id INTEGER"))

    await session.execute(text("CREATE INDEX IF NOT EXISTS ix_chat_session_context_summary_message_id ON chat_session (context_summary_message_id)"))
