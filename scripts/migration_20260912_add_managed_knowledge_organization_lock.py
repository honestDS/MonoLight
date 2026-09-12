from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260912_add_managed_knowledge_organization_lock_v1"


def _ensure_organization_lock_column(connection) -> None:
    inspector = inspect(connection)
    column_names = {column["name"] for column in inspector.get_columns("managed_knowledge_item")}
    if "organization_lock_token" not in column_names:
        connection.execute(text("ALTER TABLE managed_knowledge_item ADD COLUMN organization_lock_token VARCHAR(64) NULL"))

    inspector = inspect(connection)
    index_names = {index["name"] for index in inspector.get_indexes("managed_knowledge_item")}
    if "ix_managed_knowledge_item_organization_lock_token" not in index_names:
        connection.execute(text("CREATE INDEX ix_managed_knowledge_item_organization_lock_token ON managed_knowledge_item (organization_lock_token)"))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_organization_lock_column)
