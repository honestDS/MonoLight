from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260709_add_chat_session_source"


async def ensure_column(session: AsyncSession, table_name: str, column_name: str) -> None:
    connection = await session.connection()
    column_names = set(
        await connection.run_sync(
            lambda sync_connection: [column["name"] for column in inspect(sync_connection).get_columns(table_name)]
        )
    )
    if column_name in column_names:
        return

    if connection.dialect.name == "mysql":
        await session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} VARCHAR(50) NOT NULL DEFAULT 'http'"))
        return

    await session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} VARCHAR(50) NOT NULL DEFAULT 'http'"))


async def ensure_index(session: AsyncSession, table_name: str, index_name: str, column_name: str) -> None:
    connection = await session.connection()
    if connection.dialect.name == "mysql":
        index_rows = await session.execute(
            text(
                """
                SELECT 1
                FROM information_schema.statistics
                WHERE table_schema = DATABASE()
                  AND table_name = :table_name
                  AND index_name = :index_name
                LIMIT 1
                """
            ),
            {"table_name": table_name, "index_name": index_name},
        )
        if index_rows.scalar() is None:
            await session.execute(text(f"CREATE INDEX {index_name} ON {table_name} ({column_name})"))
        return

    await session.execute(text(f"CREATE INDEX IF NOT EXISTS {index_name} ON {table_name} ({column_name})"))


async def migrate(session: AsyncSession) -> None:
    await ensure_column(session, "chat_session", "source")
    await ensure_index(session, "chat_session", "ix_chat_session_source", "source")

    await session.execute(
        text(
            """
            UPDATE chat_session
            SET source = 'weixin-openclaw'
            WHERE session_id LIKE 'weixin-openclaw:%'
              AND (source IS NULL OR source = '' OR source = 'http')
            """
        )
    )
