from __future__ import annotations

import re
from typing import Any

from sqlalchemy import CheckConstraint, MetaData, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t

MIGRATION_ID = "20261101_02_bind_scheduled_tasks_to_sessions"

_TABLE_NAME = "scheduled_task"
_ID_COLUMN = "id"
_PROFILE_COLUMN = "profile_id"
_TEMP_TABLE_NAME = "scheduled_task__bind_sessions_new"
_PROFILE_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])profile_id(?![A-Za-z0-9_])", re.IGNORECASE)


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _table_exists(connection: Connection) -> bool:
    return inspect(connection).has_table(_TABLE_NAME)


def _sqlite_schema_sql(connection: Connection, object_type: str) -> list[tuple[str, str]]:
    rows = connection.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type = :object_type AND tbl_name = :table_name AND sql IS NOT NULL ORDER BY name"),
        {"object_type": object_type, "table_name": _TABLE_NAME},
    ).all()
    return [(str(row[0]), str(row[1])) for row in rows if row[0] and row[1]]


def _sqlite_index_uses_profile(connection: Connection, index_name: str, sql: str) -> bool:
    rows = connection.exec_driver_sql(f"PRAGMA index_info({_quote(connection, index_name)})").all()
    if any(row[2] == _PROFILE_COLUMN for row in rows):
        return True
    return bool(_PROFILE_IDENTIFIER.search(sql))


def _sqlite_indexes_without_profile(connection: Connection) -> list[str]:
    index_sql: list[str] = []
    for index_name, sql in _sqlite_schema_sql(connection, "index"):
        if not _sqlite_index_uses_profile(connection, index_name, sql):
            index_sql.append(sql)
    return index_sql


def _sqlite_triggers(connection: Connection) -> list[str]:
    return [sql for _, sql in _sqlite_schema_sql(connection, "trigger")]


def _constraint_uses_profile(constraint: Any) -> bool:
    columns = getattr(constraint, "columns", ())
    if any(getattr(column, "name", None) == _PROFILE_COLUMN for column in columns):
        return True
    if isinstance(constraint, CheckConstraint):
        return bool(_PROFILE_IDENTIFIER.search(str(constraint.sqltext)))
    return False


def _reflected_table_without_profile(connection: Connection) -> tuple[Table, Table] | None:
    source_metadata = MetaData()
    source = Table(_TABLE_NAME, source_metadata, autoload_with=connection)
    if _PROFILE_COLUMN not in source.c:
        return None

    target_metadata = MetaData()
    for parent in tuple(source_metadata.tables.values()):
        if parent is not source:
            parent.to_metadata(target_metadata)
    temporary = source.to_metadata(target_metadata, name=_TEMP_TABLE_NAME)
    for constraint in tuple(temporary.constraints):
        if _constraint_uses_profile(constraint):
            temporary.constraints.remove(constraint)
    temporary._columns.remove(temporary.c[_PROFILE_COLUMN])
    for index in tuple(temporary.indexes):
        temporary.indexes.remove(index)
    return source, temporary


def _row_count(connection: Connection, table_name: str) -> int:
    return int(connection.execute(text(f"SELECT COUNT(*) FROM {_quote(connection, table_name)}")).scalar_one() or 0)


def _rebuild_sqlite_table(connection: Connection) -> None:
    reflected = _reflected_table_without_profile(connection)
    if reflected is None:
        return
    source, temporary = reflected
    columns = tuple(column.name for column in source.columns if column.name != _PROFILE_COLUMN)
    if not columns:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} has no columns after removing {_PROFILE_COLUMN}")

    index_sql = _sqlite_indexes_without_profile(connection)
    trigger_sql = _sqlite_triggers(connection)
    table_name = _quote(connection, _TABLE_NAME)
    temporary_name = _quote(connection, _TEMP_TABLE_NAME)
    column_sql = ", ".join(_quote(connection, column) for column in columns)
    source_count = _row_count(connection, _TABLE_NAME)

    id_name = _quote(connection, _ID_COLUMN)
    connection.execute(text(f"UPDATE {table_name} SET {id_name} = {id_name} WHERE 1 = 0"))
    connection.execute(text(f"DROP TABLE IF EXISTS {temporary_name}"))
    temporary.create(connection, checkfirst=False)
    connection.execute(text(f"INSERT INTO {temporary_name} ({column_sql}) SELECT {column_sql} FROM {table_name}"))
    copied_count = _row_count(connection, _TEMP_TABLE_NAME)
    if copied_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} row count changed while copying: source={source_count}, copied={copied_count}")

    connection.execute(text(f"DROP TABLE {table_name}"))
    connection.execute(text(f"ALTER TABLE {temporary_name} RENAME TO {table_name}"))
    for statement in index_sql:
        connection.exec_driver_sql(statement)
    for statement in trigger_sql:
        connection.exec_driver_sql(statement)

    final_count = _row_count(connection, _TABLE_NAME)
    if final_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} row count changed after rebuild: source={source_count}, final={final_count}")


def _migrate_sqlite(connection: Connection) -> None:
    if not _table_exists(connection):
        return
    _rebuild_sqlite_table(connection)


def _mysql_profile_foreign_key_names(connection: Connection) -> list[str]:
    names: list[str] = []
    for record in inspect(connection).get_foreign_keys(_TABLE_NAME):
        constrained_columns = tuple(str(column) for column in record.get("constrained_columns") or ())
        if _PROFILE_COLUMN not in constrained_columns:
            continue
        name = record.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} profile foreign key has no usable constraint name")
        if name not in names:
            names.append(name)
    return names


def _migrate_mysql(connection: Connection) -> None:
    if not _table_exists(connection):
        return
    columns = {str(column["name"]) for column in inspect(connection).get_columns(_TABLE_NAME)}
    if _PROFILE_COLUMN not in columns:
        return

    clauses = [f"DROP FOREIGN KEY {_quote(connection, name)}" for name in _mysql_profile_foreign_key_names(connection)]
    clauses.append(f"DROP COLUMN {_quote(connection, _PROFILE_COLUMN)}")
    connection.execute(text(f"ALTER TABLE {_quote(connection, _TABLE_NAME)} {', '.join(clauses)}"))


def _migrate_sync(connection: Connection) -> None:
    database_type = connection.dialect.name
    if database_type == "sqlite":
        _migrate_sqlite(connection)
    elif database_type == "mysql":
        _migrate_mysql(connection)
    else:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    if database_type not in {"sqlite", "mysql"}:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))
    await connection.run_sync(_migrate_sync)
