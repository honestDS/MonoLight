"""为知识库 collection 建立全局所有权注册和清理队列。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from sqlalchemy import DateTime, Integer, String, Text, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateIndex

import app.models  # noqa: F401
from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED, ERR_FOREIGN_KEY_MIGRATION_DATA_INVALID
from app.core.i18n import t
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseCollectionOwner

MIGRATION_ID = "20260822_add_knowledge_base_collection_owner_v1"

_KNOWLEDGE_BASE_TABLE = KnowledgeBase.__table__
_OWNER_TABLE = KnowledgeBaseCollectionOwner.__table__
_COLLECTION_FIELDS = (
    "collection_name",
    "active_collection_name",
    "target_collection_name",
    "old_collection_name",
)
_OWNER_TRIGGER_NAMES = (
    "trg_knowledge_base_collection_owner_before_insert",
    "trg_knowledge_base_collection_owner_after_insert",
    "trg_knowledge_base_collection_owner_before_update",
    "trg_knowledge_base_collection_owner_after_update",
)
_TRIGGER_ERROR_RELATION = "knowledge_base.collection_owner"


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _qualified(connection: Connection, alias: str, column: str) -> str:
    return f"{_quote(connection, alias)}.{_quote(connection, column)}"


def _invalid_data(relation: str, count: int = 1) -> RuntimeError:
    return RuntimeError(t(ERR_FOREIGN_KEY_MIGRATION_DATA_INVALID, relation=relation, count=count))


def _raise_invalid_data(relation: str, count: int = 1) -> NoReturn:
    raise _invalid_data(relation, count)


def _table_exists(connection: Connection, table_name: str) -> bool:
    return table_name in set(inspect(connection).get_table_names())


def _table_columns(connection: Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in inspect(connection).get_columns(table_name)}


def _require_table_columns(connection: Connection, table_name: str, columns: tuple[str, ...]) -> None:
    if not _table_exists(connection, table_name):
        _raise_invalid_data(f"{table_name}.table")
    actual_columns = _table_columns(connection, table_name)
    missing = [column for column in columns if column not in actual_columns]
    if missing:
        _raise_invalid_data(f"{table_name}.columns", len(missing))


def _normalize_ondelete(value: Any) -> str | None:
    if value is None:
        return None
    normalized = " ".join(str(value).strip().upper().replace("_", " ").split())
    return None if normalized in {"", "NO ACTION"} else normalized


def _foreign_key_signature(record: Mapping[str, Any]) -> tuple[tuple[str, ...], str, tuple[str, ...], str | None]:
    options = record.get("options") or {}
    return (
        tuple(str(column) for column in record.get("constrained_columns") or ()),
        str(record.get("referred_table") or "").lower(),
        tuple(str(column) for column in record.get("referred_columns") or ()),
        _normalize_ondelete(options.get("ondelete", record.get("ondelete"))),
    )


def _type_matches(actual: Any, expected: Any) -> bool:
    if isinstance(expected, Text):
        return isinstance(actual, Text)
    if isinstance(expected, String):
        return isinstance(actual, String) and getattr(actual, "length", None) == getattr(expected, "length", None)
    if isinstance(expected, Integer):
        return isinstance(actual, Integer)
    if isinstance(expected, DateTime):
        return isinstance(actual, DateTime)
    return getattr(actual, "_type_affinity", None) is getattr(expected, "_type_affinity", None)


def _validate_knowledge_base_table(connection: Connection) -> None:
    required_columns = ("id", *_COLLECTION_FIELDS)
    _require_table_columns(connection, _KNOWLEDGE_BASE_TABLE.name, required_columns)
    primary_key = inspect(connection).get_pk_constraint(_KNOWLEDGE_BASE_TABLE.name)
    if tuple(primary_key.get("constrained_columns") or ()) != ("id",):
        _raise_invalid_data(f"{_KNOWLEDGE_BASE_TABLE.name}.id")


def _validate_owner_table(connection: Connection) -> None:
    table_name = _OWNER_TABLE.name
    columns = _table_columns(connection, table_name)
    for model_column in _OWNER_TABLE.columns:
        actual = columns.get(model_column.name)
        if actual is None:
            _raise_invalid_data(f"{table_name}.columns")
        nullable = actual.get("nullable")
        if nullable is not None and bool(nullable) != bool(model_column.nullable):
            _raise_invalid_data(f"{table_name}.{model_column.name}")
        if not _type_matches(actual.get("type"), model_column.type):
            _raise_invalid_data(f"{table_name}.{model_column.name}")

    primary_key = inspect(connection).get_pk_constraint(table_name)
    if tuple(primary_key.get("constrained_columns") or ()) != ("collection_name",):
        _raise_invalid_data(f"{table_name}.collection_name")

    expected_foreign_key = (("knowledge_base_id",), "knowledge_base", ("id",), "SET NULL")
    actual_foreign_keys = {_foreign_key_signature(item) for item in inspect(connection).get_foreign_keys(table_name)}
    if actual_foreign_keys != {expected_foreign_key}:
        _raise_invalid_data(f"{table_name}.knowledge_base_id")


def _ensure_owner_table(connection: Connection) -> None:
    if not _table_exists(connection, _OWNER_TABLE.name):
        _OWNER_TABLE.create(connection, checkfirst=False)
    _validate_owner_table(connection)
    _ensure_owner_indexes(connection)


def _ensure_owner_indexes(connection: Connection) -> None:
    inspector = inspect(connection)
    actual_indexes = inspector.get_indexes(_OWNER_TABLE.name)
    for target in _OWNER_TABLE.indexes:
        expected_columns = tuple(column.name for column in target.columns)
        expected_unique = bool(target.unique)
        same_name = next((item for item in actual_indexes if item.get("name") == target.name), None)
        if same_name is not None:
            actual_signature = (
                tuple(str(column) for column in same_name.get("column_names") or ()),
                bool(same_name.get("unique")),
            )
            if actual_signature != (expected_columns, expected_unique):
                _raise_invalid_data(f"{_OWNER_TABLE.name}.{target.name}")
            continue
        statement = str(CreateIndex(target).compile(dialect=connection.dialect))
        connection.execute(text(statement))
        actual_indexes = inspect(connection).get_indexes(_OWNER_TABLE.name)


def _count(connection: Connection, statement: str, parameters: Mapping[str, Any] | None = None) -> int:
    return int(connection.execute(text(statement), parameters or {}).scalar_one() or 0)


def _nonempty(expression: str) -> str:
    return f"{expression} IS NOT NULL AND TRIM({expression}) <> ''"


def _reference_union(connection: Connection) -> str:
    table = _quote(connection, _KNOWLEDGE_BASE_TABLE.name)
    alias = _quote(connection, "kb_ref")
    return " UNION ALL ".join(
        f"SELECT {_qualified(connection, 'kb_ref', 'id')} AS {_quote(connection, 'knowledge_base_id')}, {_qualified(connection, 'kb_ref', field)} AS {_quote(connection, 'collection_name')} FROM {table} AS {alias} WHERE {_nonempty(_qualified(connection, 'kb_ref', field))}" for field in _COLLECTION_FIELDS
    )


def _fetch_references(connection: Connection) -> dict[str, set[int]]:
    references = _reference_union(connection)
    rows = connection.execute(text(f"SELECT {_qualified(connection, 'refs', 'knowledge_base_id')}, {_qualified(connection, 'refs', 'collection_name')} FROM ({references}) AS {_quote(connection, 'refs')}")).mappings()
    result: dict[str, set[int]] = {}
    for row in rows:
        collection_name = row["collection_name"]
        knowledge_base_id = row["knowledge_base_id"]
        if collection_name is None or knowledge_base_id is None:
            continue
        result.setdefault(str(collection_name), set()).add(int(knowledge_base_id))
    return result


def _validate_reference_conflicts(connection: Connection) -> dict[str, set[int]]:
    references = _reference_union(connection)
    conflicting = _count(
        connection,
        f"SELECT COUNT(*) FROM ("
        f"SELECT {_qualified(connection, 'refs', 'collection_name')} "
        f"FROM ({references}) AS {_quote(connection, 'refs')} "
        f"GROUP BY {_qualified(connection, 'refs', 'collection_name')} "
        f"HAVING COUNT(DISTINCT {_qualified(connection, 'refs', 'knowledge_base_id')}) > 1"
        f") AS {_quote(connection, 'conflicting_collections')} ",
    )
    if conflicting:
        _raise_invalid_data(_TRIGGER_ERROR_RELATION, conflicting)
    return _fetch_references(connection)


def _validate_owner_data(connection: Connection) -> None:
    owner = _quote(connection, _OWNER_TABLE.name)
    owner_alias = _quote(connection, "owner_row")
    knowledge_base = _quote(connection, _KNOWLEDGE_BASE_TABLE.name)

    invalid_name = _count(
        connection,
        f"SELECT COUNT(*) FROM {owner} AS {owner_alias} WHERE {_qualified(connection, 'owner_row', 'collection_name')} IS NULL OR TRIM({_qualified(connection, 'owner_row', 'collection_name')}) = ''",
    )
    if invalid_name:
        _raise_invalid_data(f"{_OWNER_TABLE.name}.collection_name", invalid_name)

    invalid_attempts = _count(
        connection,
        f"SELECT COUNT(*) FROM {owner} AS {owner_alias} WHERE {_qualified(connection, 'owner_row', 'cleanup_attempt_count')} IS NULL OR {_qualified(connection, 'owner_row', 'cleanup_attempt_count')} < 0",
    )
    if invalid_attempts:
        _raise_invalid_data(f"{_OWNER_TABLE.name}.cleanup_attempt_count", invalid_attempts)

    invalid_parent = _count(
        connection,
        f"SELECT COUNT(*) FROM {owner} AS {owner_alias} "
        f"WHERE {_qualified(connection, 'owner_row', 'knowledge_base_id')} IS NOT NULL "
        f"AND NOT EXISTS (SELECT 1 FROM {knowledge_base} AS {_quote(connection, 'kb_parent')} "
        f"WHERE {_qualified(connection, 'kb_parent', 'id')} = "
        f"{_qualified(connection, 'owner_row', 'knowledge_base_id')})",
    )
    if invalid_parent:
        _raise_invalid_data(f"{_OWNER_TABLE.name}.knowledge_base_id", invalid_parent)

    references = _reference_union(connection)
    null_reused = _count(
        connection,
        f"SELECT COUNT(*) FROM {owner} AS {owner_alias} "
        f"WHERE {_qualified(connection, 'owner_row', 'knowledge_base_id')} IS NULL "
        f"AND EXISTS (SELECT 1 FROM ({references}) AS {_quote(connection, 'refs')} "
        f"WHERE {_qualified(connection, 'refs', 'collection_name')} = "
        f"{_qualified(connection, 'owner_row', 'collection_name')})",
    )
    if null_reused:
        _raise_invalid_data(_TRIGGER_ERROR_RELATION, null_reused)

    reassigned = _count(
        connection,
        f"SELECT COUNT(*) FROM {owner} AS {owner_alias} "
        f"JOIN ({references}) AS {_quote(connection, 'refs')} ON "
        f"{_qualified(connection, 'refs', 'collection_name')} = "
        f"{_qualified(connection, 'owner_row', 'collection_name')} "
        f"WHERE {_qualified(connection, 'owner_row', 'knowledge_base_id')} IS NOT NULL "
        f"AND {_qualified(connection, 'refs', 'knowledge_base_id')} <> "
        f"{_qualified(connection, 'owner_row', 'knowledge_base_id')}",
    )
    if reassigned:
        _raise_invalid_data(_TRIGGER_ERROR_RELATION, reassigned)


def _clear_stale_owners(connection: Connection) -> None:
    owner = _quote(connection, _OWNER_TABLE.name)
    knowledge_base = _quote(connection, _KNOWLEDGE_BASE_TABLE.name)
    reference_conditions = []
    for field in _COLLECTION_FIELDS:
        field_expression = _qualified(connection, "kb_parent", field)
        reference_conditions.append(f"({_nonempty(field_expression)} AND {field_expression} = {_qualified(connection, _OWNER_TABLE.name, 'collection_name')})")
    connection.execute(
        text(
            f"UPDATE {owner} SET "
            f"{_quote(connection, 'knowledge_base_id')} = NULL, "
            f"{_quote(connection, 'cleanup_attempt_count')} = 0, "
            f"{_quote(connection, 'cleanup_error')} = NULL, "
            f"{_quote(connection, 'updated_at')} = CURRENT_TIMESTAMP "
            f"WHERE {_qualified(connection, _OWNER_TABLE.name, 'knowledge_base_id')} IS NOT NULL "
            f"AND NOT EXISTS (SELECT 1 FROM {knowledge_base} AS {_quote(connection, 'kb_parent')} "
            f"WHERE {_qualified(connection, 'kb_parent', 'id')} = "
            f"{_qualified(connection, _OWNER_TABLE.name, 'knowledge_base_id')} "
            f"AND ({' OR '.join(reference_conditions)}))"
        )
    )


def _backfill_owners(connection: Connection, references: dict[str, set[int]]) -> None:
    owner = _quote(connection, _OWNER_TABLE.name)
    columns = ", ".join(
        _quote(connection, column)
        for column in (
            "collection_name",
            "knowledge_base_id",
            "cleanup_attempt_count",
            "cleanup_error",
            "created_at",
            "updated_at",
        )
    )
    insert_prefix = "INSERT OR IGNORE" if connection.dialect.name == "sqlite" else "INSERT IGNORE"
    statement = text(f"{insert_prefix} INTO {owner} ({columns}) VALUES (:collection_name, :knowledge_base_id, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)")
    for collection_name in sorted(references):
        knowledge_base_ids = references[collection_name]
        if len(knowledge_base_ids) != 1:
            _raise_invalid_data(_TRIGGER_ERROR_RELATION, len(knowledge_base_ids))
        connection.execute(
            statement,
            {
                "collection_name": collection_name,
                "knowledge_base_id": next(iter(knowledge_base_ids)),
            },
        )


def _owner_columns_sql(connection: Connection) -> str:
    return ", ".join(
        _quote(connection, column)
        for column in (
            "collection_name",
            "knowledge_base_id",
            "cleanup_attempt_count",
            "cleanup_error",
            "created_at",
            "updated_at",
        )
    )


def _new_expression(connection: Connection, prefix: str, column: str) -> str:
    return f"{prefix}.{_quote(connection, column)}"


def _before_insert_condition(connection: Connection) -> str:
    owner = _quote(connection, _OWNER_TABLE.name)
    checks = []
    for field in _COLLECTION_FIELDS:
        expression = _new_expression(connection, "NEW", field)
        checks.append(f"({_nonempty(expression)} AND EXISTS (SELECT 1 FROM {owner} AS {_quote(connection, 'owner_row')} WHERE {_qualified(connection, 'owner_row', 'collection_name')} = {expression}))")
    return " OR ".join(checks)


def _before_update_condition(connection: Connection) -> str:
    owner = _quote(connection, _OWNER_TABLE.name)
    checks = []
    for field in _COLLECTION_FIELDS:
        expression = _new_expression(connection, "NEW", field)
        checks.append(
            f"({_nonempty(expression)} AND EXISTS (SELECT 1 FROM {owner} AS "
            f"{_quote(connection, 'owner_row')} WHERE "
            f"{_qualified(connection, 'owner_row', 'collection_name')} = {expression} AND "
            f"({_qualified(connection, 'owner_row', 'knowledge_base_id')} IS NULL OR "
            f"{_qualified(connection, 'owner_row', 'knowledge_base_id')} <> OLD.{_quote(connection, 'id')})))"
        )
    return " OR ".join(checks)


def _keep_owner_condition(connection: Connection, prefix: str) -> str:
    owner_collection = _quote(connection, "collection_name")
    checks = []
    for field in _COLLECTION_FIELDS:
        expression = _new_expression(connection, prefix, field)
        checks.append(f"({_nonempty(expression)} AND {owner_collection} = {expression})")
    return " OR ".join(checks)


def _sqlite_registration_statements(connection: Connection, prefix: str) -> list[str]:
    owner = _quote(connection, _OWNER_TABLE.name)
    columns = _owner_columns_sql(connection)
    statements = []
    for field in _COLLECTION_FIELDS:
        expression = _new_expression(connection, prefix, field)
        statements.append(f"INSERT OR IGNORE INTO {owner} ({columns}) SELECT {expression}, {_new_expression(connection, prefix, 'id')}, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP WHERE {_nonempty(expression)}")
    return statements


def _mysql_registration_statements(connection: Connection, prefix: str) -> list[str]:
    owner = _quote(connection, _OWNER_TABLE.name)
    owner_alias = _quote(connection, "owner_row")
    owner_collection = _qualified(connection, "owner_row", "collection_name")
    owner_knowledge_base = _qualified(connection, "owner_row", "knowledge_base_id")
    columns = _owner_columns_sql(connection)
    statements = []
    for field in _COLLECTION_FIELDS:
        expression = _new_expression(connection, prefix, field)
        knowledge_base_id = _new_expression(connection, prefix, "id")
        statements.append(
            f"IF {_nonempty(expression)} THEN "
            f"INSERT IGNORE INTO {owner} ({columns}) VALUES ({expression}, {knowledge_base_id}, 0, NULL, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP); "
            f"IF NOT EXISTS (SELECT 1 FROM {owner} AS {owner_alias} WHERE {owner_collection} = {expression} AND {owner_knowledge_base} = {knowledge_base_id}) THEN "
            f"SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_TRIGGER_ERROR_RELATION}'; "
            f"END IF; "
            f"END IF;"
        )
    return statements


def _cleanup_registration_statement(connection: Connection) -> str:
    owner = _quote(connection, _OWNER_TABLE.name)
    keep_condition = _keep_owner_condition(connection, "NEW")
    return (
        f"UPDATE {owner} SET "
        f"{_quote(connection, 'knowledge_base_id')} = NULL, "
        f"{_quote(connection, 'cleanup_attempt_count')} = 0, "
        f"{_quote(connection, 'cleanup_error')} = NULL, "
        f"{_quote(connection, 'updated_at')} = CURRENT_TIMESTAMP "
        f"WHERE {_quote(connection, 'knowledge_base_id')} = NEW.{_quote(connection, 'id')} "
        f"AND NOT ({keep_condition})"
    )


def _sqlite_trigger_statements(connection: Connection) -> list[str]:
    knowledge_base = _quote(connection, _KNOWLEDGE_BASE_TABLE.name)
    before_insert_condition = _before_insert_condition(connection)
    before_update_condition = _before_update_condition(connection)
    registration = ";\n".join(_sqlite_registration_statements(connection, "NEW"))
    cleanup = _cleanup_registration_statement(connection)
    return [
        f"CREATE TRIGGER {_quote(connection, _OWNER_TRIGGER_NAMES[0])} BEFORE INSERT ON {knowledge_base} FOR EACH ROW WHEN ({before_insert_condition}) BEGIN SELECT RAISE(ABORT, '{_TRIGGER_ERROR_RELATION}'); END",
        f"CREATE TRIGGER {_quote(connection, _OWNER_TRIGGER_NAMES[1])} AFTER INSERT ON {knowledge_base} FOR EACH ROW BEGIN {registration}; END",
        f"CREATE TRIGGER {_quote(connection, _OWNER_TRIGGER_NAMES[2])} BEFORE UPDATE ON {knowledge_base} FOR EACH ROW WHEN ({before_update_condition}) BEGIN SELECT RAISE(ABORT, '{_TRIGGER_ERROR_RELATION}'); END",
        f"CREATE TRIGGER {_quote(connection, _OWNER_TRIGGER_NAMES[3])} AFTER UPDATE ON {knowledge_base} FOR EACH ROW BEGIN {registration}; {cleanup}; END",
    ]


def _mysql_trigger_statements(connection: Connection) -> list[str]:
    knowledge_base = _quote(connection, _KNOWLEDGE_BASE_TABLE.name)
    before_insert_condition = _before_insert_condition(connection)
    before_update_condition = _before_update_condition(connection)
    registration = " ".join(_mysql_registration_statements(connection, "NEW"))
    cleanup = _cleanup_registration_statement(connection)
    return [
        f"CREATE TRIGGER {_quote(connection, _OWNER_TRIGGER_NAMES[0])} BEFORE INSERT ON {knowledge_base} FOR EACH ROW BEGIN IF ({before_insert_condition}) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_TRIGGER_ERROR_RELATION}'; END IF; END",
        f"CREATE TRIGGER {_quote(connection, _OWNER_TRIGGER_NAMES[1])} AFTER INSERT ON {knowledge_base} FOR EACH ROW BEGIN {registration} END",
        f"CREATE TRIGGER {_quote(connection, _OWNER_TRIGGER_NAMES[2])} BEFORE UPDATE ON {knowledge_base} FOR EACH ROW BEGIN IF ({before_update_condition}) THEN SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '{_TRIGGER_ERROR_RELATION}'; END IF; END",
        f"CREATE TRIGGER {_quote(connection, _OWNER_TRIGGER_NAMES[3])} AFTER UPDATE ON {knowledge_base} FOR EACH ROW BEGIN {registration} {cleanup}; END",
    ]


def _install_triggers(connection: Connection) -> None:
    for trigger_name in _OWNER_TRIGGER_NAMES:
        connection.execute(text(f"DROP TRIGGER IF EXISTS {_quote(connection, trigger_name)}"))
    statements = _sqlite_trigger_statements(connection) if connection.dialect.name == "sqlite" else _mysql_trigger_statements(connection)
    for statement in statements:
        connection.execute(text(statement))


def _migrate_sync(connection: Connection) -> None:
    database_type = connection.dialect.name
    if database_type not in {"sqlite", "mysql"}:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))

    _validate_knowledge_base_table(connection)
    _ensure_owner_table(connection)
    references = _validate_reference_conflicts(connection)
    _validate_owner_data(connection)
    _clear_stale_owners(connection)
    _backfill_owners(connection, references)
    _install_triggers(connection)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
    if database_type not in {"sqlite", "mysql"}:
        raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=database_type))

    foreign_keys_enabled = False
    if database_type == "sqlite":
        foreign_keys_enabled = bool((await connection.execute(text("PRAGMA foreign_keys"))).scalar_one())

    try:
        await connection.run_sync(_migrate_sync)
    except BaseException:
        if database_type == "sqlite" and foreign_keys_enabled:
            await session.rollback()
        raise
    else:
        if database_type == "sqlite" and foreign_keys_enabled:
            await session.commit()
