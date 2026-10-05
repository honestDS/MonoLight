from __future__ import annotations

import re

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t

MIGRATION_ID = "20261006_01_enable_session_event_autoincrement"

_TABLE_NAME = "session_event"
_TEMP_TABLE_NAME = "session_event__autoincrement_new"
_AUTOINCREMENT_PATTERN = re.compile(r"\bPRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE)
_CREATE_TABLE_PATTERN = re.compile(
    r"^\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(?:\"session_event\"|`session_event`|\[session_event\]|session_event)\s*\(",
    re.IGNORECASE | re.DOTALL,
)
_ID_COLUMN_PATTERN = re.compile(
    r"(?P<prefix>(?:\(|,)\s*(?:\"id\"|`id`|\[id\]|id)\s+INTEGER(?:\s+NOT\s+NULL)?)\s*,",
    re.IGNORECASE | re.DOTALL,
)
_INLINE_ID_PRIMARY_KEY_PATTERN = re.compile(
    r"(?P<prefix>(?:\(|,)\s*(?:\"id\"|`id`|\[id\]|id)\s+INTEGER(?:\s+NOT\s+NULL)?)\s+PRIMARY\s+KEY\b",
    re.IGNORECASE | re.DOTALL,
)
_TABLE_PRIMARY_KEY_PATTERN = re.compile(
    r",\s*(?:CONSTRAINT\s+(?:\"[^\"]+\"|`[^`]+`|\[[^\]]+\]|\S+)\s+)?PRIMARY\s+KEY\s*\(\s*(?:\"id\"|`id`|\[id\]|id)\s*\)",
    re.IGNORECASE | re.DOTALL,
)


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _sqlite_table_sql(connection: Connection) -> str | None:
    return connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :name"),
        {"name": _TABLE_NAME},
    ).scalar_one_or_none()


def _sqlite_uses_autoincrement(connection: Connection) -> bool:
    sql = _sqlite_table_sql(connection)
    return isinstance(sql, str) and _AUTOINCREMENT_PATTERN.search(sql) is not None


def _schema_objects(connection: Connection) -> tuple[str, ...]:
    rows = connection.execute(
        text("SELECT type, name, sql FROM sqlite_master WHERE tbl_name = :table_name AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name"),
        {"table_name": _TABLE_NAME},
    ).all()
    return tuple(str(row.sql) for row in rows if isinstance(row.sql, str) and row.sql.strip())


def _column_names(connection: Connection) -> tuple[str, ...]:
    return tuple(str(column["name"]) for column in inspect(connection).get_columns(_TABLE_NAME) if column.get("computed") is None)


def _build_autoincrement_table_sql(source_sql: str, connection: Connection) -> str:
    quoted_temp = _quote(connection, _TEMP_TABLE_NAME)
    rebuilt_sql, create_count = _CREATE_TABLE_PATTERN.subn(f"CREATE TABLE {quoted_temp} (", source_sql, count=1)
    if create_count != 1:
        raise RuntimeError("unable to identify session_event CREATE TABLE statement")

    rebuilt_sql, id_count = _ID_COLUMN_PATTERN.subn(lambda match: f"{match.group('prefix')} PRIMARY KEY AUTOINCREMENT,", rebuilt_sql)
    if id_count == 1:
        rebuilt_sql, primary_key_count = _TABLE_PRIMARY_KEY_PATTERN.subn("", rebuilt_sql)
        if primary_key_count != 1:
            raise RuntimeError("unable to identify session_event table-level primary key constraint")
        return rebuilt_sql
    if id_count != 0:
        raise RuntimeError("session_event id column definition must be unique")

    rebuilt_sql, inline_primary_key_count = _INLINE_ID_PRIMARY_KEY_PATTERN.subn(lambda match: f"{match.group('prefix')} PRIMARY KEY AUTOINCREMENT", rebuilt_sql)
    if inline_primary_key_count != 1:
        raise RuntimeError("session_event id must be an INTEGER primary key")
    return rebuilt_sql


def _rebuild_sqlite_session_event(connection: Connection) -> None:
    if _TABLE_NAME not in inspect(connection).get_table_names():
        return
    if _sqlite_uses_autoincrement(connection):
        return

    source_sql = _sqlite_table_sql(connection)
    if not isinstance(source_sql, str) or not source_sql.strip():
        raise RuntimeError("session_event CREATE TABLE statement is unavailable")

    columns = _column_names(connection)
    schema_objects = _schema_objects(connection)
    rebuilt_sql = _build_autoincrement_table_sql(source_sql, connection)

    connection.exec_driver_sql(rebuilt_sql)

    columns_sql = ", ".join(_quote(connection, column) for column in columns)
    connection.execute(text(f"INSERT INTO {_quote(connection, _TEMP_TABLE_NAME)} ({columns_sql}) SELECT {columns_sql} FROM {_quote(connection, _TABLE_NAME)}"))
    connection.exec_driver_sql(f"DROP TABLE {_quote(connection, _TABLE_NAME)}")
    connection.exec_driver_sql(f"ALTER TABLE {_quote(connection, _TEMP_TABLE_NAME)} RENAME TO {_quote(connection, _TABLE_NAME)}")
    for ddl in schema_objects:
        connection.exec_driver_sql(ddl)

    violations = connection.execute(text(f"PRAGMA foreign_key_check({_quote(connection, _TABLE_NAME)})")).all()
    if violations:
        raise RuntimeError(f"session_event autoincrement migration foreign key violations: {len(violations)}")
    if not _sqlite_uses_autoincrement(connection):
        raise RuntimeError("session_event AUTOINCREMENT migration did not take effect")


async def migrate(session: AsyncSession) -> None:
    database_type = session.get_bind().dialect.name
    if database_type == "mysql":
        return
    if database_type != "sqlite":
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))

    async with session.begin_nested():
        connection = await session.connection()
        await connection.run_sync(_rebuild_sqlite_session_event)
