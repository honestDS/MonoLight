"""知识库容器的 MySQL 增量迁移。"""

from __future__ import annotations

from typing import Any

from sqlalchemy import CheckConstraint, Column, UniqueConstraint, text
from sqlalchemy.schema import CreateColumn

from scripts.knowledge_base_container_migration_common import (
    ensure_profile_owner_key,
    foreign_key_signature,
    index_signature,
    invalid_data,
    normalize_knowledge_base,
    normalized_sql,
    quote,
    repair_orphan_children,
    schema_snapshot,
    table_columns,
    unique_signature,
    validate_existing_type_profile,
    validate_final_values,
    validate_foreign_references,
)
from scripts.knowledge_base_container_migration_schema import (
    FOREIGN_KEY_SPECS,
    LEGACY_COLUMN_NAMES,
    MANAGED_PROFILE_FOREIGN_KEY_COLUMNS,
    MANAGED_PROFILE_FOREIGN_KEY_NAME,
    MANAGED_PROFILE_FOREIGN_KEY_ONDELETE,
    MYSQL_REQUIRED_DEFAULTS,
    NEW_COLUMN_NAMES,
    PROFILE_OWNER_KEY_COLUMNS,
    TARGET_COLUMN_NAMES,
    TARGET_TABLE,
)

_ENUM_DEFAULTS = {
    "knowledge_base_type": "USER",
    "old_collection_cleanup_status": "NONE",
    "index_status": "PENDING",
}


def _default_expression(name: str) -> str:
    value = str(MYSQL_REQUIRED_DEFAULTS[name])
    if name in _ENUM_DEFAULTS:
        return f"'{_ENUM_DEFAULTS[name]}'"
    return value


def mysql_add_column_ddl(column: Column[Any], dialect: Any) -> str:
    """生成不携带外键和索引的 MySQL 加列片段。"""
    default = None
    if column.nullable is False and column.name in NEW_COLUMN_NAMES:
        default = text(_default_expression(column.name))
    copy = Column(column.name, column.type, nullable=column.nullable, server_default=default)
    return f"ADD COLUMN {CreateColumn(copy).compile(dialect=dialect)}"


def _run(connection: Any, sql: str, parameters: dict[str, Any] | None = None) -> Any:
    return connection.execute(text(sql), parameters or {})


def _table(connection: Any) -> str:
    return quote(connection, TARGET_TABLE.name)


def _action(value: Any) -> str:
    return str(value or "RESTRICT").upper()


def _index_key(value: Any) -> tuple[tuple[str, ...], bool]:
    signature = index_signature(value)
    return tuple(signature[0]), bool(signature[1])


def _foreign_key_key(value: Any) -> tuple[tuple[str, ...], str, tuple[str, ...]]:
    signature = foreign_key_signature(value)
    return tuple(signature[0]), signature[1], tuple(signature[2])


def _foreign_key_sql(
    connection: Any,
    local_columns: tuple[str, ...],
    parent_table: str,
    parent_columns: tuple[str, ...],
    spec: dict[str, Any],
) -> str:
    dialect = connection
    table = _table(connection)
    name = spec["name"]
    action = _action(spec.get("ondelete"))
    local = ", ".join(quote(dialect, column) for column in local_columns)
    parent = ", ".join(quote(dialect, column) for column in parent_columns)
    return f"ALTER TABLE {table} ADD CONSTRAINT {quote(dialect, name)} FOREIGN KEY ({local}) REFERENCES {quote(dialect, parent_table)} ({parent}) ON DELETE {action}"


