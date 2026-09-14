from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from typing import Any

from sqlalchemy import ForeignKeyConstraint, MetaData, Table, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import NoReferencedColumnError, NoReferencedTableError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260914_restore_managed_knowledge_owner_fk_v1"

_TABLE_NAME = "managed_knowledge_item"
_PARENT_TABLE_NAME = "knowledge_base"
_CHILD_COLUMNS = ("knowledge_base_id", "uid")
_PARENT_COLUMNS = ("id", "uid")
_TEMP_TABLE_NAME = "managed_knowledge_item__owner_fk_v1"

_REFERENTIAL_ACTIONS = frozenset({"CASCADE", "NO ACTION", "RESTRICT", "SET NULL", "SET DEFAULT"})
_MATCH_TYPES = frozenset({"NONE", "PARTIAL", "FULL"})


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _table_exists(connection: Connection, table_name: str) -> bool:
    return inspect(connection).has_table(table_name)


def _table_columns(connection: Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in inspect(connection).get_columns(table_name)}


def _resolved_columns(connection: Connection, table_name: str, expected: tuple[str, ...]) -> tuple[str, ...]:
    columns = _table_columns(connection, table_name)
    by_normalized_name: dict[str, str] = {}
    for column_name in columns:
        normalized_name = column_name.casefold()
        if normalized_name in by_normalized_name:
            raise RuntimeError(f"{MIGRATION_ID}: {table_name} has ambiguous column name {column_name}")
        by_normalized_name[normalized_name] = column_name

    missing = [column_name for column_name in expected if column_name.casefold() not in by_normalized_name]
    if missing:
        raise RuntimeError(f"{MIGRATION_ID}: {table_name} is missing columns: {', '.join(missing)}")
    return tuple(by_normalized_name[column_name.casefold()] for column_name in expected)


def _same_identifiers(actual: tuple[str, ...], expected: tuple[str, ...]) -> bool:
    return len(actual) == len(expected) and all(actual_name.casefold() == expected_name.casefold() for actual_name, expected_name in zip(actual, expected, strict=True))


def _normalize_action(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).strip().upper().replace("_", " ").split())
    return normalized or None


def _safe_action(value: Any, *, detail: str) -> str | None:
    action = _normalize_action(value)
    if action is not None and action not in _REFERENTIAL_ACTIONS:
        raise RuntimeError(f"{MIGRATION_ID}: unable to preserve {detail} action: {action}")
    return action


def _record_option(record: Mapping[str, Any], option: str) -> Any:
    options = record.get("options")
    if isinstance(options, Mapping) and option in options:
        return options[option]
    return record.get(option)


