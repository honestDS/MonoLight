from __future__ import annotations

import re
from typing import NoReturn

from sqlalchemy import MetaData, Table, UniqueConstraint, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, AsyncSession
from sqlalchemy.schema import CreateTable

from app.core.constants import (
    ERR_DATABASE_TYPE_UNSUPPORTED,
    ERR_MESSAGE_PLATFORM_NAME_EXISTS,
    ERR_MIGRATION_SCRIPT_INVALID,
)
from app.core.i18n import t

MIGRATION_ID = "20261011_01_scope_message_platform_names"

_TABLE_NAME = "message_platform"
_TEMP_TABLE_NAME = "message_platform__owner_name_new"
_OWNER_UNIQUE_NAME = "uq_message_platform_uid_name"
_NAME_INDEX_NAME = "ix_message_platform_name"
_AUTOINCREMENT_PATTERN = re.compile(r"\bPRIMARY\s+KEY\s+AUTOINCREMENT\b", re.IGNORECASE)


def _invalid() -> NoReturn:
    raise RuntimeError(t(ERR_MIGRATION_SCRIPT_INVALID, script_name=MIGRATION_ID))


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _table_exists(connection: Connection) -> bool:
    return _TABLE_NAME in set(inspect(connection).get_table_names())


def _sqlite_table_sql(connection: Connection, table_name: str) -> str | None:
    value = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
        {"table_name": table_name},
    ).scalar_one_or_none()
    return value if isinstance(value, str) and value.strip() else None


def _sqlite_index_ddl(connection: Connection) -> dict[str, str]:
    rows = connection.execute(
        text("SELECT name, sql FROM sqlite_master WHERE type = 'index' AND tbl_name = :table_name AND sql IS NOT NULL"),
        {"table_name": _TABLE_NAME},
    ).all()
    return {str(row[0]): str(row[1]) for row in rows if isinstance(row[0], str) and isinstance(row[1], str) and row[1].strip()}


def _sqlite_unique_constraint_signatures(connection: Connection) -> set[tuple[str, ...]]:
    signatures: set[tuple[str, ...]] = set()
    for row in connection.exec_driver_sql(f"PRAGMA index_list({_quote(connection, _TABLE_NAME)})").all():
        if len(row) < 4 or not row[2] or row[3] != "u":
            continue
        index_name = str(row[1])
        columns = tuple(str(index_info[2]) for index_info in connection.exec_driver_sql(f"PRAGMA index_info({_quote(connection, index_name)})").all() if len(index_info) >= 3 and index_info[2] is not None)
        if columns:
            signatures.add(columns)
    return signatures


def _sqlite_trigger_ddl(connection: Connection) -> tuple[str, ...]:
    rows = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'trigger' AND tbl_name = :table_name AND sql IS NOT NULL UNION ALL SELECT sql FROM sqlite_temp_master WHERE type = 'trigger' AND tbl_name = :table_name AND sql IS NOT NULL"),
        {"table_name": _TABLE_NAME},
    ).all()
    return tuple(str(row[0]) for row in rows if isinstance(row[0], str) and row[0].strip())


