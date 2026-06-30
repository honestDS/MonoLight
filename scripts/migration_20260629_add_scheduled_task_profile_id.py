from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260629_add_scheduled_task_profile_id"


async def ensure_column(session: AsyncSession, table_name: str, column_name: str) -> None:
    connection = await session.connection()
    column_names = set(
        await connection.run_sync(
            lambda sync_connection: [column["name"] for column in inspect(sync_connection).get_columns(table_name)]
        )
    )
    if column_name not in column_names:
        await session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} INTEGER NULL"))


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
    await ensure_column(session, "chat_session", "profile_id")
    await ensure_column(session, "scheduled_task", "profile_id")
    await ensure_index(session, "chat_session", "ix_chat_session_profile_id", "profile_id")
    await ensure_index(session, "scheduled_task", "ix_scheduled_task_profile_id", "profile_id")

    await session.execute(
        text(
            """
            UPDATE chat_session
            SET profile_id = (
                SELECT message.profile_id
                FROM message
                WHERE message.session_id = chat_session.session_id
                  AND message.uid = chat_session.uid
                  AND message.type != 'scheduled_task_trigger'
                ORDER BY message.created_at DESC
                LIMIT 1
            )
            WHERE profile_id IS NULL
            """
        )
    )

    await session.execute(
        text(
            """
            UPDATE scheduled_task
            SET profile_id = (
                SELECT chat_session.profile_id
                FROM chat_session
                WHERE chat_session.session_id = scheduled_task.session_id
                  AND chat_session.uid = scheduled_task.uid
                LIMIT 1
            )
            WHERE profile_id IS NULL
            """
        )
    )