def _sqlite_foreign_key_groups(connection: Connection, table_name: str) -> tuple[tuple[Mapping[str, Any], ...], ...]:
    rows = connection.exec_driver_sql(f"PRAGMA foreign_key_list({_quote(connection, table_name)})").mappings().all()
    grouped: dict[Any, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        foreign_key_id = row.get("id")
        sequence = row.get("seq")
        if foreign_key_id is None or sequence is None:
            raise RuntimeError(f"{MIGRATION_ID}: unable to identify SQLite foreign keys on {table_name}")
        grouped[foreign_key_id].append(row)

    result: list[tuple[Mapping[str, Any], ...]] = []
    for foreign_key_id, group in grouped.items():
        try:
            ordered = tuple(sorted(group, key=lambda row: int(row["seq"])))
            sequences = [int(row["seq"]) for row in ordered]
        except (KeyError, TypeError, ValueError) as error:
            raise RuntimeError(f"{MIGRATION_ID}: unable to identify SQLite foreign key {foreign_key_id} on {table_name}") from error
        if sequences != list(range(len(ordered))):
            raise RuntimeError(f"{MIGRATION_ID}: SQLite foreign key {foreign_key_id} on {table_name} has invalid column order")
        result.append(ordered)
    return tuple(result)


def _sqlite_foreign_key_signature(group: tuple[Mapping[str, Any], ...]) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    child_columns: list[str] = []
    parent_columns: list[str] = []
    parent_table: str | None = None
    for row in group:
        child_column = row.get("from")
        parent_column = row.get("to")
        row_parent_table = row.get("table")
        if not isinstance(child_column, str) or not isinstance(parent_column, str) or not isinstance(row_parent_table, str) or not row_parent_table.strip():
            raise RuntimeError(f"{MIGRATION_ID}: unable to identify SQLite composite foreign key")
        if parent_table is None:
            parent_table = row_parent_table
        elif parent_table.casefold() != row_parent_table.casefold():
            raise RuntimeError(f"{MIGRATION_ID}: SQLite composite foreign key has inconsistent parent tables")
        child_columns.append(child_column)
        parent_columns.append(parent_column)
    if parent_table is None:
        raise RuntimeError(f"{MIGRATION_ID}: unable to identify SQLite composite foreign key")
    return tuple(child_columns), parent_table, tuple(parent_columns)


def _sqlite_foreign_key_action(group: tuple[Mapping[str, Any], ...], action_name: str, *, detail: str) -> str | None:
    values: list[str | None] = []
    for row in group:
        if action_name not in row:
            raise RuntimeError(f"{MIGRATION_ID}: unable to identify SQLite {detail} action")
        values.append(_safe_action(row[action_name], detail=detail))
    if len(set(values)) != 1:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite composite foreign key has inconsistent {detail} actions")
    return values[0]


def _sqlite_foreign_key_match(group: tuple[Mapping[str, Any], ...]) -> str:
    values: list[str] = []
    for row in group:
        match = row.get("match")
        if not isinstance(match, str) or not match.strip():
            raise RuntimeError(f"{MIGRATION_ID}: unable to identify SQLite composite foreign key match semantics")
        normalized = match.strip().upper()
        if normalized not in _MATCH_TYPES:
            raise RuntimeError(f"{MIGRATION_ID}: unable to preserve SQLite composite foreign key match action: {match}")
        values.append(normalized)
    if len(set(values)) != 1:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite composite foreign key has inconsistent match semantics")
    return values[0]


def _sqlite_target_foreign_key(connection: Connection) -> tuple[Mapping[str, Any], ...] | None:
    if not _table_exists(connection, _TABLE_NAME) or not _table_exists(connection, _PARENT_TABLE_NAME):
        return None

    child_columns = _resolved_columns(connection, _TABLE_NAME, _CHILD_COLUMNS)
    parent_columns = _resolved_columns(connection, _PARENT_TABLE_NAME, _PARENT_COLUMNS)
    matches: list[tuple[Mapping[str, Any], ...]] = []
    for group in _sqlite_foreign_key_groups(connection, _TABLE_NAME):
        actual_child_columns, actual_parent_table, actual_parent_columns = _sqlite_foreign_key_signature(group)
        if _same_identifiers(actual_child_columns, child_columns) and actual_parent_table.casefold() == _PARENT_TABLE_NAME.casefold() and _same_identifiers(actual_parent_columns, parent_columns):
            matches.append(group)

    if len(matches) != 1:
        if not matches:
            raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} target composite foreign key is missing")
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} target composite foreign key is ambiguous")
    return matches[0]


def _sqlite_table_sql(connection: Connection, table_name: str) -> str | None:
    value = connection.execute(
        text("SELECT sql FROM sqlite_master WHERE type = 'table' AND name = :table_name"),
        {"table_name": table_name},
    ).scalar_one_or_none()
    return value if isinstance(value, str) else None


def _sqlite_uses_autoincrement(connection: Connection, table_name: str) -> bool:
    table_sql = _sqlite_table_sql(connection, table_name)
    return isinstance(table_sql, str) and "AUTOINCREMENT" in table_sql.upper()


