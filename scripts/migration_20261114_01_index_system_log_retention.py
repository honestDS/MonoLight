from __future__ import annotations

import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261114_01_index_system_log_retention"

_TABLE_NAME = "system_log"
_TEMP_TABLE_NAME = "system_log__autoincrement_new"
_INDEX_NAME = "ix_system_log_created_at_id"

_AUTOINCREMENT_PATTERN = re.compile(r"\bPRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE)
_ID_COLUMN_PATTERN = re.compile(
    r"(?P<prefix>(?:\(|,)\s*(?:\"id\"|`id`|\[id\]|id)\s+INTEGER(?:\s+NOT\s+NULL)?)\s*,",
    re.IGNORECASE | re.DOTALL,
)
_INLINE_ID_PRIMARY_KEY_PATTERN = re.compile(
    r"(?P<prefix>(?:\(|,)\s*(?:\"id\"|`id`|\[id\]|id)\s+INTEGER(?:\s+NOT\s+NULL)?)\s+PRIMARY\s+KEY(?=\s*(?:,|\)))",
    re.IGNORECASE | re.DOTALL,
)
_TABLE_PRIMARY_KEY_PATTERN = re.compile(
    r",\s*(?:CONSTRAINT\s+(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|\S+)\s+)?PRIMARY\s+KEY\s*\(\s*(?:\"id\"|`id`|\[id\]|id)\s*\)(?=\s*(?:,|\)))",
    re.IGNORECASE | re.DOTALL,
)


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _sqlite_table_sql(connection: Connection) -> str | None:
    row = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
        {"table_name": _TABLE_NAME},
    ).first()
    if row is None:
        return None
    value = row[0]
    if not isinstance(value, str) or not value.strip():
        raise RuntimeError(f"{MIGRATION_ID}: system_log CREATE TABLE statement is unavailable")
    return value


def _sqlite_schema_objects(connection: Connection) -> tuple[str, ...]:
    rows = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE tbl_name = :table_name AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name"),
        {"table_name": _TABLE_NAME},
    ).all()
    return tuple(str(row[0]) for row in rows if isinstance(row[0], str) and row[0].strip())


def _column_names(connection: Connection) -> tuple[str, ...]:
    return tuple(str(column["name"]) for column in inspect(connection).get_columns(_TABLE_NAME) if column.get("computed") is None)


def _build_autoincrement_table_sql(source_sql: str, connection: Connection) -> str:
    _, opening_parenthesis, table_definition = source_sql.partition("(")
    if not opening_parenthesis:
        raise RuntimeError(f"{MIGRATION_ID}: unable to identify system_log CREATE TABLE statement")
    rebuilt_sql = f"CREATE TABLE {_quote(connection, _TEMP_TABLE_NAME)} ({table_definition}"

    rebuilt_sql, id_count = _ID_COLUMN_PATTERN.subn(
        lambda match: f"{match.group('prefix')} PRIMARY KEY AUTOINCREMENT,",
        rebuilt_sql,
    )
    if id_count == 1:
        rebuilt_sql, primary_key_count = _TABLE_PRIMARY_KEY_PATTERN.subn("", rebuilt_sql)
        if primary_key_count != 1:
            raise RuntimeError(f"{MIGRATION_ID}: system_log id must use a table-level PRIMARY KEY (id)")
        return rebuilt_sql
    if id_count != 0:
        raise RuntimeError(f"{MIGRATION_ID}: system_log id column definition is ambiguous")

    rebuilt_sql, inline_primary_key_count = _INLINE_ID_PRIMARY_KEY_PATTERN.subn(
        lambda match: f"{match.group('prefix')} PRIMARY KEY AUTOINCREMENT",
        rebuilt_sql,
        count=1,
    )
    if inline_primary_key_count != 1 or _TABLE_PRIMARY_KEY_PATTERN.search(rebuilt_sql):
        raise RuntimeError(f"{MIGRATION_ID}: system_log id must be an INTEGER PRIMARY KEY")
    return rebuilt_sql


def _ensure_index(connection: Connection, *, check_table: bool = True) -> None:
    inspector = inspect(connection)
    if check_table and not inspector.has_table(_TABLE_NAME):
        return

    index_names = {str(index["name"]) for index in inspector.get_indexes(_TABLE_NAME) if index.get("name")}
    if _INDEX_NAME in index_names:
        return

    table_name = _quote(connection, _TABLE_NAME)
    index_name = _quote(connection, _INDEX_NAME)
    created_at = _quote(connection, "created_at")
    identifier = _quote(connection, "id")
    connection.execute(text(f"CREATE INDEX {index_name} ON {table_name} ({created_at}, {identifier})"))


def _rebuild_sqlite_system_log(connection: Connection) -> None:
    source_sql = _sqlite_table_sql(connection)
    if source_sql is None:
        return
    if _AUTOINCREMENT_PATTERN.search(source_sql):
        _ensure_index(connection, check_table=False)
        return

    columns = _column_names(connection)
    if "id" not in columns or "created_at" not in columns:
        raise RuntimeError(f"{MIGRATION_ID}: system_log is missing created_at or id")

    schema_objects = _sqlite_schema_objects(connection)
    rebuilt_sql = _build_autoincrement_table_sql(source_sql, connection)

    connection.exec_driver_sql(rebuilt_sql)

    columns_sql = ", ".join(_quote(connection, column) for column in columns)
    temporary_name = _quote(connection, _TEMP_TABLE_NAME)
    table_name = _quote(connection, _TABLE_NAME)
    connection.execute(text(f"INSERT INTO {temporary_name} ({columns_sql}) SELECT {columns_sql} FROM {table_name}"))

    connection.exec_driver_sql(f"DROP TABLE {table_name}")
    connection.exec_driver_sql(f"ALTER TABLE {temporary_name} RENAME TO {table_name}")
    for ddl in schema_objects:
        connection.exec_driver_sql(ddl)

    _ensure_index(connection, check_table=False)


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)
    connection = await session.connection()
    database_type = connection.dialect.name

    if database_type == "sqlite":
        async with session.begin_nested():
            await connection.run_sync(_rebuild_sqlite_system_log)
        return
    if database_type == "mysql":
        await connection.run_sync(_ensure_index)
        return
    raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))
