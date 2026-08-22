from __future__ import annotations

from typing import Any

from sqlalchemy import (
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Text,
    text,
)
from sqlalchemy import (
    Enum as SAEnum,
)
from sqlalchemy.engine import Connection

from scripts.knowledge_base_container_migration_common import (
    ensure_profile_owner_key,
    foreign_key_signature,
    index_signature,
    invalid_data,
    normalize_knowledge_base,
    normalized_sql,
    profile_owner_key_matches,
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
    MANAGED_PROFILE_FOREIGN_KEY_ONDELETE,
    MYSQL_REQUIRED_DEFAULTS,
    NEW_COLUMN_NAMES,
    OWNER_CHECK_SQL,
    PROFILE_OWNER_KEY_COLUMNS,
    TARGET_COLUMN_NAMES,
    TARGET_TABLE,
    copy_target_table,
)

_TABLE_NAME = TARGET_TABLE.name
_TEMP_TABLE_NAME = "knowledge_base__container_v2"


def sqlite_table_matches_target(connection: Connection) -> bool:
    snapshot = schema_snapshot(connection)
    if _TABLE_NAME not in snapshot.get("tables", ()):
        return False
    columns = snapshot["columns"].get(_TABLE_NAME, {})
    if any(columns.get(column.name) is None or bool(columns[column.name].get("nullable")) != bool(column.nullable) or not _type_matches(column.type, columns[column.name].get("type")) for column in TARGET_TABLE.columns):
        return False
    unique_constraints = snapshot.get("unique_constraints", {}).get(_TABLE_NAME, ())
    if not any(item.get("name") == "uq_knowledge_base_managed_profile" and unique_signature(item) == ("managed_profile_id",) for item in unique_constraints):
        return False
    checks = snapshot.get("check_constraints", {}).get(_TABLE_NAME, ())
    if not any(item.get("name") == "ck_knowledge_base_type_profile_owner" and _check_signature(item.get("sqltext")) == _check_signature(OWNER_CHECK_SQL) for item in checks):
        return False
    foreign_keys = snapshot.get("foreign_keys", {}).get(_TABLE_NAME, ())
    if not profile_owner_key_matches(snapshot):
        return False
    if not _foreign_key_matches(
        foreign_keys,
        MANAGED_PROFILE_FOREIGN_KEY_COLUMNS,
        "profile",
        PROFILE_OWNER_KEY_COLUMNS,
        MANAGED_PROFILE_FOREIGN_KEY_ONDELETE,
    ):
        return False
    for name, spec in FOREIGN_KEY_SPECS.items():
        parent_table, parent_column = spec["target"].split(".", 1)
        if not _foreign_key_matches(
            foreign_keys,
            (name,),
            parent_table,
            (parent_column,),
            spec["ondelete"],
        ):
            return False
    indexes = {item.get("name"): item for item in snapshot.get("indexes", {}).get(_TABLE_NAME, ())}
    return all(
        index.name in indexes
        and index_signature(indexes[index.name])
        == index_signature(
            {
                "column_names": tuple(column.name for column in index.columns),
                "unique": bool(index.unique),
            }
        )
        for index in TARGET_TABLE.indexes
    )


def migrate_sqlite(connection: Connection) -> None:
    snapshot = schema_snapshot(connection)
    if _TABLE_NAME not in snapshot.get("tables", ()):
        ensure_profile_owner_key(connection)
        TARGET_TABLE.create(connection, checkfirst=False)
        _create_target_indexes(connection)
        _check_foreign_keys(connection)
        return

    source_columns = table_columns(snapshot, _TABLE_NAME)
    missing_columns = set(NEW_COLUMN_NAMES) - source_columns
    legacy_schema = not bool(source_columns & set(NEW_COLUMN_NAMES))
    matches_target = sqlite_table_matches_target(connection)
    if matches_target:
        repair_orphan_children(connection, snapshot)
        validate_foreign_references(connection, snapshot)
        validate_existing_type_profile(connection, snapshot)
        normalize_knowledge_base(
            connection,
            legacy_schema=legacy_schema,
            missing_columns=missing_columns,
        )
        validate_final_values(connection)
        validate_foreign_references(connection, schema_snapshot(connection))
        _check_foreign_keys(connection)
        return

    foreign_keys = _foreign_keys_enabled(connection)
    try:
        connection.execute(text("PRAGMA foreign_keys = OFF"))
        repair_orphan_children(connection, snapshot)
        validate_foreign_references(connection, snapshot)
        validate_existing_type_profile(connection, snapshot)
        ensure_profile_owner_key(connection)
        _rebuild_table(connection, snapshot)
        normalize_knowledge_base(
            connection,
            legacy_schema=legacy_schema,
            missing_columns=missing_columns,
        )
        validate_final_values(connection)
        validate_foreign_references(connection, schema_snapshot(connection))
        _check_foreign_keys(connection)
    finally:
        connection.execute(text(f"PRAGMA foreign_keys = {int(foreign_keys)}"))


