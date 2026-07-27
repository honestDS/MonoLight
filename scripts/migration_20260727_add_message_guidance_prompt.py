from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260727_add_message_guidance_prompt"


async def _table_columns(session: AsyncSession, table_name: str) -> set[str]:
    connection = await session.connection()

    def inspect_table(sync_connection) -> set[str]:
        inspector = inspect(sync_connection)
        if table_name not in inspector.get_table_names():
            return set()
        return {str(item["name"]) for item in inspector.get_columns(table_name)}

    return await connection.run_sync(inspect_table)


async def migrate(session: AsyncSession) -> None:
    columns = await _table_columns(session, "message")
    if not columns or "guidance_prompt" in columns:
        return
    await session.execute(text("ALTER TABLE message ADD COLUMN guidance_prompt TEXT"))