def _ensure_foreign_keys(connection: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    current = list(snapshot["foreign_keys"].get(TARGET_TABLE.name, []))
    table = _table(connection)
    for local, spec in FOREIGN_KEY_SPECS.items():
        parent_table, parent_column = spec["target"].split(".", 1)
        expected_key = ((local,), parent_table, (parent_column,))
        matches = [item for item in current if _foreign_key_key(item) == expected_key]
        expected_action = _action(spec.get("ondelete"))
        if any(_action(foreign_key_signature(item)[3]) == expected_action for item in matches):
            continue
        for item in matches:
            if item.get("name"):
                _run(connection, f"ALTER TABLE {table} DROP FOREIGN KEY {quote(connection, item['name'])}")
        _run(connection, _foreign_key_sql(connection, (local,), parent_table, (parent_column,), spec))
        snapshot = schema_snapshot(connection)
        current = list(snapshot["foreign_keys"].get(TARGET_TABLE.name, []))

    managed_local_columns = MANAGED_PROFILE_FOREIGN_KEY_COLUMNS
    managed_parent_table = "profile"
    managed_parent_columns = PROFILE_OWNER_KEY_COLUMNS
    managed_spec = {
        "name": MANAGED_PROFILE_FOREIGN_KEY_NAME,
        "ondelete": MANAGED_PROFILE_FOREIGN_KEY_ONDELETE,
    }
    legacy_key = (("managed_profile_id",), managed_parent_table, ("id",))
    for item in [item for item in current if _foreign_key_key(item) == legacy_key]:
        if item.get("name"):
            _run(connection, f"ALTER TABLE {table} DROP FOREIGN KEY {quote(connection, item['name'])}")
    if any(_foreign_key_key(item) == legacy_key for item in current):
        snapshot = schema_snapshot(connection)
        current = list(snapshot["foreign_keys"].get(TARGET_TABLE.name, []))

    expected_key = (managed_local_columns, managed_parent_table, managed_parent_columns)
    matches = [item for item in current if _foreign_key_key(item) == expected_key]
    if any(item.get("name") == MANAGED_PROFILE_FOREIGN_KEY_NAME and _action(foreign_key_signature(item)[3]) == MANAGED_PROFILE_FOREIGN_KEY_ONDELETE for item in matches):
        return snapshot
    for item in matches:
        if item.get("name"):
            _run(connection, f"ALTER TABLE {table} DROP FOREIGN KEY {quote(connection, item['name'])}")
    _run(
        connection,
        _foreign_key_sql(
            connection,
            managed_local_columns,
            managed_parent_table,
            managed_parent_columns,
            managed_spec,
        ),
    )
    return schema_snapshot(connection)


def _unique_sql(connection: Any, constraint: UniqueConstraint) -> str:
    columns = ", ".join(quote(connection, column.name) for column in constraint.columns)
    name = constraint.name or f"uq_{TARGET_TABLE.name}_{next(iter(constraint.columns)).name}"
    return f"ALTER TABLE {_table(connection)} ADD CONSTRAINT {quote(connection, name)} UNIQUE ({columns})"


def _ensure_unique(connection: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    table = _table(connection)
    for constraint in TARGET_TABLE.constraints:
        if not isinstance(constraint, UniqueConstraint):
            continue
        expected = tuple(column.name for column in constraint.columns)
        unique = list(snapshot["unique_constraints"].get(TARGET_TABLE.name, []))
        indexes = list(snapshot["indexes"].get(TARGET_TABLE.name, []))
        if any(unique_signature(item) == expected for item in unique) or any(item.get("unique") and unique_signature(item) == expected for item in indexes):
            continue
        name = constraint.name or f"uq_{TARGET_TABLE.name}_{expected[0]}"
        same_name = next((item for item in unique + indexes if item.get("name") == name), None)
        if same_name:
            _run(connection, f"ALTER TABLE {table} DROP INDEX {quote(connection, name)}")
        _run(connection, _unique_sql(connection, constraint))
        snapshot = schema_snapshot(connection)
    return snapshot


def _check_sql(connection: Any, constraint: CheckConstraint) -> str:
    name = constraint.name or f"ck_{TARGET_TABLE.name}"
    return f"ALTER TABLE {_table(connection)} ADD CONSTRAINT {quote(connection, name)} CHECK ({constraint.sqltext})"


def _ensure_checks(connection: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    checks = list(snapshot["check_constraints"].get(TARGET_TABLE.name, []))
    for constraint in TARGET_TABLE.constraints:
        if not isinstance(constraint, CheckConstraint):
            continue
        expected = normalized_sql(constraint.sqltext)
        if any(normalized_sql(item.get("sqltext")) == expected for item in checks):
            continue
        name = constraint.name or f"ck_{TARGET_TABLE.name}"
        same_name = next((item for item in checks if item.get("name") == name), None)
        if same_name:
            _run(connection, f"ALTER TABLE {_table(connection)} DROP CHECK {quote(connection, name)}")
        _run(connection, _check_sql(connection, constraint))
        snapshot = schema_snapshot(connection)
        checks = list(snapshot["check_constraints"].get(TARGET_TABLE.name, []))
    return snapshot


def _ensure_indexes(connection: Any, snapshot: dict[str, Any]) -> dict[str, Any]:
    table = _table(connection)
    indexes = list(snapshot["indexes"].get(TARGET_TABLE.name, []))
    signatures = {_index_key(item) for item in indexes}
    names = {item.get("name"): item for item in indexes}
    for target in TARGET_TABLE.indexes:
        expected = (tuple(column.name for column in target.columns), bool(target.unique))
        same_name = names.get(target.name)
        if same_name and _index_key(same_name) != expected:
            _run(connection, f"ALTER TABLE {table} DROP INDEX {quote(connection, target.name)}")
            signatures.discard(_index_key(same_name))
            names.pop(target.name, None)
        if expected in signatures:
            continue
        columns = ", ".join(quote(connection, column.name) for column in target.columns)
        unique = "UNIQUE " if target.unique else ""
        _run(connection, f"CREATE {unique}INDEX {quote(connection, target.name)} ON {table} ({columns})")
        signatures.add(expected)
        names[target.name] = {"name": target.name, "column_names": expected[0], "unique": expected[1]}
    return schema_snapshot(connection)


def _ensure_target_constraints(connection: Any, snapshot: dict[str, Any]) -> None:
    snapshot = _ensure_foreign_keys(connection, snapshot)
    snapshot = _ensure_unique(connection, snapshot)
    snapshot = _ensure_checks(connection, snapshot)
    _ensure_indexes(connection, snapshot)


def migrate_mysql(connection: Any) -> None:
    """执行知识库容器的 MySQL 增量迁移。"""
    snapshot = schema_snapshot(connection)
    if TARGET_TABLE.name not in snapshot["tables"]:
        ensure_profile_owner_key(connection)
        TARGET_TABLE.create(connection, checkfirst=True)
        for index in TARGET_TABLE.indexes:
            index.create(connection, checkfirst=True)
        return
    columns = set(table_columns(snapshot, TARGET_TABLE.name))
    missing_legacy = sorted(set(LEGACY_COLUMN_NAMES) - columns)
    if missing_legacy:
        raise invalid_data(
            "knowledge_base.legacy_columns",
            count=len(missing_legacy),
            columns=missing_legacy,
        )
    legacy_schema = not columns.intersection(NEW_COLUMN_NAMES)
    missing_columns = [name for name in TARGET_COLUMN_NAMES if name in NEW_COLUMN_NAMES and name not in columns]
    repair_orphan_children(connection, snapshot)
    validate_foreign_references(connection, snapshot)
    validate_existing_type_profile(connection, snapshot)
    ensure_profile_owner_key(connection)
    for name in missing_columns:
        column = TARGET_TABLE.c[name]
        _run(connection, f"ALTER TABLE {_table(connection)} {mysql_add_column_ddl(column, connection.dialect)}")
    normalize_knowledge_base(
        connection,
        legacy_schema=legacy_schema,
        missing_columns=missing_columns,
    )
    snapshot = schema_snapshot(connection)
    validate_final_values(connection)
    validate_foreign_references(connection, snapshot)
    _ensure_target_constraints(connection, snapshot)