def _sqlite_sequence_value(connection: Connection, table_name: str) -> int | None:
    exists = connection.execute(text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'")).scalar_one_or_none()
    if exists is None:
        return None
    value = connection.execute(
        text("SELECT seq FROM sqlite_sequence WHERE name = :table_name"),
        {"table_name": table_name},
    ).scalar_one_or_none()
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise RuntimeError(f"{MIGRATION_ID}: invalid SQLite AUTOINCREMENT sequence for {table_name}") from error


def _restore_sqlite_sequence(connection: Connection, table_name: str, value: int | None) -> None:
    exists = connection.execute(text("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'")).scalar_one_or_none()
    if exists is None:
        if value is not None:
            raise RuntimeError(f"{MIGRATION_ID}: SQLite AUTOINCREMENT sequence table disappeared")
        return

    if value is None:
        connection.execute(
            text("DELETE FROM sqlite_sequence WHERE name = :table_name"),
            {"table_name": table_name},
        )
        return

    existing = connection.execute(
        text("SELECT seq FROM sqlite_sequence WHERE name = :table_name"),
        {"table_name": table_name},
    ).scalar_one_or_none()
    if existing is None:
        connection.execute(
            text("INSERT INTO sqlite_sequence (name, seq) VALUES (:table_name, :sequence_value)"),
            {"table_name": table_name, "sequence_value": value},
        )
    else:
        connection.execute(
            text("UPDATE sqlite_sequence SET seq = :sequence_value WHERE name = :table_name"),
            {"table_name": table_name, "sequence_value": value},
        )


def _sqlite_schema_objects(connection: Connection, table_name: str) -> tuple[tuple[str, str, str], ...]:
    rows = connection.execute(
        text("SELECT type, name, sql FROM sqlite_master WHERE tbl_name = :table_name AND type IN ('index', 'trigger') AND sql IS NOT NULL ORDER BY type, name"),
        {"table_name": table_name},
    ).all()
    return tuple((str(row[0]), str(row[1]), str(row[2])) for row in rows if row[0] and row[1] and row[2])


def _normalized_schema_sql(sql: str) -> str:
    return " ".join(sql.strip().split()).upper()


def _sqlite_index_snapshot(objects: tuple[tuple[str, str, str], ...]) -> tuple[tuple[str, str], ...]:
    return tuple((name, _normalized_schema_sql(sql)) for object_type, name, sql in objects if object_type == "index")


def _sqlite_trigger_snapshot(objects: tuple[tuple[str, str, str], ...]) -> tuple[str, ...]:
    return tuple(_normalized_schema_sql(sql) for object_type, _, sql in objects if object_type == "trigger")


def _reflected_foreign_key_signature(constraint: ForeignKeyConstraint) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    child_columns: list[str] = []
    parent_columns: list[str] = []
    parent_table: str | None = None
    for element in constraint.elements:
        try:
            row_parent_table = str(element.column.table.name)
            row_parent_column = str(element.column.name)
        except (AttributeError, NoReferencedColumnError, NoReferencedTableError) as error:
            raise RuntimeError(f"{MIGRATION_ID}: unable to reflect a foreign key on {_TABLE_NAME}") from error
        if parent_table is None:
            parent_table = row_parent_table
        elif parent_table.casefold() != row_parent_table.casefold():
            raise RuntimeError(f"{MIGRATION_ID}: reflected composite foreign key has inconsistent parent tables")
        child_columns.append(str(element.parent.name))
        parent_columns.append(row_parent_column)
    if parent_table is None:
        raise RuntimeError(f"{MIGRATION_ID}: unable to identify a reflected foreign key on {_TABLE_NAME}")
    return tuple(child_columns), parent_table, tuple(parent_columns)


def _reflected_target_foreign_key(source: Table) -> ForeignKeyConstraint:
    matches: list[ForeignKeyConstraint] = []
    for constraint in source.foreign_key_constraints:
        child_columns, parent_table, parent_columns = _reflected_foreign_key_signature(constraint)
        if _same_identifiers(child_columns, _CHILD_COLUMNS) and parent_table.casefold() == _PARENT_TABLE_NAME.casefold() and _same_identifiers(parent_columns, _PARENT_COLUMNS):
            matches.append(constraint)
    if len(matches) != 1:
        if not matches:
            raise RuntimeError(f"{MIGRATION_ID}: unable to reflect {_TABLE_NAME} target composite foreign key")
        raise RuntimeError(f"{MIGRATION_ID}: reflected {_TABLE_NAME} target composite foreign key is ambiguous")
    return matches[0]


def _sqlite_foreign_key_options(
    source_constraint: ForeignKeyConstraint,
    pragma_group: tuple[Mapping[str, Any], ...],
) -> dict[str, Any]:
    options: dict[str, Any] = {}
    on_update = _sqlite_foreign_key_action(pragma_group, "on_update", detail=f"{_TABLE_NAME} ON UPDATE")
    if on_update is not None:
        options["onupdate"] = on_update
    match = _sqlite_foreign_key_match(pragma_group)
    if match != "NONE":
        options["match"] = match
    for option in ("deferrable", "initially"):
        value = getattr(source_constraint, option, None)
        if value is not None:
            options[option] = value
    return options


def _replace_sqlite_target_foreign_key(
    source: Table,
    temporary: Table,
    pragma_group: tuple[Mapping[str, Any], ...],
) -> None:
    source_constraint = _reflected_target_foreign_key(source)
    temporary_constraint = _reflected_target_foreign_key(temporary)
    temporary.constraints.remove(temporary_constraint)
    child_columns = tuple(element.parent.name for element in source_constraint.elements)
    target_columns = tuple(element.target_fullname for element in source_constraint.elements)
    temporary.append_constraint(
        ForeignKeyConstraint(
            [temporary.c[column_name] for column_name in child_columns],
            target_columns,
            name=source_constraint.name,
            ondelete="CASCADE",
            **_sqlite_foreign_key_options(source_constraint, pragma_group),
        )
    )


def _copy_foreign_key_parents(table: Table, metadata: MetaData, copied: set[str]) -> None:
    for constraint in table.foreign_key_constraints:
        for element in constraint.elements:
            try:
                parent = element.column.table
            except (AttributeError, NoReferencedColumnError, NoReferencedTableError) as error:
                raise RuntimeError(f"{MIGRATION_ID}: unable to reflect parent table for a foreign key on {table.name}") from error
            parent_key = parent.fullname
            if parent_key in copied:
                continue
            copied.add(parent_key)
            _copy_foreign_key_parents(parent, metadata, copied)
            parent.to_metadata(metadata)


def _row_count(connection: Connection, table_name: str) -> int:
    return int(connection.execute(text(f"SELECT COUNT(*) FROM {_quote(connection, table_name)}")).scalar_one() or 0)


def _rebuild_sqlite_table(connection: Connection, pragma_group: tuple[Mapping[str, Any], ...]) -> None:
    source_metadata = MetaData()
    try:
        source = Table(_TABLE_NAME, source_metadata, autoload_with=connection)
    except (NoReferencedColumnError, NoReferencedTableError, SQLAlchemyError) as error:
        raise RuntimeError(f"{MIGRATION_ID}: unable to reflect {_TABLE_NAME} and its foreign keys") from error

    _reflected_target_foreign_key(source)
    source_column_names = tuple(column.name for column in source.columns)
    source_uses_autoincrement = _sqlite_uses_autoincrement(connection, _TABLE_NAME)
    source_sequence = _sqlite_sequence_value(connection, _TABLE_NAME) if source_uses_autoincrement else None
    source_objects = _sqlite_schema_objects(connection, _TABLE_NAME)
    source_indexes = _sqlite_index_snapshot(source_objects)
    source_triggers = _sqlite_trigger_snapshot(source_objects)
    source_count = _row_count(connection, _TABLE_NAME)
    source_fk_signature = _sqlite_foreign_key_signature(pragma_group)
    source_on_update = _sqlite_foreign_key_action(pragma_group, "on_update", detail=f"{_TABLE_NAME} ON UPDATE")
    source_match = _sqlite_foreign_key_match(pragma_group)

    target_metadata = MetaData()
    _copy_foreign_key_parents(source, target_metadata, set())
    temporary = source.to_metadata(target_metadata, name=_TEMP_TABLE_NAME)
    _replace_sqlite_target_foreign_key(source, temporary, pragma_group)
    temporary.dialect_options["sqlite"]["autoincrement"] = source_uses_autoincrement
    for index in tuple(temporary.indexes):
        temporary.indexes.remove(index)

    quoted_table = _quote(connection, _TABLE_NAME)
    quoted_temporary = _quote(connection, _TEMP_TABLE_NAME)
    insertable_columns = tuple(column.name for column in source.columns if getattr(column, "computed", None) is None)
    if not insertable_columns and source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} has no insertable columns")
    quoted_columns = ", ".join(_quote(connection, column_name) for column_name in insertable_columns)

    connection.execute(text(f"DROP TABLE IF EXISTS {quoted_temporary}"))
    temporary.create(bind=connection, checkfirst=False)
    if insertable_columns:
        connection.execute(text(f"INSERT INTO {quoted_temporary} ({quoted_columns}) SELECT {quoted_columns} FROM {quoted_table}"))
    copied_count = _row_count(connection, _TEMP_TABLE_NAME)
    if copied_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} row count changed while copying: source={source_count}, copied={copied_count}")

    connection.execute(text(f"DROP TABLE {quoted_table}"))
    connection.execute(text(f"ALTER TABLE {quoted_temporary} RENAME TO {quoted_table}"))
    if source_uses_autoincrement:
        _restore_sqlite_sequence(connection, _TABLE_NAME, source_sequence)
    for _, _, schema_sql in source_objects:
        connection.execute(text(schema_sql))

    final_count = _row_count(connection, _TABLE_NAME)
    if final_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} row count changed after rebuild: source={source_count}, final={final_count}")
    if _sqlite_uses_autoincrement(connection, _TABLE_NAME) != source_uses_autoincrement:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite AUTOINCREMENT property was not preserved")
    if source_uses_autoincrement and _sqlite_sequence_value(connection, _TABLE_NAME) != source_sequence:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite AUTOINCREMENT sequence was not preserved")

    final_objects = _sqlite_schema_objects(connection, _TABLE_NAME)
    if _sqlite_index_snapshot(final_objects) != source_indexes:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite indexes were not preserved")
    if _sqlite_trigger_snapshot(final_objects) != source_triggers:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite triggers were not preserved")

    final_group = _sqlite_target_foreign_key(connection)
    if final_group is None:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} target foreign key disappeared after rebuild")
    final_signature = _sqlite_foreign_key_signature(final_group)
    final_on_update = _sqlite_foreign_key_action(final_group, "on_update", detail=f"{_TABLE_NAME} ON UPDATE")
    final_match = _sqlite_foreign_key_match(final_group)
    if final_signature != source_fk_signature or final_on_update != source_on_update or final_match != source_match:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite target foreign key semantics were not preserved")
    if _sqlite_foreign_key_action(final_group, "on_delete", detail=f"{_TABLE_NAME} ON DELETE") != "CASCADE":
        raise RuntimeError(f"{MIGRATION_ID}: SQLite target foreign key does not use ON DELETE CASCADE")

    final_metadata = MetaData()
    try:
        final = Table(_TABLE_NAME, final_metadata, autoload_with=connection)
    except (NoReferencedColumnError, NoReferencedTableError, SQLAlchemyError) as error:
        raise RuntimeError(f"{MIGRATION_ID}: unable to reflect {_TABLE_NAME} after rebuild") from error
    if tuple(column.name for column in final.columns) != source_column_names:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} columns were not preserved")


