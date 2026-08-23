from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import CheckConstraint, ForeignKeyConstraint, MetaData, Table, UniqueConstraint, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateIndex
from sqlmodel import SQLModel

import app.models  # noqa: F401
from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED
from app.core.i18n import t

MIGRATION_ID = "20260823_remove_context_summary_work_owner_foreign_keys_v1"

_TARGET_FOREIGN_KEYS = {
    "context_summary_stage": (
        "fk_context_summary_stage_work_owner",
        (("work_id", "session_id", "uid"), "session_reply_work_item", ("id", "session_id", "uid"), "CASCADE"),
    ),
    "context_summary_fragment": (
        "fk_context_summary_fragment_work_owner",
        (("work_id", "session_id", "uid"), "session_reply_work_item", ("id", "session_id", "uid"), "CASCADE"),
    ),
}


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _normalize_ondelete(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).strip().upper().replace("_", " ").split())
    return None if normalized in {"", "NO ACTION"} else normalized


def _foreign_key_signature(record: Mapping[str, Any]) -> tuple[tuple[str, ...], str, tuple[str, ...], str | None]:
    options = record.get("options")
    if not isinstance(options, Mapping):
        options = {}
    return (
        tuple(str(column) for column in record.get("constrained_columns") or ()),
        str(record.get("referred_table") or "").lower(),
        tuple(str(column) for column in record.get("referred_columns") or ()),
        _normalize_ondelete(options.get("ondelete", record.get("ondelete"))),
    )


def _constraint_signature(constraint: ForeignKeyConstraint) -> tuple[tuple[str, ...], str, tuple[str, ...], str | None]:
    elements = tuple(constraint.elements)
    if not elements:
        return (), "", (), _normalize_ondelete(constraint.ondelete)
    return (
        tuple(element.parent.name for element in elements),
        elements[0].column.table.name.lower(),
        tuple(element.column.name for element in elements),
        _normalize_ondelete(constraint.ondelete),
    )


def _table_exists(connection: Connection, table_name: str) -> bool:
    return inspect(connection).has_table(table_name)


def _target_table(table_name: str) -> Table:
    table = SQLModel.metadata.tables.get(table_name)
    if not isinstance(table, Table):
        raise RuntimeError(f"{MIGRATION_ID}: target table metadata is missing for {table_name}")
    return table


def _target_foreign_key_signature(table_name: str) -> tuple[tuple[str, ...], str, tuple[str, ...], str | None]:
    return _TARGET_FOREIGN_KEYS[table_name][1]


def _matching_foreign_keys(connection: Connection, table_name: str) -> list[Mapping[str, Any]]:
    expected = _target_foreign_key_signature(table_name)
    return [record for record in inspect(connection).get_foreign_keys(table_name) if _foreign_key_signature(record) == expected]


def _raise_schema_mismatch(table_name: str, detail: str) -> None:
    raise RuntimeError(f"{MIGRATION_ID}: {table_name} schema mismatch: {detail}")


def _column_type_sql(connection: Connection, column: Any) -> str:
    return " ".join(str(column.type.compile(dialect=connection.dialect)).upper().split())


def _validate_columns(connection: Connection, table_name: str, source: Table, target: Table) -> None:
    source_names = tuple(column.name for column in source.columns)
    target_names = tuple(column.name for column in target.columns)
    if set(source_names) != set(target_names):
        missing = sorted(set(target_names) - set(source_names))
        extra = sorted(set(source_names) - set(target_names))
        _raise_schema_mismatch(table_name, f"missing columns={missing}, extra columns={extra}")

    for column_name in target_names:
        source_column = source.c[column_name]
        target_column = target.c[column_name]
        if bool(source_column.primary_key) != bool(target_column.primary_key):
            _raise_schema_mismatch(table_name, f"primary-key field differs for {column_name}")
        if bool(source_column.nullable) != bool(target_column.nullable):
            _raise_schema_mismatch(table_name, f"nullability differs for {column_name}")
        if _column_type_sql(connection, source_column) != _column_type_sql(connection, target_column):
            _raise_schema_mismatch(table_name, f"type differs for {column_name}")


