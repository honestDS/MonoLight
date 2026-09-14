from __future__ import annotations

from typing import Any

from sqlalchemy import MetaData, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260913_make_managed_knowledge_unique_fields_nullable_v1"

_TABLE_NAME = "managed_knowledge_item"
_TEMP_TABLE_NAME = "managed_knowledge_item__nullable_v1"
_NULLABLE_COLUMNS = ("knowledge_key", "content_hash")


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _table_columns(connection: Connection) -> dict[str, dict[str, Any]]:
    return {str(column["name"]): column for column in inspect(connection).get_columns(_TABLE_NAME)}


def _ensure_target_columns(columns: dict[str, dict[str, Any]]) -> None:
    missing = [column_name for column_name in _NULLABLE_COLUMNS if column_name not in columns]
    if missing:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} is missing columns: {', '.join(missing)}")


def _needs_sqlite_rebuild(connection: Connection) -> bool:
    inspector = inspect(connection)
    if not inspector.has_table(_TABLE_NAME):
        return False

    columns = _table_columns(connection)
    _ensure_target_columns(columns)
    return any(columns[column_name].get("nullable") is not True for column_name in _NULLABLE_COLUMNS)


def _sqlite_table_sql(connection: Connection) -> str | None:
    value = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
        {"table_name": _TABLE_NAME},
    ).scalar_one_or_none()
    return value if isinstance(value, str) else None


def _sqlite_uses_autoincrement(connection: Connection) -> bool:
    table_sql = _sqlite_table_sql(connection)
    return isinstance(table_sql, str) and "AUTOINCREMENT" in table_sql.upper()


def _sqlite_index_definitions(connection: Connection) -> tuple[tuple[str, str], ...]:
    rows = connection.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :table_name AND sql IS NOT NULL ORDER BY name"),
        {"table_name": _TABLE_NAME},
    ).all()
    return tuple((str(row[0]), str(row[1])) for row in rows if row[0] and row[1])


def _row_count(connection: Connection, table_name: str) -> int:
    quoted_table = _quote(connection, table_name)
    return int(connection.execute(text(f"SELECT COUNT(*) FROM {quoted_table}")).scalar_one() or 0)


def _copy_foreign_key_parents(table: Table, metadata: MetaData, copied: set[str]) -> None:
    for constraint in table.foreign_key_constraints:
        for element in constraint.elements:
            parent = element.column.table
            parent_key = parent.fullname
            if parent_key in copied:
                continue
            copied.add(parent_key)
            _copy_foreign_key_parents(parent, metadata, copied)
            parent.to_metadata(metadata)


