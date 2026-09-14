from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260913_add_knowledge_organization_state_v1"

_TABLE_NAME = "knowledge_base"
_COLUMN_DEFINITIONS = (
    ("organization_last_job_id", "INTEGER NULL"),
    ("organization_last_run_at", "DATETIME NULL"),
    ("organization_error", "TEXT NULL"),
)
_INDEX_NAME = "ix_knowledge_base_organization_last_job_id"


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _ensure_organization_state(connection: Connection) -> None:
    inspector = inspect(connection)
    if _TABLE_NAME not in inspector.get_table_names():
        return

    table_name = _quote(connection, _TABLE_NAME)
    column_names = {str(column["name"]) for column in inspector.get_columns(_TABLE_NAME)}
    for column_name, definition in _COLUMN_DEFINITIONS:
        if column_name not in column_names:
            connection.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {_quote(connection, column_name)} {definition}"))
            column_names.add(column_name)

    index_names = {str(index["name"]) for index in inspect(connection).get_indexes(_TABLE_NAME)}
    if _INDEX_NAME not in index_names:
        column_name = _quote(connection, "organization_last_job_id")
        connection.execute(text(f"CREATE INDEX {_quote(connection, _INDEX_NAME)} ON {table_name} ({column_name})"))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_ensure_organization_state)