def _unique_signature(record: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(str(column) for column in record.get("column_names") or ())


def _model_unique_constraints(table: Table) -> tuple[UniqueConstraint, ...]:
    return tuple(constraint for constraint in table.constraints if isinstance(constraint, UniqueConstraint))


def _validate_unique_constraints(connection: Connection, table_name: str, target: Table) -> None:
    actual = inspect(connection).get_unique_constraints(table_name)
    actual_by_columns = {_unique_signature(record): record for record in actual}
    for constraint in _model_unique_constraints(target):
        columns = tuple(column.name for column in constraint.columns)
        record = actual_by_columns.get(columns)
        if record is None:
            _raise_schema_mismatch(table_name, f"missing unique constraint for {columns}")
        actual_name = record.get("name")
        if constraint.name and actual_name and str(actual_name) != constraint.name:
            _raise_schema_mismatch(table_name, f"unique constraint name differs for {columns}")


def _index_signature(record: Mapping[str, Any]) -> tuple[tuple[str, ...], bool]:
    return _unique_signature(record), bool(record.get("unique"))


def _validate_model_indexes(connection: Connection, table_name: str, target: Table) -> None:
    actual = {str(record.get("name")): record for record in inspect(connection).get_indexes(table_name) if record.get("name")}
    for index in target.indexes:
        record = actual.get(str(index.name))
        expected = (tuple(column.name for column in index.columns), bool(index.unique))
        if record is None or _index_signature(record) != expected:
            _raise_schema_mismatch(table_name, f"missing or changed model index {index.name}")


def _append_extra_unique_constraints(source: Table, target: Table) -> None:
    target_signatures = {tuple(column.name for column in constraint.columns) for constraint in _model_unique_constraints(target)}
    for constraint in source.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        signature = tuple(column.name for column in constraint.columns)
        if signature in target_signatures:
            continue
        target.append_constraint(UniqueConstraint(*(target.c[column] for column in signature), name=constraint.name))
        target_signatures.add(signature)


def _append_extra_check_constraints(source: Table, target: Table) -> None:
    existing = {(constraint.name, str(constraint.sqltext)) for constraint in target.constraints if isinstance(constraint, CheckConstraint)}
    for constraint in source.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        signature = (constraint.name, str(constraint.sqltext))
        if signature not in existing:
            target.append_constraint(CheckConstraint(str(constraint.sqltext), name=constraint.name))
            existing.add(signature)


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


def _append_extra_foreign_keys(source: Table, target: Table, metadata: MetaData, copied_parents: set[str]) -> None:
    existing_signatures = {_constraint_signature(constraint) for constraint in target.foreign_key_constraints}
    removed_signature = _target_foreign_key_signature(source.name)
    for constraint in source.foreign_key_constraints:
        signature = _constraint_signature(constraint)
        if signature == removed_signature or signature in existing_signatures:
            continue
        _copy_foreign_key_parents(source, metadata, copied_parents)
        elements = tuple(constraint.elements)
        kwargs: dict[str, Any] = {}
        for option in ("ondelete", "onupdate", "deferrable", "initially", "match"):
            value = getattr(constraint, option, None)
            if value is not None:
                kwargs[option] = value
        target.append_constraint(
            ForeignKeyConstraint(
                [target.c[element.parent.name] for element in elements],
                [element.target_fullname for element in elements],
                name=constraint.name,
                **kwargs,
            )
        )
        existing_signatures.add(signature)


def _count_rows(connection: Connection, table_name: str) -> int:
    return int(connection.execute(text(f"SELECT COUNT(*) FROM {_quote(connection, table_name)}")).scalar_one() or 0)


def _rebuild_sqlite_table(connection: Connection, table_name: str) -> None:
    target = _target_table(table_name)
    source_metadata = MetaData()
    source = Table(table_name, source_metadata, autoload_with=connection)
    _validate_columns(connection, table_name, source, target)
    _validate_unique_constraints(connection, table_name, target)
    _validate_model_indexes(connection, table_name, target)

    temporary_name = f"{table_name}__remove_work_owner_new"
    connection.execute(text(f"DROP TABLE IF EXISTS {_quote(connection, temporary_name)}"))

    target_metadata = MetaData()
    copied_parents: set[str] = set()
    _copy_foreign_key_parents(target, target_metadata, copied_parents)
    temporary = target.to_metadata(target_metadata, name=temporary_name)
    _append_extra_unique_constraints(source, temporary)
    _append_extra_check_constraints(source, temporary)
    _append_extra_foreign_keys(source, temporary, target_metadata, copied_parents)

    index_statements = tuple(str(CreateIndex(index).compile(dialect=connection.dialect)) for index in source.indexes if index.name)
    for index in tuple(temporary.indexes):
        temporary.indexes.remove(index)
    target_metadata.create_all(connection, tables=[temporary], checkfirst=False)

    columns = tuple(column.name for column in target.columns)
    column_sql = ", ".join(_quote(connection, column) for column in columns)
    source_count = _count_rows(connection, table_name)
    connection.execute(text(f"INSERT INTO {_quote(connection, temporary_name)} ({column_sql}) SELECT {column_sql} FROM {_quote(connection, table_name)}"))
    copied_count = _count_rows(connection, temporary_name)
    if copied_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {table_name} row count changed while copying: source={source_count}, copied={copied_count}")

    connection.execute(text(f"DROP TABLE {_quote(connection, table_name)}"))
    connection.execute(text(f"ALTER TABLE {_quote(connection, temporary_name)} RENAME TO {_quote(connection, table_name)}"))
    for index_statement in index_statements:
        connection.execute(text(index_statement))

    final_count = _count_rows(connection, table_name)
    if final_count != source_count:
        raise RuntimeError(f"{MIGRATION_ID}: {table_name} row count changed after rebuild: source={source_count}, final={final_count}")


def _migrate_sqlite(connection: Connection) -> None:
    foreign_keys_enabled = bool(connection.exec_driver_sql("PRAGMA foreign_keys").scalar_one())
    if foreign_keys_enabled:
        connection.exec_driver_sql("PRAGMA foreign_keys = OFF")

    for table_name in _TARGET_FOREIGN_KEYS:
        if not _table_exists(connection, table_name):
            continue
        if _matching_foreign_keys(connection, table_name):
            _rebuild_sqlite_table(connection, table_name)

    violations = connection.exec_driver_sql("PRAGMA foreign_key_check").all()
    if violations:
        raise RuntimeError(f"{MIGRATION_ID}: SQLite foreign_key_check found {len(violations)} violations")


def _migrate_mysql(connection: Connection) -> None:
    for table_name, (historical_name, _) in _TARGET_FOREIGN_KEYS.items():
        if not _table_exists(connection, table_name):
            continue
        matching = _matching_foreign_keys(connection, table_name)
        if not matching:
            continue

        names: list[str] = []
        for record in matching:
            name = record.get("name")
            if not isinstance(name, str) or not name.strip():
                raise RuntimeError(f"{MIGRATION_ID}: {table_name} target foreign key {historical_name} has no usable constraint name")
            names.append(name.strip())
        for name in names:
            connection.execute(text(f"ALTER TABLE {_quote(connection, table_name)} DROP FOREIGN KEY {_quote(connection, name)}"))


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

    foreign_keys_enabled = False
    if database_type == "sqlite":
        foreign_keys_enabled = bool((await connection.exec_driver_sql("PRAGMA foreign_keys")).scalar_one())

    try:
        await connection.run_sync(_migrate_sync)
    except BaseException:
        if database_type == "sqlite" and foreign_keys_enabled:
            await session.rollback()
        raise
    else:
        if database_type == "sqlite" and foreign_keys_enabled:
            await session.commit()
    finally:
        if database_type == "sqlite":
            connection = await session.connection()
            await connection.exec_driver_sql(f"PRAGMA foreign_keys = {int(foreign_keys_enabled)}")
