from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Mapping
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection

from app.core.constants import ERR_FOREIGN_KEY_MIGRATION_DATA_INVALID
from app.core.i18n import t
from scripts.knowledge_base_container_migration_schema import (
    ACTIVE_LEGACY_PAIRS,
    FOREIGN_KEY_SPECS,
    NEW_COLUMN_NAMES,
    OWNER_CHECK_SQL,
    PROFILE_OWNER_KEY_COLUMNS,
    PROFILE_OWNER_UNIQUE_NAME,
    KnowledgeBaseIndexStatus,
    KnowledgeBaseMigrationStatus,
    KnowledgeBaseOldCollectionCleanupStatus,
    KnowledgeBaseType,
)

_TABLE = "knowledge_base"
_PROFILE_TABLE = "profile"
_BINDING = "knowledge_base_profile_binding"
_DOCUMENT = "knowledge_base_document"
_ORPHAN_RELATIONS = (
    (_BINDING, "knowledge_base_id", _TABLE, "id"),
    (_BINDING, "profile_id", "profile", "id"),
    (_DOCUMENT, "knowledge_base_id", _TABLE, "id"),
)
_COUNTERS = (
    "migration_total_count",
    "migration_success_count",
    "migration_failure_count",
    "migration_delta_high_watermark",
    "migration_delta_applied_watermark",
)


def quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def schema_snapshot(connection: Connection) -> dict[str, Any]:
    inspector = inspect(connection)
    tables = [str(name) for name in inspector.get_table_names()]
    snapshot: dict[str, Any] = {
        "tables": tables,
        "columns": {table: {item["name"]: item for item in inspector.get_columns(table)} for table in tables},
        "indexes": {table: inspector.get_indexes(table) for table in tables},
        "unique_constraints": {table: inspector.get_unique_constraints(table) for table in tables},
        "foreign_keys": {table: inspector.get_foreign_keys(table) for table in tables},
        "check_constraints": {},
    }
    for table in tables:
        try:
            snapshot["check_constraints"][table] = inspector.get_check_constraints(table)
        except (AttributeError, NotImplementedError):
            snapshot["check_constraints"][table] = []
    return snapshot


def table_columns(snapshot: Mapping[str, Any], table_name: str) -> set[str]:
    return set(snapshot.get("columns", {}).get(table_name, {}))


def profile_owner_key_matches(snapshot: Mapping[str, Any]) -> bool:
    if _PROFILE_TABLE not in snapshot.get("tables", ()):
        return False
    if not set(PROFILE_OWNER_KEY_COLUMNS) <= table_columns(snapshot, _PROFILE_TABLE):
        return False

    unique_constraints = snapshot.get("unique_constraints", {}).get(_PROFILE_TABLE, ())
    if any(unique_signature(item) == PROFILE_OWNER_KEY_COLUMNS for item in unique_constraints):
        return True
    indexes = snapshot.get("indexes", {}).get(_PROFILE_TABLE, ())
    return any(index_signature(item) == (PROFILE_OWNER_KEY_COLUMNS, True) for item in indexes)


def ensure_profile_owner_key(connection: Connection) -> None:
    snapshot = schema_snapshot(connection)
    relation = f"{_PROFILE_TABLE}.id/uid"
    if _PROFILE_TABLE not in snapshot.get("tables", ()) or not set(PROFILE_OWNER_KEY_COLUMNS) <= table_columns(snapshot, _PROFILE_TABLE):
        raise invalid_data(relation)
    if profile_owner_key_matches(snapshot):
        return

    named_constraints = snapshot.get("unique_constraints", {}).get(_PROFILE_TABLE, ())
    named_indexes = snapshot.get("indexes", {}).get(_PROFILE_TABLE, ())
    if any(item.get("name") == PROFILE_OWNER_UNIQUE_NAME for item in (*named_constraints, *named_indexes)):
        raise invalid_data(relation)

    table = quote(connection, _PROFILE_TABLE)
    columns = ", ".join(quote(connection, column) for column in PROFILE_OWNER_KEY_COLUMNS)
    connection.execute(text(f"CREATE UNIQUE INDEX {quote(connection, PROFILE_OWNER_UNIQUE_NAME)} ON {table} ({columns})"))