def _sqlite_sequence_value(connection: Connection) -> int | None:
    if connection.execute(text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'")).scalar_one_or_none() is None:
        return None
    value = connection.execute(text("SELECT seq FROM sqlite_sequence WHERE name = :table_name"), {"table_name": _TABLE_NAME}).scalar_one_or_none()
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        _invalid()


def _columns(item: dict) -> tuple:
    return tuple(item.get("column_names") or ())


def _schema_signatures(connection: Connection) -> tuple[list[dict], set[str], bool, bool, bool]:
    inspector = inspect(connection)
    indexes = inspector.get_indexes(_TABLE_NAME)
    unique_constraints = inspector.get_unique_constraints(_TABLE_NAME)

    named_indexes = [index for index in indexes if index.get("name") == _NAME_INDEX_NAME]
    if len(named_indexes) > 1 or (named_indexes and _columns(named_indexes[0]) != ("name",)):
        _invalid()

    owner_indexes = [index for index in indexes if index.get("name") == _OWNER_UNIQUE_NAME]
    if any(not index.get("unique") or _columns(index) != ("uid", "name") for index in owner_indexes):
        _invalid()
    owner_constraints = [constraint for constraint in unique_constraints if constraint.get("name") == _OWNER_UNIQUE_NAME]
    if any(_columns(constraint) != ("uid", "name") for constraint in owner_constraints):
        _invalid()

    name_unique_indexes = {str(index["name"]) for index in indexes if index.get("unique") and _columns(index) == ("name",) and isinstance(index.get("name"), str)}
    has_owner_index = bool(owner_indexes)
    has_owner_constraint = bool(owner_constraints)
    has_name_constraint = any(_columns(constraint) == ("name",) for constraint in unique_constraints)
    if connection.dialect.name == "sqlite":
        has_name_constraint = has_name_constraint or ("name",) in _sqlite_unique_constraint_signatures(connection)
    return indexes, name_unique_indexes, has_owner_index, has_owner_constraint, has_name_constraint


def _check_duplicate_names(connection: Connection) -> None:
    duplicate = connection.execute(text("SELECT 1 FROM message_platform WHERE uid IS NOT NULL AND name IS NOT NULL GROUP BY uid, name HAVING COUNT(*) > 1 LIMIT 1")).first()
    if duplicate is not None:
        raise RuntimeError(t(ERR_MESSAGE_PLATFORM_NAME_EXISTS))


def _mysql_name_unique_keys(indexes: list[dict], unique_constraints: list[dict]) -> set[str]:
    signatures: dict[str, tuple] = {}
    for item, unique in [(index, bool(index.get("unique"))) for index in indexes] + [(constraint, True) for constraint in unique_constraints]:
        if not unique or _columns(item) != ("name",):
            continue
        key_name = item.get("name")
        if not isinstance(key_name, str) or not key_name:
            _invalid()
        signature = _columns(item)
        if key_name in signatures and signatures[key_name] != signature:
            _invalid()
        signatures[key_name] = signature
    return set(signatures)


def _owner_constraint(table: Table) -> bool:
    return any(isinstance(constraint, UniqueConstraint) and constraint.name == _OWNER_UNIQUE_NAME and tuple(column.name for column in constraint.columns) == ("uid", "name") for constraint in table.constraints)


def _rebuild_sqlite_table(
    connection: Connection,
    name_unique_indexes: set[str],
    has_owner_index: bool,
) -> None:
    source_metadata = MetaData()
    source_table = Table(_TABLE_NAME, source_metadata, autoload_with=connection)
    native_unique_constraints = _sqlite_unique_constraint_signatures(connection)
    reflected_unique_constraints = {tuple(column.name for column in constraint.columns) for constraint in source_table.constraints if isinstance(constraint, UniqueConstraint)}
    table_sql = _sqlite_table_sql(connection, _TABLE_NAME)
    if table_sql is None:
        _invalid()
    row_count = int(connection.execute(text("SELECT COUNT(*) FROM message_platform")).scalar_one())
    sequence_value = _sqlite_sequence_value(connection)
    index_ddl = _sqlite_index_ddl(connection)
    trigger_ddl = _sqlite_trigger_ddl(connection)

    replacement_metadata = MetaData()
    for reflected_table in source_metadata.tables.values():
        reflected_table.to_metadata(replacement_metadata)
    replacement = source_table.to_metadata(replacement_metadata, name=_TEMP_TABLE_NAME)
    for index in tuple(replacement.indexes):
        replacement.indexes.remove(index)
    for constraint in tuple(replacement.constraints):
        if isinstance(constraint, UniqueConstraint) and tuple(column.name for column in constraint.columns) == ("name",):
            replacement.constraints.remove(constraint)
    replacement.c.name.unique = False
    for signature in native_unique_constraints - reflected_unique_constraints:
        if signature == ("name",):
            continue
        if any(column not in source_table.c for column in signature):
            _invalid()
        if signature == ("uid", "name"):
            if not has_owner_index and not _owner_constraint(replacement):
                replacement.append_constraint(UniqueConstraint(replacement.c.uid, replacement.c.name, name=_OWNER_UNIQUE_NAME))
        else:
            replacement.append_constraint(UniqueConstraint(*(replacement.c[column] for column in signature)))
    if _AUTOINCREMENT_PATTERN.search(table_sql):
        replacement.dialect_options["sqlite"]["autoincrement"] = True
    if not has_owner_index and not _owner_constraint(replacement):
        replacement.append_constraint(UniqueConstraint(replacement.c.uid, replacement.c.name, name=_OWNER_UNIQUE_NAME))

    connection.execute(CreateTable(replacement))
    columns = tuple(column.name for column in source_table.columns if column.computed is None)
    if not columns:
        _invalid()
    column_sql = ", ".join(_quote(connection, column) for column in columns)
    connection.execute(text(f"INSERT INTO {_quote(connection, _TEMP_TABLE_NAME)} ({column_sql}) SELECT {column_sql} FROM {_quote(connection, _TABLE_NAME)}"))
    if int(connection.execute(text(f"SELECT COUNT(*) FROM {_quote(connection, _TEMP_TABLE_NAME)}")).scalar_one()) != row_count:
        _invalid()

    connection.exec_driver_sql(f"DROP TABLE {_quote(connection, _TABLE_NAME)}")
    connection.exec_driver_sql(f"ALTER TABLE {_quote(connection, _TEMP_TABLE_NAME)} RENAME TO {_quote(connection, _TABLE_NAME)}")

    if sequence_value is not None:
        if connection.execute(text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'")).scalar_one_or_none() is None:
            _invalid()
        current_value = connection.execute(text("SELECT seq FROM sqlite_sequence WHERE name = :table_name"), {"table_name": _TABLE_NAME}).scalar_one_or_none()
        if current_value is None:
            connection.execute(text("INSERT INTO sqlite_sequence (name, seq) VALUES (:table_name, :seq)"), {"table_name": _TABLE_NAME, "seq": sequence_value})
        elif int(current_value) < sequence_value:
            connection.execute(text("UPDATE sqlite_sequence SET seq = :seq WHERE name = :table_name"), {"table_name": _TABLE_NAME, "seq": sequence_value})

    skip_indexes = name_unique_indexes | {_NAME_INDEX_NAME}
    for name, ddl in index_ddl.items():
        if name not in skip_indexes:
            connection.exec_driver_sql(ddl)
    connection.exec_driver_sql("CREATE INDEX ix_message_platform_name ON message_platform (name)")
    for ddl in trigger_ddl:
        connection.exec_driver_sql(ddl)

    if _AUTOINCREMENT_PATTERN.search(table_sql):
        rebuilt_sql = _sqlite_table_sql(connection, _TABLE_NAME)
        if rebuilt_sql is None or _AUTOINCREMENT_PATTERN.search(rebuilt_sql) is None:
            _invalid()
    if int(connection.execute(text("SELECT COUNT(*) FROM message_platform")).scalar_one()) != row_count:
        _invalid()


def _apply_direct_indexes(connection: Connection, indexes: list[dict], name_unique_indexes: set[str], has_owner_index: bool, has_owner_constraint: bool) -> None:
    if not has_owner_index and not has_owner_constraint:
        connection.exec_driver_sql("CREATE UNIQUE INDEX uq_message_platform_uid_name ON message_platform (uid, name)")
    for name in name_unique_indexes:
        connection.exec_driver_sql(f"DROP INDEX {_quote(connection, name)}")
    named_index = next((index for index in indexes if index.get("name") == _NAME_INDEX_NAME), None)
    if named_index is None or _NAME_INDEX_NAME in name_unique_indexes:
        connection.exec_driver_sql("CREATE INDEX ix_message_platform_name ON message_platform (name)")


def _migrate_mysql(connection: Connection) -> None:
    inspector = inspect(connection)
    if _TABLE_NAME not in set(inspector.get_table_names()):
        return
    if not {str(column["name"]) for column in inspector.get_columns(_TABLE_NAME)}.issuperset({"uid", "name"}):
        _invalid()
    indexes, _, has_owner_index, has_owner_constraint, _ = _schema_signatures(connection)
    unique_constraints = inspector.get_unique_constraints(_TABLE_NAME)
    _check_duplicate_names(connection)
    name_unique_keys = _mysql_name_unique_keys(indexes, unique_constraints)
    named_index = next((index for index in indexes if index.get("name") == _NAME_INDEX_NAME), None)
    clauses: list[str] = []
    if not has_owner_index and not has_owner_constraint:
        clauses.append("ADD CONSTRAINT " + _quote(connection, _OWNER_UNIQUE_NAME) + f" UNIQUE ({_quote(connection, 'uid')}, {_quote(connection, 'name')})")
    clauses.extend(f"DROP INDEX {_quote(connection, key_name)}" for key_name in sorted(name_unique_keys))
    if named_index is None or _NAME_INDEX_NAME in name_unique_keys:
        clauses.append("ADD INDEX " + _quote(connection, _NAME_INDEX_NAME) + f" ({_quote(connection, 'name')})")
    if clauses:
        connection.exec_driver_sql(f"ALTER TABLE {_quote(connection, _TABLE_NAME)} " + ", ".join(clauses))
    _verify_schema(connection)


def _verify_schema(connection: Connection) -> None:
    inspector = inspect(connection)
    indexes = inspector.get_indexes(_TABLE_NAME)
    unique_constraints = inspector.get_unique_constraints(_TABLE_NAME)
    if any(index.get("unique") and _columns(index) == ("name",) for index in indexes):
        _invalid()
    if any(_columns(constraint) == ("name",) for constraint in unique_constraints):
        _invalid()
    if connection.dialect.name == "sqlite" and ("name",) in _sqlite_unique_constraint_signatures(connection):
        _invalid()
    if not any(index.get("name") == _OWNER_UNIQUE_NAME and index.get("unique") and _columns(index) == ("uid", "name") for index in indexes) and not any(constraint.get("name") == _OWNER_UNIQUE_NAME and _columns(constraint) == ("uid", "name") for constraint in unique_constraints):
        _invalid()
    name_indexes = [index for index in indexes if index.get("name") == _NAME_INDEX_NAME]
    if len(name_indexes) != 1 or name_indexes[0].get("unique") or _columns(name_indexes[0]) != ("name",):
        _invalid()


def _verify_foreign_keys(connection: Connection) -> None:
    if connection.exec_driver_sql("PRAGMA foreign_key_check").all():
        _invalid()


def _migrate_sync(connection: Connection) -> None:
    inspector = inspect(connection)
    if _TABLE_NAME not in set(inspector.get_table_names()):
        return
    if not {str(column["name"]) for column in inspector.get_columns(_TABLE_NAME)}.issuperset({"uid", "name"}):
        _invalid()
    if _TEMP_TABLE_NAME in set(inspector.get_table_names()) or _TEMP_TABLE_NAME in set(inspector.get_temp_table_names()):
        _invalid()
    indexes, name_unique_indexes, has_owner_index, has_owner_constraint, has_name_constraint = _schema_signatures(connection)
    _check_duplicate_names(connection)
    if has_name_constraint:
        _rebuild_sqlite_table(connection, name_unique_indexes, has_owner_index)
    else:
        _apply_direct_indexes(connection, indexes, name_unique_indexes, has_owner_index, has_owner_constraint)
    _verify_schema(connection)
    _verify_foreign_keys(connection)


async def _run_sqlite(connection: AsyncConnection) -> None:
    if not await connection.run_sync(_table_exists):
        await connection.rollback()
        return
    foreign_keys = int((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())
    await connection.commit()
    try:
        await connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        await connection.commit()
        async with connection.begin():
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            await connection.run_sync(_migrate_sync)
    finally:
        if connection.in_transaction():
            await connection.rollback()
        await connection.exec_driver_sql(f"PRAGMA foreign_keys = {foreign_keys}")
        await connection.commit()


async def migrate(session: AsyncSession) -> None:
    database_type = session.get_bind().dialect.name
    if database_type == "mysql":
        connection = await session.connection()
        await connection.run_sync(_migrate_mysql)
        return
    if database_type != "sqlite":
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))
    if session.in_transaction():
        await session.commit()

    bind = session.bind
    if isinstance(bind, AsyncEngine):
        async with bind.connect() as connection:
            await _run_sqlite(connection)
    elif isinstance(bind, AsyncConnection):
        await _run_sqlite(bind)
    else:
        _invalid()