def _migrate_sqlite(connection: Connection, pragma_group: tuple[Mapping[str, Any], ...]) -> None:
    if _sqlite_foreign_key_action(pragma_group, "on_delete", detail=f"{_TABLE_NAME} ON DELETE") == "CASCADE":
        return
    _rebuild_sqlite_table(connection, pragma_group)


def _mysql_target_foreign_key(connection: Connection) -> Mapping[str, Any] | None:
    if not _table_exists(connection, _TABLE_NAME) or not _table_exists(connection, _PARENT_TABLE_NAME):
        return None

    child_columns = _resolved_columns(connection, _TABLE_NAME, _CHILD_COLUMNS)
    parent_columns = _resolved_columns(connection, _PARENT_TABLE_NAME, _PARENT_COLUMNS)
    matches: list[Mapping[str, Any]] = []
    for record in inspect(connection).get_foreign_keys(_TABLE_NAME):
        constrained_columns = tuple(str(column) for column in record.get("constrained_columns") or ())
        referred_columns = tuple(str(column) for column in record.get("referred_columns") or ())
        referred_table = record.get("referred_table")
        if _same_identifiers(constrained_columns, child_columns) and isinstance(referred_table, str) and referred_table.casefold() == _PARENT_TABLE_NAME.casefold() and _same_identifiers(referred_columns, parent_columns):
            matches.append(record)

    if len(matches) != 1:
        if not matches:
            raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} target composite foreign key is missing")
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} target composite foreign key is ambiguous")
    return matches[0]