def invalid_data(relation: str, count: int = 1, **kwargs: Any) -> RuntimeError:
    return RuntimeError(t(ERR_FOREIGN_KEY_MIGRATION_DATA_INVALID, relation=relation, count=count, **kwargs))


# 孤儿文档失去父记录后无法可靠确定原 collection，这里只修复关系库记录，禁止跨 collection 猜测删除 Chroma 向量。
def repair_orphan_children(connection: Connection, snapshot: Mapping[str, Any]) -> None:
    for relation in _ORPHAN_RELATIONS:
        _delete_orphans(connection, snapshot, *relation)


def validate_foreign_references(connection: Connection, snapshot: Mapping[str, Any]) -> None:
    tables = set(snapshot.get("tables", ()))
    columns = table_columns(snapshot, _TABLE)
    for local_column, spec in FOREIGN_KEY_SPECS.items():
        if local_column not in columns:
            continue
        parent_table, parent_column = str(spec["target"]).split(".", 1)
        local = quote(connection, local_column)
        required = local_column == "embedding_channel_id"
        parent_exists = parent_table in tables and parent_column in table_columns(snapshot, parent_table)
        if not parent_exists:
            condition = "1 = 1" if required else f"{local} IS NOT NULL"
        else:
            parent = quote(connection, parent_table)
            remote = quote(connection, parent_column)
            exists = f"EXISTS (SELECT 1 FROM {parent} WHERE {remote} = {local})"
            condition = f"{local} IS NULL OR NOT ({exists})" if required else f"{local} IS NOT NULL AND NOT ({exists})"
        count = _count(connection, _TABLE, condition)
        if count:
            relation = f"{_TABLE}.{local_column}->{parent_table}.{parent_column}"
            raise invalid_data(relation, count=count)


def validate_existing_type_profile(connection: Connection, snapshot: Mapping[str, Any]) -> None:
    columns = table_columns(snapshot, _TABLE)
    type_column, owner_column = "knowledge_base_type", "managed_profile_id"
    selected = [column for column in (type_column, owner_column) if column in columns]
    if not selected:
        return
    projection = ", ".join(quote(connection, column) for column in selected)
    rows = connection.execute(text(f"SELECT {projection} FROM {quote(connection, _TABLE)}")).mappings().all()
    allowed = _enum_names(KnowledgeBaseType)
    invalid_types = inconsistent = owner_without_type = 0
    owners: list[Any] = []
    for row in rows:
        type_name = _normalized_name(row.get(type_column))
        owner = row.get(owner_column)
        if type_column in columns:
            invalid_types += int(type_name not in allowed)
            if type_name in allowed and owner_column in columns:
                inconsistent += int((type_name == "USER" and owner is not None) or (type_name == "LLM_MANAGED" and owner is None))
            elif type_name == "LLM_MANAGED":
                inconsistent += 1
        elif owner is not None:
            owner_without_type += 1
        if owner_column in columns and owner is not None:
            owners.append(owner)
    if invalid_types:
        raise invalid_data(f"{_TABLE}.{type_column}", count=invalid_types)
    if inconsistent or owner_without_type:
        raise invalid_data(
            f"{_TABLE}.{type_column}/{owner_column}",
            count=inconsistent + owner_without_type,
        )
    duplicate_rows = sum(value for value in Counter(owners).values() if value > 1)
    if duplicate_rows:
        raise invalid_data(f"{_TABLE}.{owner_column}", count=duplicate_rows)

    if not {"managed_profile_id", "uid"} <= columns:
        return

    relation = f"{_TABLE}.managed_profile_owner->{_PROFILE_TABLE}.id/uid"
    managed = quote(connection, owner_column)
    managed_count = _count(connection, _TABLE, f"{managed} IS NOT NULL")
    if not managed_count:
        return

    profile_columns = table_columns(snapshot, _PROFILE_TABLE)
    if _PROFILE_TABLE not in snapshot.get("tables", ()) or not set(PROFILE_OWNER_KEY_COLUMNS) <= profile_columns:
        raise invalid_data(relation, count=managed_count)

    knowledge_base = quote(connection, _TABLE)
    profile = quote(connection, _PROFILE_TABLE)
    profile_id = quote(connection, PROFILE_OWNER_KEY_COLUMNS[0])
    profile_uid = quote(connection, PROFILE_OWNER_KEY_COLUMNS[1])
    uid = quote(connection, "uid")
    invalid_owner_condition = f"{knowledge_base}.{managed} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {profile} WHERE {profile}.{profile_id} = {knowledge_base}.{managed} AND {profile}.{profile_uid} = {knowledge_base}.{uid})"
    invalid_owners = _count(connection, _TABLE, invalid_owner_condition)
    if invalid_owners:
        raise invalid_data(relation, count=invalid_owners)