def _rebuild_sqlite_table(connection: Connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table(_TABLE_NAME):
        return

    source_metadata = MetaData()
    source = Table(_TABLE_NAME, source_metadata, autoload_with=connection)
    source_columns = tuple(column.name for column in source.columns)
    source_column_names = set(source_columns)
    if any(column_name not in source_column_names for column_name in _NULLABLE_COLUMNS):
        _ensure_target_columns(_table_columns(connection))

    if all(source.c[column_name].nullable for column_name in _NULLABLE_COLUMNS):
        return

    index_definitions = _sqlite_index_definitions(connection)
    source_uses_autoincrement = _sqlite_uses_autoincrement(connection)

    target_metadata = MetaData()
    _copy_foreign_key_parents(source, target_metadata, set())
    temporary = source.to_metadata(target_metadata, name=_TEMP_TABLE_NAME)
    for column_name in _NULLABLE_COLUMNS:
        temporary.c[column_name].nullable = True
    temporary.dialect_options["sqlite"]["autoincrement"] = source_uses_autoincrement
    for index in tuple(temporary.indexes):
        temporary.indexes.remove(index)

    quoted_table = _quote(connection, _TABLE_NAME)
    quoted_temporary = _quote(connection, _TEMP_TABLE_NAME)
    quoted_columns = ", ".join(_quote(connection, column_name) for column_name in source_columns)
    source_count = _row_count(connection, _TABLE_NAME)

    connection.execute(text(f"DROP TABLE IF EXISTS {quoted_temporary}"))
    temporary.create(bind=connection, checkfirst=False)
    connection.execute(text(f"INSERT INTO {quoted_temporary} ({quoted_columns}) SELECT {quoted_columns} FROM {quoted_table}"))
    copied_count = _row_count(connection, _TEMP_TABLE_NAME)
    if copied_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} row count changed while copying: source={source_count}, copied={copied_count}")

    connection.execute(text(f"DROP TABLE {quoted_table}"))
    connection.execute(text(f"ALTER TABLE {quoted_temporary} RENAME TO {quoted_table}"))
    for _, index_definition in index_definitions:
        connection.execute(text(index_definition))

    final_inspector = inspect(connection)
    if not final_inspector.has_table(_TABLE_NAME):
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} was not restored after SQLite rebuild")

    final_columns = _table_columns(connection)
    _ensure_target_columns(final_columns)
    if any(final_columns[column_name].get("nullable") is not True for column_name in _NULLABLE_COLUMNS):
        raise RuntimeError(f"{MIGRATION_ID}: nullable column change did not take effect")
    if _sqlite_uses_autoincrement(connection) != source_uses_autoincrement:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite AUTOINCREMENT property was not preserved")

    final_index_names = {
        str(row[0])
        for row in connection.execute(
            text("SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = :table_name AND sql IS NOT NULL"),
            {"table_name": _TABLE_NAME},
        ).all()
    }
    source_index_names = {name for name, _ in index_definitions}
    if source_index_names - final_index_names:
        missing_indexes = ", ".join(sorted(source_index_names - final_index_names))
        raise RuntimeError(f"{MIGRATION_ID}: SQLite indexes were not restored: {missing_indexes}")

    final_count = _row_count(connection, _TABLE_NAME)
    if final_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} row count changed after rebuild: source={source_count}, final={final_count}")


def _migrate_mysql(connection: Connection) -> None:
    inspector = inspect(connection)
    if not inspector.has_table(_TABLE_NAME):
        return

    columns = _table_columns(connection)
    _ensure_target_columns(columns)
    quoted_table = _quote(connection, _TABLE_NAME)
    for column_name in _NULLABLE_COLUMNS:
        column = columns[column_name]
        if column.get("nullable") is True:
            continue
        column_type = column.get("type")
        if column_type is None:
            raise RuntimeError(f"{MIGRATION_ID}: unable to reflect {_TABLE_NAME}.{column_name} type")
        compiled_type = str(column_type.compile(dialect=connection.dialect))
        connection.execute(text(f"ALTER TABLE {quoted_table} MODIFY COLUMN {_quote(connection, column_name)} {compiled_type} NULL"))

    final_columns = _table_columns(connection)
    _ensure_target_columns(final_columns)
    if any(final_columns[column_name].get("nullable") is not True for column_name in _NULLABLE_COLUMNS):
        raise RuntimeError(f"{MIGRATION_ID}: MySQL nullable column change did not take effect")


async def _migrate_sqlite(session: AsyncSession, connection) -> None:
    if not await connection.run_sync(_needs_sqlite_rebuild):
        return

    foreign_keys_enabled = bool((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())
    try:
        await connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        if foreign_keys_enabled and bool((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()):
            raise RuntimeError(f"{MIGRATION_ID}: failed to disable SQLite foreign keys for table rebuild")
        await connection.run_sync(_rebuild_sqlite_table)
    except BaseException:
        await session.rollback()
        raise
    else:
        await session.commit()
    finally:
        connection = await session.connection()
        await connection.exec_driver_sql(f"PRAGMA foreign_keys = {int(foreign_keys_enabled)}")


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    if database_type == "sqlite":
        await _migrate_sqlite(session, connection)
    elif database_type == "mysql":
        await connection.run_sync(_migrate_mysql)
    else:
        raise RuntimeError(f"{MIGRATION_ID}: unsupported database dialect: {database_type}")