def _type_matches(expected: Any, actual: Any) -> bool:
    if isinstance(expected, SAEnum):
        return isinstance(actual, String) and actual.length == expected.length
    if isinstance(expected, Text):
        return isinstance(actual, Text)
    if isinstance(expected, String):
        return isinstance(actual, String) and actual.length == expected.length
    if isinstance(expected, DateTime):
        return isinstance(actual, DateTime)
    if isinstance(expected, Integer):
        return isinstance(actual, Integer)
    return type(expected) is type(actual)


def _foreign_key_matches(
    records: Any,
    local_columns: tuple[str, ...],
    parent_table: str,
    parent_columns: tuple[str, ...],
    ondelete: str,
) -> bool:
    expected = foreign_key_signature(
        {
            "constrained_columns": local_columns,
            "referred_table": parent_table,
            "referred_columns": parent_columns,
            "options": {"ondelete": ondelete},
        }
    )
    return any(foreign_key_signature(item) == expected for item in records)


def _check_signature(value: Any) -> str:
    return normalized_sql(value).replace("(", "").replace(")", "")


def _create_target_indexes(connection: Connection) -> None:
    for index in sorted(TARGET_TABLE.indexes, key=lambda item: item.name or ""):
        index.create(connection, checkfirst=True)


def _rebuild_table(connection: Connection, snapshot: Any) -> None:
    connection.execute(text(f"DROP TABLE IF EXISTS {quote(connection, _TEMP_TABLE_NAME)}"))
    source_columns = table_columns(snapshot, _TABLE_NAME)
    missing_legacy = [name for name in LEGACY_COLUMN_NAMES if name not in source_columns]
    if missing_legacy:
        raise invalid_data(
            f"{_TABLE_NAME}.legacy_columns",
            count=len(missing_legacy),
            columns=", ".join(missing_legacy),
        )
    metadata = MetaData()
    temporary = copy_target_table(metadata, name=_TEMP_TABLE_NAME)
    source = snapshot["columns"][_TABLE_NAME]
    extra_columns = [name for name in source if name not in TARGET_COLUMN_NAMES]
    for name in extra_columns:
        record = source[name]
        kwargs: dict[str, Any] = {
            "nullable": bool(record.get("nullable", True)),
        }
        if record.get("default") is not None:
            kwargs["server_default"] = text(str(record["default"]))
        temporary.append_column(Column(name, record["type"], **kwargs))
    temporary.create(connection, checkfirst=False)
    target_names = [column.name for column in TARGET_TABLE.columns]
    expressions: list[str] = []
    for name in target_names:
        if name in source_columns:
            expression = quote(connection, name)
            if name == "knowledge_base_type":
                expression = _type_copy_expression(connection, name)
        else:
            expression = MYSQL_REQUIRED_DEFAULTS.get(name, "NULL")
        expressions.append(expression)
    names = target_names + extra_columns
    expressions.extend(quote(connection, name) for name in extra_columns)
    connection.execute(text(f"INSERT INTO {quote(connection, _TEMP_TABLE_NAME)} ({', '.join(quote(connection, name) for name in names)}) SELECT {', '.join(expressions)} FROM {quote(connection, _TABLE_NAME)}"))
    connection.execute(text(f"DROP TABLE {quote(connection, _TABLE_NAME)}"))
    connection.execute(text(f"ALTER TABLE {quote(connection, _TEMP_TABLE_NAME)} RENAME TO {quote(connection, _TABLE_NAME)}"))
    _create_target_indexes(connection)


def _type_copy_expression(connection: Connection, column_name: str) -> str:
    name = quote(connection, column_name)
    return f"CASE UPPER(TRIM({name})) WHEN 'USER' THEN 'USER' WHEN 'LLM_MANAGED' THEN 'LLM_MANAGED' ELSE {name} END"


def _foreign_keys_enabled(connection: Connection) -> bool:
    return bool(connection.execute(text("PRAGMA foreign_keys")).scalar_one())


def _check_foreign_keys(connection: Connection) -> None:
    violations = connection.execute(text("PRAGMA foreign_key_check")).all()
    if violations:
        raise invalid_data("foreign_key_check", count=len(violations))