def normalize_knowledge_base(
    connection: Connection,
    *,
    legacy_schema: bool,
    missing_columns: Iterable[str],
) -> None:
    snapshot = schema_snapshot(connection)
    columns = table_columns(snapshot, _TABLE)
    new_columns = columns & set(NEW_COLUMN_NAMES)
    if not new_columns:
        return
    if "knowledge_base_type" in new_columns:
        _update(
            connection,
            "knowledge_base_type",
            "'USER'"
            if legacy_schema
            else _enum_expression(
                connection,
                "knowledge_base_type",
                _enum_names(KnowledgeBaseType),
                default="USER",
            ),
        )
    if legacy_schema and "managed_profile_id" in new_columns:
        _update(connection, "managed_profile_id", "NULL")

    for active, legacy in dict(ACTIVE_LEGACY_PAIRS).items():
        if active not in columns or legacy not in columns:
            continue
        active_sql = quote(connection, active)
        empty = f"{active_sql} IS NULL"
        if active.endswith(("_model_id", "_collection_name")):
            empty = f"({empty} OR TRIM({active_sql}) = '')"
        _update(connection, active, quote(connection, legacy), where=empty)

    for column in ("active_embedding_revision", "index_revision"):
        if column in new_columns:
            name = quote(connection, column)
            _update(
                connection,
                column,
                f"CASE WHEN {name} IS NULL OR {name} = 0 THEN 1 ELSE {name} END",
            )
    for column in _COUNTERS:
        if column in new_columns:
            name = quote(connection, column)
            _update(connection, column, f"COALESCE({name}, 0)")

    enum_updates = (
        ("migration_status", KnowledgeBaseMigrationStatus, None, False),
        (
            "old_collection_cleanup_status",
            KnowledgeBaseOldCollectionCleanupStatus,
            "NONE",
            False,
        ),
        (
            "index_status",
            KnowledgeBaseIndexStatus,
            None,
            True,
        ),
    )
    for column, enum_type, default, pending_to_ready in enum_updates:
        if column in new_columns:
            _update(
                connection,
                column,
                _enum_expression(
                    connection,
                    column,
                    _enum_names(enum_type),
                    default=default,
                    pending_to_ready=pending_to_ready,
                ),
            )


def validate_final_values(connection: Connection) -> None:
    snapshot = schema_snapshot(connection)
    columns = table_columns(snapshot, _TABLE)
    if not columns:
        return
    validate_existing_type_profile(connection, snapshot)
    if "knowledge_base_type" in columns:
        count = _invalid_enum_count(connection, "knowledge_base_type", _enum_names(KnowledgeBaseType), False)
        if count:
            raise invalid_data(f"{_TABLE}.knowledge_base_type", count=count)
    if {"knowledge_base_type", "managed_profile_id"} <= columns:
        count = _count(connection, _TABLE, f"NOT ({_quoted_owner_check(connection)})")
        if count:
            raise invalid_data(f"{_TABLE}.type_profile_owner", count=count)
    statuses = (
        ("migration_status", KnowledgeBaseMigrationStatus, True),
        ("old_collection_cleanup_status", KnowledgeBaseOldCollectionCleanupStatus, False),
        ("index_status", KnowledgeBaseIndexStatus, False),
    )
    for column, enum_type, allow_null in statuses:
        if column not in columns:
            continue
        count = _invalid_enum_count(connection, column, _enum_names(enum_type), allow_null)
        if count:
            raise invalid_data(f"{_TABLE}.{column}", count=count)


def normalize_ondelete(value: str | None) -> str | None:
    if value is None:
        return None
    value = " ".join(str(value).strip().upper().replace("_", " ").split())
    return None if value in {"", "NO ACTION"} else value