def _mysql_action(record: Mapping[str, Any], action_name: str) -> str | None:
    return _safe_action(_record_option(record, action_name), detail=f"{_TABLE_NAME} {action_name}")


def _mysql_actions_equal(actual: str | None, expected: str | None) -> bool:
    if actual == expected:
        return True
    equivalent_restrict_actions = {None, "NO ACTION", "RESTRICT"}
    return actual in equivalent_restrict_actions and expected in equivalent_restrict_actions


def _migrate_mysql(connection: Connection) -> None:
    target = _mysql_target_foreign_key(connection)
    if target is None:
        return
    if _mysql_action(target, "ondelete") == "CASCADE":
        return

    name = target.get("name")
    referred_table = target.get("referred_table")
    if not isinstance(name, str) or not name.strip():
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} target foreign key has no usable constraint name")
    if not isinstance(referred_table, str) or not referred_table.strip():
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} target foreign key has no usable parent table")

    on_update = _mysql_action(target, "onupdate")
    on_update_clause = f" ON UPDATE {on_update}" if on_update is not None else ""
    child_columns = _resolved_columns(connection, _TABLE_NAME, _CHILD_COLUMNS)
    parent_columns = _resolved_columns(connection, _PARENT_TABLE_NAME, _PARENT_COLUMNS)
    local_sql = ", ".join(_quote(connection, column_name) for column_name in child_columns)
    parent_sql = ", ".join(_quote(connection, column_name) for column_name in parent_columns)
    quoted_table = _quote(connection, _TABLE_NAME)
    quoted_name = _quote(connection, name.strip())
    statement = f"ALTER TABLE {quoted_table} DROP FOREIGN KEY {quoted_name}, ADD CONSTRAINT {quoted_name} FOREIGN KEY ({local_sql}) REFERENCES {_quote(connection, referred_table.strip())} ({parent_sql}) ON DELETE CASCADE{on_update_clause}"
    connection.execute(text(statement))

    final = _mysql_target_foreign_key(connection)
    if final is None:
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} target foreign key disappeared after repair")
    final_name = final.get("name")
    if not isinstance(final_name, str) or final_name.strip() != name.strip():
        raise RuntimeError(f"{MIGRATION_ID}: {_TABLE_NAME} target foreign key name was not preserved")
    if _mysql_action(final, "ondelete") != "CASCADE":
        raise RuntimeError(f"{MIGRATION_ID}: MySQL target foreign key does not use ON DELETE CASCADE")
    if not _mysql_actions_equal(_mysql_action(final, "onupdate"), on_update):
        raise RuntimeError(f"{MIGRATION_ID}: MySQL target foreign key ON UPDATE action was not preserved")


