from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260703_add_message_platform"


async def table_exists(session: AsyncSession, table_name: str) -> bool:
    connection = await session.connection()
    return await connection.run_sync(lambda sync_connection: inspect(sync_connection).has_table(table_name))


async def create_index(session: AsyncSession, table_name: str, index_name: str, column_name: str, *, unique: bool = False) -> None:
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
            unique_sql = "UNIQUE " if unique else ""
            await session.execute(text(f"CREATE {unique_sql}INDEX {index_name} ON {table_name} ({column_name})"))
        return

    unique_sql = "UNIQUE " if unique else ""
    await session.execute(text(f"CREATE {unique_sql}INDEX IF NOT EXISTS {index_name} ON {table_name} ({column_name})"))


async def migrate(session: AsyncSession) -> None:
    if not await table_exists(session, "message_platform"):
        connection = await session.connection()
        if connection.dialect.name == "mysql":
            create_table_sql = """
                CREATE TABLE message_platform (
                    id INTEGER PRIMARY KEY AUTO_INCREMENT,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    platform_type VARCHAR(50) NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT 0,
                    status VARCHAR(50) NOT NULL DEFAULT 'DISCONNECTED',
                    account_id VARCHAR(255) NULL,
                    uid VARCHAR(100) NULL,
                    config JSON NULL,
                    state JSON NULL,
                    last_error VARCHAR(1000) NULL,
                    created_at TIMESTAMP NULL,
                    updated_at TIMESTAMP NULL
                )
                """
        else:
            create_table_sql = """
                CREATE TABLE message_platform (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(100) NOT NULL UNIQUE,
                    platform_type VARCHAR(50) NOT NULL,
                    is_enabled BOOLEAN NOT NULL DEFAULT 0,
                    status VARCHAR(50) NOT NULL DEFAULT 'DISCONNECTED',
                    account_id VARCHAR(255) NULL,
                    uid VARCHAR(100) NULL,
                    config JSON NOT NULL DEFAULT '{}',
                    state JSON NOT NULL DEFAULT '{}',
                    last_error VARCHAR(1000) NULL,
                    created_at TIMESTAMP NULL,
                    updated_at TIMESTAMP NULL
                )
                """
        await session.execute(text(create_table_sql))

    await create_index(session, "message_platform", "ix_message_platform_id", "id")
    await create_index(session, "message_platform", "ix_message_platform_name", "name", unique=True)
    await create_index(session, "message_platform", "ix_message_platform_platform_type", "platform_type")
    await create_index(session, "message_platform", "ix_message_platform_is_enabled", "is_enabled")
    await create_index(session, "message_platform", "ix_message_platform_status", "status")
    await create_index(session, "message_platform", "ix_message_platform_uid", "uid")