def foreign_key_signature(record: Mapping[str, Any]) -> tuple[Any, ...]:
    options = record.get("options") or {}
    return (
        tuple(record.get("constrained_columns") or ()),
        record.get("referred_table"),
        tuple(record.get("referred_columns") or ()),
        normalize_ondelete(options.get("ondelete")),
    )


def index_signature(record: Mapping[str, Any]) -> tuple[Any, ...]:
    return tuple(record.get("column_names") or ()), bool(record.get("unique"))


def unique_signature(record: Mapping[str, Any]) -> tuple[str, ...]:
    return tuple(record.get("column_names") or ())


def normalized_sql(sql: str | None) -> str:
    value = re.sub(r"\s+", " ", str(sql or "")).strip().upper()
    return value.replace("`", "").replace('"', "").replace("[", "").replace("]", "")


def _count(connection: Connection, table: str, condition: str) -> int:
    result = connection.execute(text(f"SELECT COUNT(*) FROM {quote(connection, table)} WHERE {condition}"))
    return int(result.scalar_one())


def _update(
    connection: Connection,
    column: str,
    expression: str,
    *,
    where: str | None = None,
) -> None:
    statement = f"UPDATE {quote(connection, _TABLE)} SET {quote(connection, column)} = {expression}"
    if where:
        statement += f" WHERE {where}"
    connection.execute(text(statement))


def _delete_orphans(
    connection: Connection,
    snapshot: Mapping[str, Any],
    child_table: str,
    child_column: str,
    parent_table: str,
    parent_column: str,
) -> None:
    tables = set(snapshot.get("tables", ()))
    if child_table not in tables or parent_table not in tables:
        return
    if child_column not in table_columns(snapshot, child_table):
        return
    if parent_column not in table_columns(snapshot, parent_table):
        return
    child, local = quote(connection, child_table), quote(connection, child_column)
    parent, remote = quote(connection, parent_table), quote(connection, parent_column)
    condition = f"{local} IS NOT NULL AND NOT EXISTS (SELECT 1 FROM {parent} WHERE {remote} = {local})"
    connection.execute(text(f"DELETE FROM {child} WHERE {condition}"))


def _enum_expression(
    connection: Connection,
    column: str,
    allowed: Iterable[str],
    *,
    default: str | None = None,
    pending_to_ready: bool = False,
) -> str:
    name = quote(connection, column)
    normalized = f"UPPER(TRIM({name}))"
    cases = " ".join(f"WHEN '{value}' THEN '{value}'" for value in sorted(allowed))
    mapped = f"CASE {normalized} {cases} ELSE {name} END"
    if pending_to_ready:
        return f"CASE WHEN {name} IS NULL OR TRIM({name}) = '' OR {normalized} = 'PENDING' THEN 'READY' ELSE {mapped} END"
    if default is not None:
        return f"CASE WHEN {name} IS NULL OR TRIM({name}) = '' THEN '{default}' ELSE {mapped} END"
    return f"CASE WHEN {name} IS NULL OR TRIM({name}) = '' THEN {name} ELSE {mapped} END"


def _invalid_enum_count(
    connection: Connection,
    column: str,
    allowed: Iterable[str],
    allow_null: bool,
) -> int:
    values = ", ".join(f"'{value}'" for value in sorted(allowed))
    name = quote(connection, column)
    invalid = f"TRIM({name}) NOT IN ({values})"
    condition = f"{name} IS NOT NULL AND ({invalid})" if allow_null else f"{name} IS NULL OR ({invalid})"
    return _count(connection, _TABLE, condition)


def _quoted_owner_check(connection: Connection) -> str:
    expression = OWNER_CHECK_SQL
    for name in ("knowledge_base_type", "managed_profile_id"):
        expression = re.sub(
            rf"(?<![A-Za-z0-9_]){name}(?![A-Za-z0-9_])",
            quote(connection, name),
            expression,
        )
    return expression


def _enum_names(enum_type: Any) -> frozenset[str]:
    return frozenset(member.name for member in enum_type)


def _normalized_name(value: Any) -> str | None:
    if value is None:
        return None
    return str(getattr(value, "name", value)).strip().upper()
