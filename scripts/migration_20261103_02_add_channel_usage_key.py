from uuid import uuid4

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261103_02_add_channel_usage_key"

_TABLE_NAME = "channel"
_COLUMN_NAME = "usage_key"
_INDEX_NAME = "uq_channel_usage_key"


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _table_columns(connection: Connection) -> set[str] | None:
    inspector = inspect(connection)
    if not inspector.has_table(_TABLE_NAME):
        return None
    return {str(column["name"]) for column in inspector.get_columns(_TABLE_NAME)}


def _has_unique_usage_key(connection: Connection) -> bool:
    inspector = inspect(connection)
    expected_columns = (_COLUMN_NAME,)

    for index in inspector.get_indexes(_TABLE_NAME):
        columns = tuple(str(column) for column in index.get("column_names") or ())
        if index.get("unique") and columns == expected_columns:
            return True

    for constraint in inspector.get_unique_constraints(_TABLE_NAME):
        columns = tuple(str(column) for column in constraint.get("column_names") or ())
        if columns == expected_columns:
            return True

    return False


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)
    connection = await session.connection()
    columns = await connection.run_sync(_table_columns)
    if columns is None:
        return

    table_name = _quote(connection.sync_connection, _TABLE_NAME)
    column_name = _quote(connection.sync_connection, _COLUMN_NAME)
    id_name = _quote(connection.sync_connection, "id")
    index_name = _quote(connection.sync_connection, _INDEX_NAME)

    if _COLUMN_NAME not in columns:
        await session.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} VARCHAR(32) NOT NULL DEFAULT ''"))

    rows = (await session.execute(text(f"SELECT {id_name}, {column_name} FROM {table_name} ORDER BY {id_name}"))).mappings().all()
    used_keys = {str(row[_COLUMN_NAME]) for row in rows if row[_COLUMN_NAME] not in (None, "")}
    for row in rows:
        if row[_COLUMN_NAME] not in (None, ""):
            continue

        usage_key = uuid4().hex
        while usage_key in used_keys:
            usage_key = uuid4().hex
        used_keys.add(usage_key)
        await session.execute(
            text(f"UPDATE {table_name} SET {column_name} = :usage_key WHERE {id_name} = :channel_id AND ({column_name} IS NULL OR {column_name} = '')"),
            {"usage_key": usage_key, "channel_id": row["id"]},
        )

    if not await connection.run_sync(_has_unique_usage_key):
        await session.execute(text(f"CREATE UNIQUE INDEX {index_name} ON {table_name} ({column_name})"))
