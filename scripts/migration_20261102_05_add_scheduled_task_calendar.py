from __future__ import annotations

from typing import Any

from sqlalchemy import Column, MetaData, String, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t

MIGRATION_ID = "20261102_05_add_scheduled_task_calendar"

_TABLE_NAME = "scheduled_task"
_TEMP_TABLE_NAME = "scheduled_task__calendar_new"
_INTERVAL_COLUMN = "interval_seconds"
_CRON_COLUMN = "cron_expression"
_TIMEZONE_COLUMN = "timezone"


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _table_columns(connection: Connection) -> dict[str, dict[str, Any]] | None:
    inspector = inspect(connection)
    if not inspector.has_table(_TABLE_NAME):
        return None
    return {str(column["name"]): column for column in inspector.get_columns(_TABLE_NAME)}


def _row_count(connection: Connection, table_name: str) -> int:
    return int(connection.execute(text(f"SELECT COUNT(*) FROM {_quote(connection, table_name)}")).scalar_one() or 0)


def _sqlite_schema_sql(connection: Connection, object_type: str) -> list[tuple[str, str]]:
    rows = connection.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type = :object_type AND tbl_name = :table_name AND sql IS NOT NULL ORDER BY name"),
        {"object_type": object_type, "table_name": _TABLE_NAME},
    ).all()
    return [(str(row[0]), str(row[1])) for row in rows if row[0] and row[1]]


def _needs_sqlite_rebuild(connection: Connection) -> bool:
    columns = _table_columns(connection)
    if columns is None:
        return False
    if _INTERVAL_COLUMN not in columns:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} is missing {_INTERVAL_COLUMN}")
    return columns[_INTERVAL_COLUMN].get("nullable") is not True or _CRON_COLUMN not in columns or _TIMEZONE_COLUMN not in columns


def _rebuild_sqlite_table(connection: Connection) -> None:
    source_metadata = MetaData()
    source = Table(_TABLE_NAME, source_metadata, autoload_with=connection)
    if _INTERVAL_COLUMN not in source.c:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} is missing {_INTERVAL_COLUMN}")

    target_metadata = MetaData()
    for parent in tuple(source_metadata.tables.values()):
        if parent is not source:
            parent.to_metadata(target_metadata)
    replacement = source.to_metadata(target_metadata, name=_TEMP_TABLE_NAME)
    for index in tuple(replacement.indexes):
        replacement.indexes.remove(index)

    replacement.c[_INTERVAL_COLUMN].nullable = True
    if _CRON_COLUMN not in replacement.c:
        replacement.append_column(Column(_CRON_COLUMN, String(255), nullable=True))
    if _TIMEZONE_COLUMN not in replacement.c:
        replacement.append_column(Column(_TIMEZONE_COLUMN, String(100), nullable=True))

    index_sql = _sqlite_schema_sql(connection, "index")
    trigger_sql = _sqlite_schema_sql(connection, "trigger")
    source_columns = tuple(column.name for column in source.columns)
    column_sql = ", ".join(_quote(connection, column) for column in source_columns)
    quoted_table = _quote(connection, _TABLE_NAME)
    quoted_temporary = _quote(connection, _TEMP_TABLE_NAME)
    source_count = _row_count(connection, _TABLE_NAME)

    connection.execute(text(f"DROP TABLE IF EXISTS {quoted_temporary}"))
    replacement.create(bind=connection, checkfirst=False)
    connection.execute(text(f"INSERT INTO {quoted_temporary} ({column_sql}) SELECT {column_sql} FROM {quoted_table}"))
    copied_count = _row_count(connection, _TEMP_TABLE_NAME)
    if copied_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} row count changed while copying: source={source_count}, copied={copied_count}")

    connection.execute(text(f"DROP TABLE {quoted_table}"))
    connection.execute(text(f"ALTER TABLE {quoted_temporary} RENAME TO {quoted_table}"))
    for _, definition in index_sql:
        connection.exec_driver_sql(definition)
    for _, definition in trigger_sql:
        connection.exec_driver_sql(definition)

    final_columns = _table_columns(connection)
    if final_columns is None or any(column not in final_columns for column in source_columns):
        raise RuntimeError(f"{MIGRATION_ID}: original {_TABLE_NAME} columns were not preserved")
    if final_columns[_INTERVAL_COLUMN].get("nullable") is not True:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite interval column is still non-nullable")
    if _CRON_COLUMN not in final_columns or _TIMEZONE_COLUMN not in final_columns:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite calendar columns were not created")
    final_count = _row_count(connection, _TABLE_NAME)
    if final_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} row count changed after rebuild: source={source_count}, final={final_count}")


def _mysql_type(column: dict[str, Any], connection: Connection, column_name: str) -> str:
    column_type = column.get("type")
    if column_type is None:
        raise RuntimeError(f"{MIGRATION_ID}: unable to reflect {_TABLE_NAME}.{column_name} type")
    return str(column_type.compile(dialect=connection.dialect))


def _migrate_mysql(connection: Connection) -> None:
    columns = _table_columns(connection)
    if columns is None:
        return
    if _INTERVAL_COLUMN not in columns:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} is missing {_INTERVAL_COLUMN}")

    clauses: list[str] = []
    if _CRON_COLUMN not in columns:
        clauses.append(f"ADD COLUMN {_quote(connection, _CRON_COLUMN)} VARCHAR(255) NULL")
    if _TIMEZONE_COLUMN not in columns:
        clauses.append(f"ADD COLUMN {_quote(connection, _TIMEZONE_COLUMN)} VARCHAR(100) NULL")
    if columns[_INTERVAL_COLUMN].get("nullable") is not True:
        interval_type = _mysql_type(columns[_INTERVAL_COLUMN], connection, _INTERVAL_COLUMN)
        clauses.append(f"MODIFY COLUMN {_quote(connection, _INTERVAL_COLUMN)} {interval_type} NULL")
    if clauses:
        connection.execute(text(f"ALTER TABLE {_quote(connection, _TABLE_NAME)} {', '.join(clauses)}"))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    if database_type == "sqlite":
        if not await connection.run_sync(_needs_sqlite_rebuild):
            return
        await session.execute(text(f"UPDATE {_TABLE_NAME} SET id = id WHERE 1 = 0"))
        await connection.run_sync(_rebuild_sqlite_table)
        return
    if database_type == "mysql":
        await connection.run_sync(_migrate_mysql)
        return
    raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))