async def _migrate_sqlite_async(session: AsyncSession, connection) -> None:
    pragma_group = await connection.run_sync(_sqlite_target_foreign_key)
    if pragma_group is None or _sqlite_foreign_key_action(pragma_group, "on_delete", detail=f"{_TABLE_NAME} ON DELETE") == "CASCADE":
        return

    foreign_keys_enabled = bool((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())
    try:
        await connection.exec_driver_sql("PRAGMA foreign_keys = OFF")
        if bool((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one()):
            raise RuntimeError(f"{MIGRATION_ID}: failed to disable SQLite foreign keys for table rebuild")
        await connection.run_sync(_migrate_sqlite, pragma_group)
    except BaseException:
        await session.rollback()
        raise
    else:
        await session.commit()
    finally:
        connection = await session.connection()
        await connection.exec_driver_sql(f"PRAGMA foreign_keys = {int(foreign_keys_enabled)}")
        restored = bool((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())
        if restored != foreign_keys_enabled:
            raise RuntimeError(f"{MIGRATION_ID}: failed to restore SQLite foreign_keys setting")


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    if database_type == "sqlite":
        await _migrate_sqlite_async(session, connection)
    elif database_type == "mysql":
        await connection.run_sync(_migrate_mysql)
    else:
        raise RuntimeError(f"{MIGRATION_ID}: unsupported database dialect: {database_type}")
