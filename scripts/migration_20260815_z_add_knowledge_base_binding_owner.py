"""为知识库与 Profile 绑定补齐用户归属约束。"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, NoReturn

from sqlalchemy import Column, MetaData, Table, UniqueConstraint, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import CreateColumn, CreateIndex, ForeignKeyConstraint

import app.models  # noqa: F401
from app.core.constants import ERR_DATABASE_TYPE_UNSUPPORTED, ERR_FOREIGN_KEY_MIGRATION_DATA_INVALID
from app.core.i18n import t
from app.models.knowledge_base import KnowledgeBase, KnowledgeBaseProfileBinding
from app.models.profile import Profile

MIGRATION_ID = "20260815_add_knowledge_base_binding_owner_v1"

_PROFILE_TABLE = Profile.__table__
_KNOWLEDGE_BASE_TABLE = KnowledgeBase.__table__
_BINDING_TABLE = KnowledgeBaseProfileBinding.__table__

_PROFILE_OWNER_COLUMNS = ("id", "uid")
_KNOWLEDGE_BASE_OWNER_COLUMNS = ("id", "uid")
_BINDING_COLUMNS = tuple(column.name for column in _BINDING_TABLE.columns)
_BINDING_ID_COLUMN = "id"
_BINDING_KNOWLEDGE_BASE_COLUMN = "knowledge_base_id"
_BINDING_PROFILE_COLUMN = "profile_id"
_BINDING_UID_COLUMN = "uid"
_BINDING_TABLE_NAME = _BINDING_TABLE.name
_BINDING_TEMPORARY_TABLE_NAME = "knowledge_base_profile_binding__owner_new"


def _quote(connection: Connection, identifier: str) -> str:
    return connection.dialect.identifier_preparer.quote(identifier)


def _qualified(connection: Connection, table_name: str, column_name: str) -> str:
    return f"{_quote(connection, table_name)}.{_quote(connection, column_name)}"


def _invalid_data(relation: str, count: int = 1) -> RuntimeError:
    return RuntimeError(t(ERR_FOREIGN_KEY_MIGRATION_DATA_INVALID, relation=relation, count=count))


def _raise_invalid_data(relation: str, count: int = 1) -> NoReturn:
    raise _invalid_data(relation, count)


def _table_exists(connection: Connection, table_name: str) -> bool:
    return table_name in set(inspect(connection).get_table_names())


def _table_columns(connection: Connection, table_name: str) -> dict[str, Mapping[str, Any]]:
    return {str(column["name"]): column for column in inspect(connection).get_columns(table_name)}


def _require_table_columns(connection: Connection, table_name: str, columns: tuple[str, ...]) -> dict[str, Mapping[str, Any]]:
    if not _table_exists(connection, table_name):
        _raise_invalid_data(f"{table_name}.table")
    actual = _table_columns(connection, table_name)
    missing = [column for column in columns if column not in actual]
    if missing:
        _raise_invalid_data(f"{table_name}.columns", len(missing))
    return actual


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


def _model_foreign_key_signature(constraint: ForeignKeyConstraint) -> tuple[tuple[str, ...], str, tuple[str, ...], str | None]:
    elements = tuple(constraint.elements)
    return (
        tuple(element.parent.name for element in elements),
        elements[0].column.table.name.lower(),
        tuple(element.column.name for element in elements),
        _normalize_ondelete(constraint.ondelete),
    )


def _binding_foreign_keys() -> tuple[tuple[str, tuple[tuple[str, ...], str, tuple[str, ...], str | None]], ...]:
    return tuple((constraint.name or "", _model_foreign_key_signature(constraint)) for constraint in _BINDING_TABLE.constraints if isinstance(constraint, ForeignKeyConstraint))


def _binding_unique_pair() -> tuple[str, tuple[str, ...]]:
    for constraint in _BINDING_TABLE.constraints:
        if isinstance(constraint, UniqueConstraint) and constraint.name:
            return constraint.name, tuple(column.name for column in constraint.columns)
    _raise_invalid_data(f"{_BINDING_TABLE_NAME}.pair")


def _unique_records(connection: Connection, table_name: str) -> list[dict[str, Any]]:
    inspector = inspect(connection)
    records = [
        {
            "name": item.get("name"),
            "column_names": tuple(str(column) for column in item.get("column_names") or ()),
            "unique": True,
        }
        for item in inspector.get_unique_constraints(table_name)
    ]
    records.extend(
        {
            "name": item.get("name"),
            "column_names": tuple(str(column) for column in item.get("column_names") or ()),
            "unique": bool(item.get("unique")),
        }
        for item in inspector.get_indexes(table_name)
    )
    return records


def _ensure_unique_key(connection: Connection, table_name: str, columns: tuple[str, ...], name: str) -> None:
    _require_table_columns(connection, table_name, columns)
    column_sql = ", ".join(_quote(connection, column) for column in columns)
    duplicate_count = _count(
        connection,
        f"SELECT COALESCE(SUM(duplicate_count), 0) FROM (SELECT {column_sql}, COUNT(*) AS duplicate_count FROM {_quote(connection, table_name)} GROUP BY {column_sql} HAVING COUNT(*) > 1) AS duplicate_keys",
    )
    if duplicate_count:
        _raise_invalid_data(f"{table_name}.{name}", duplicate_count)

    records = _unique_records(connection, table_name)
    expected = tuple(columns)
    exact = any(record["unique"] and record["column_names"] == expected for record in records)
    same_name = [record for record in records if record.get("name") == name]
    if exact:
        if any(not record["unique"] or record["column_names"] != expected for record in same_name):
            _raise_invalid_data(f"{table_name}.{name}")
        return
    if same_name:
        _raise_invalid_data(f"{table_name}.{name}")

    connection.execute(text(f"CREATE UNIQUE INDEX {_quote(connection, name)} ON {_quote(connection, table_name)} ({column_sql})"))


def _ensure_parent_owner_keys(connection: Connection) -> None:
    _ensure_unique_key(
        connection,
        _PROFILE_TABLE.name,
        _PROFILE_OWNER_COLUMNS,
        next(constraint.name for constraint in _PROFILE_TABLE.constraints if isinstance(constraint, UniqueConstraint) and tuple(column.name for column in constraint.columns) == _PROFILE_OWNER_COLUMNS),
    )
    _ensure_unique_key(
        connection,
        _KNOWLEDGE_BASE_TABLE.name,
        _KNOWLEDGE_BASE_OWNER_COLUMNS,
        next(constraint.name for constraint in _KNOWLEDGE_BASE_TABLE.constraints if isinstance(constraint, UniqueConstraint) and tuple(column.name for column in constraint.columns) == _KNOWLEDGE_BASE_OWNER_COLUMNS),
    )


def _count(connection: Connection, statement: str, parameters: Mapping[str, Any] | None = None) -> int:
    return int(connection.execute(text(statement), parameters or {}).scalar_one() or 0)


def _clean_binding_data(connection: Connection) -> None:
    binding_columns = _require_table_columns(
        connection,
        _BINDING_TABLE_NAME,
        (_BINDING_ID_COLUMN, _BINDING_KNOWLEDGE_BASE_COLUMN, _BINDING_PROFILE_COLUMN),
    )
    _require_table_columns(connection, _KNOWLEDGE_BASE_TABLE.name, _KNOWLEDGE_BASE_OWNER_COLUMNS)
    _require_table_columns(connection, _PROFILE_TABLE.name, _PROFILE_OWNER_COLUMNS)

    binding = _quote(connection, _BINDING_TABLE_NAME)
    knowledge_base = _quote(connection, _KNOWLEDGE_BASE_TABLE.name)
    profile = _quote(connection, _PROFILE_TABLE.name)
    binding_kb_id = _qualified(connection, _BINDING_TABLE_NAME, _BINDING_KNOWLEDGE_BASE_COLUMN)
    binding_profile_id = _qualified(connection, _BINDING_TABLE_NAME, _BINDING_PROFILE_COLUMN)

    connection.execute(
        text(
            f"DELETE FROM {binding} "
            f"WHERE {binding_kb_id} IS NULL "
            f"OR {binding_profile_id} IS NULL "
            f"OR NOT EXISTS (SELECT 1 FROM {knowledge_base} AS kb_parent "
            f"WHERE {_qualified(connection, 'kb_parent', 'id')} = {binding_kb_id}) "
            f"OR NOT EXISTS (SELECT 1 FROM {profile} AS profile_parent "
            f"WHERE {_qualified(connection, 'profile_parent', 'id')} = {binding_profile_id})"
        )
    )

    kb_uid = _qualified(connection, "kb_parent", "uid")
    profile_uid = _qualified(connection, "profile_parent", "uid")
    connection.execute(
        text(
            f"DELETE FROM {binding} "
            f"WHERE EXISTS (SELECT 1 FROM {knowledge_base} AS kb_parent "
            f"JOIN {profile} AS profile_parent "
            f"ON {_qualified(connection, 'profile_parent', 'id')} = {binding_profile_id} "
            f"WHERE {_qualified(connection, 'kb_parent', 'id')} = {binding_kb_id} "
            f"AND (({kb_uid} IS NULL OR TRIM({kb_uid}) = '') "
            f"OR ({profile_uid} IS NULL OR TRIM({profile_uid}) = '') "
            f"OR {kb_uid} <> {profile_uid}))"
        )
    )

    if _BINDING_UID_COLUMN in binding_columns:
        connection.execute(
            text(
                f"UPDATE {binding} "
                f"SET {_quote(connection, _BINDING_UID_COLUMN)} = "
                f"(SELECT {_qualified(connection, 'kb_parent', 'uid')} FROM {knowledge_base} AS kb_parent "
                f"WHERE {_qualified(connection, 'kb_parent', 'id')} = {binding_kb_id}) "
                f"WHERE EXISTS (SELECT 1 FROM {knowledge_base} AS kb_parent "
                f"WHERE {_qualified(connection, 'kb_parent', 'id')} = {binding_kb_id})"
            )
        )

    invalid_ids = _count(
        connection,
        f"SELECT COUNT(*) FROM {binding} WHERE {_quote(connection, _BINDING_ID_COLUMN)} IS NULL",
    )
    if invalid_ids:
        _raise_invalid_data(f"{_BINDING_TABLE_NAME}.id", invalid_ids)

    duplicate_count = _count(
        connection,
        f"SELECT COALESCE(SUM(duplicate_count), 0) FROM ("
        f"SELECT {_quote(connection, _BINDING_KNOWLEDGE_BASE_COLUMN)}, {_quote(connection, _BINDING_PROFILE_COLUMN)}, COUNT(*) AS duplicate_count "
        f"FROM {binding} GROUP BY {_quote(connection, _BINDING_KNOWLEDGE_BASE_COLUMN)}, {_quote(connection, _BINDING_PROFILE_COLUMN)} "
        f"HAVING COUNT(*) > 1) AS duplicate_pairs",
    )
    if duplicate_count:
        _raise_invalid_data(f"{_BINDING_TABLE_NAME}.pair", duplicate_count)

    uid_length = int(getattr(_BINDING_TABLE.c[_BINDING_UID_COLUMN].type, "length", 0) or 0)
    if uid_length:
        length_function = "CHAR_LENGTH" if connection.dialect.name == "mysql" else "LENGTH"
        too_long = _count(
            connection,
            f"SELECT COUNT(*) FROM {knowledge_base} AS kb_parent JOIN {binding} ON {_qualified(connection, _BINDING_TABLE_NAME, _BINDING_KNOWLEDGE_BASE_COLUMN)} = {_qualified(connection, 'kb_parent', 'id')} WHERE {length_function}({_qualified(connection, 'kb_parent', 'uid')}) > :uid_length",
            {"uid_length": uid_length},
        )
        if too_long:
            _raise_invalid_data(f"{_BINDING_TABLE_NAME}.uid", too_long)


def _model_index_signature(index: Any) -> tuple[tuple[str, ...], bool]:
    return tuple(column.name for column in index.columns), bool(index.unique)


def _ensure_model_indexes(connection: Connection) -> None:
    inspector = inspect(connection)
    actual = inspector.get_indexes(_BINDING_TABLE_NAME)
    expected_indexes = tuple(_BINDING_TABLE.indexes)
    for target in expected_indexes:
        expected = _model_index_signature(target)
        same_name = next((item for item in actual if item.get("name") == target.name), None)
        if same_name is not None:
            actual_signature = (
                tuple(str(column) for column in same_name.get("column_names") or ()),
                bool(same_name.get("unique")),
            )
            if actual_signature != expected:
                _raise_invalid_data(f"{_BINDING_TABLE_NAME}.{target.name}")
            continue
        if any(
            (
                tuple(str(column) for column in item.get("column_names") or ()),
                bool(item.get("unique")),
            )
            == expected
            for item in actual
        ):
            continue
        connection.execute(text(str(CreateIndex(target).compile(dialect=connection.dialect))))
        actual = inspect(connection).get_indexes(_BINDING_TABLE_NAME)


def _binding_target_matches_sqlite(connection: Connection) -> bool:
    if not _table_exists(connection, _BINDING_TABLE_NAME):
        return False
    columns = _table_columns(connection, _BINDING_TABLE_NAME)
    for column in _BINDING_TABLE.columns:
        actual = columns.get(column.name)
        if actual is None or bool(actual.get("nullable")) != bool(column.nullable):
            return False

    primary_key = inspect(connection).get_pk_constraint(_BINDING_TABLE_NAME)
    if tuple(primary_key.get("constrained_columns") or ()) != (_BINDING_ID_COLUMN,):
        return False

    actual_foreign_keys = {_foreign_key_signature(item) for item in inspect(connection).get_foreign_keys(_BINDING_TABLE_NAME)}
    expected_foreign_keys = {signature for _, signature in _binding_foreign_keys()}
    if actual_foreign_keys != expected_foreign_keys:
        return False

    pair_name, pair_columns = _binding_unique_pair()
    del pair_name
    return any(record["unique"] and record["column_names"] == pair_columns for record in _unique_records(connection, _BINDING_TABLE_NAME))


def _copy_extra_columns(source: Table, target: Table) -> tuple[str, ...]:
    target_columns = set(_BINDING_COLUMNS)
    extra_columns: list[str] = []
    for source_column in source.columns:
        if source_column.name in target_columns:
            continue
        kwargs: dict[str, Any] = {"nullable": source_column.nullable}
        if source_column.server_default is not None:
            kwargs["server_default"] = source_column.server_default.arg
        target.append_column(Column(source_column.name, source_column.type, **kwargs))
        extra_columns.append(source_column.name)
    return tuple(extra_columns)


def _rebuild_sqlite_binding(connection: Connection) -> None:
    source_metadata = MetaData()
    source = Table(_BINDING_TABLE_NAME, source_metadata, autoload_with=connection)
    required = set((_BINDING_ID_COLUMN, _BINDING_KNOWLEDGE_BASE_COLUMN, _BINDING_PROFILE_COLUMN))
    missing = sorted(required - {column.name for column in source.c})
    if missing:
        _raise_invalid_data(f"{_BINDING_TABLE_NAME}.columns", len(missing))

    connection.execute(text(f"DROP TABLE IF EXISTS {_quote(connection, _BINDING_TEMPORARY_TABLE_NAME)}"))
    target_metadata = MetaData()
    Table(
        _PROFILE_TABLE.name,
        target_metadata,
        Column("id", _PROFILE_TABLE.c.id.type, primary_key=True),
        Column("uid", _PROFILE_TABLE.c.uid.type),
    )
    Table(
        _KNOWLEDGE_BASE_TABLE.name,
        target_metadata,
        Column("id", _KNOWLEDGE_BASE_TABLE.c.id.type, primary_key=True),
        Column("uid", _KNOWLEDGE_BASE_TABLE.c.uid.type, nullable=False),
    )
    temporary = _BINDING_TABLE.to_metadata(target_metadata, name=_BINDING_TEMPORARY_TABLE_NAME)
    for index in tuple(temporary.indexes):
        temporary.indexes.remove(index)
    extra_columns = _copy_extra_columns(source, temporary)
    temporary.create(connection, checkfirst=False)

    target_columns = tuple(_BINDING_COLUMNS) + extra_columns
    insert_columns = ", ".join(_quote(connection, column) for column in target_columns)
    source_alias = _quote(connection, "binding_source")
    knowledge_base_alias = _quote(connection, "knowledge_base_parent")
    profile_alias = _quote(connection, "profile_parent")
    source_expressions = [
        f"{source_alias}.{_quote(connection, _BINDING_ID_COLUMN)}",
        f"{source_alias}.{_quote(connection, _BINDING_KNOWLEDGE_BASE_COLUMN)}",
        f"{source_alias}.{_quote(connection, _BINDING_PROFILE_COLUMN)}",
        f"{knowledge_base_alias}.{_quote(connection, _BINDING_UID_COLUMN)}",
    ]
    source_expressions.extend(f"{source_alias}.{_quote(connection, column)}" for column in extra_columns)
    source_count = _count(connection, f"SELECT COUNT(*) FROM {_quote(connection, _BINDING_TABLE_NAME)}")
    connection.execute(
        text(
            f"INSERT INTO {_quote(connection, _BINDING_TEMPORARY_TABLE_NAME)} ({insert_columns}) "
            f"SELECT {', '.join(source_expressions)} "
            f"FROM {_quote(connection, _BINDING_TABLE_NAME)} AS {source_alias} "
            f"JOIN {_quote(connection, _KNOWLEDGE_BASE_TABLE.name)} AS {knowledge_base_alias} "
            f"ON {knowledge_base_alias}.{_quote(connection, 'id')} = {source_alias}.{_quote(connection, _BINDING_KNOWLEDGE_BASE_COLUMN)} "
            f"JOIN {_quote(connection, _PROFILE_TABLE.name)} AS {profile_alias} "
            f"ON {profile_alias}.{_quote(connection, 'id')} = {source_alias}.{_quote(connection, _BINDING_PROFILE_COLUMN)} "
            f"AND {profile_alias}.{_quote(connection, _BINDING_UID_COLUMN)} = {knowledge_base_alias}.{_quote(connection, _BINDING_UID_COLUMN)}"
        )
    )
    copied_count = _count(connection, f"SELECT COUNT(*) FROM {_quote(connection, _BINDING_TEMPORARY_TABLE_NAME)}")
    if copied_count != source_count:
        _raise_invalid_data(f"{_BINDING_TABLE_NAME}.owner", source_count - copied_count)

    connection.execute(text(f"DROP TABLE {_quote(connection, _BINDING_TABLE_NAME)}"))
    connection.execute(text(f"ALTER TABLE {_quote(connection, _BINDING_TEMPORARY_TABLE_NAME)} RENAME TO {_quote(connection, _BINDING_TABLE_NAME)}"))
    _ensure_model_indexes(connection)


def _create_sqlite_binding(connection: Connection) -> None:
    _BINDING_TABLE.create(connection, checkfirst=False)
    _ensure_model_indexes(connection)


def _check_sqlite_foreign_keys(connection: Connection) -> None:
    violations = connection.execute(text("PRAGMA foreign_key_check")).all()
    if violations:
        _raise_invalid_data("sqlite.foreign_key_check", len(violations))


def _migrate_sqlite(connection: Connection) -> None:
    foreign_keys_enabled = bool(connection.execute(text("PRAGMA foreign_keys")).scalar_one())
    if foreign_keys_enabled:
        connection.execute(text("PRAGMA foreign_keys = OFF"))

    _ensure_parent_owner_keys(connection)
    if not _table_exists(connection, _BINDING_TABLE_NAME):
        _create_sqlite_binding(connection)
        _check_sqlite_foreign_keys(connection)
        return

    _clean_binding_data(connection)
    if not _binding_target_matches_sqlite(connection):
        _rebuild_sqlite_binding(connection)
    _ensure_model_indexes(connection)
    _check_sqlite_foreign_keys(connection)


def _mysql_add_nullable_uid(connection: Connection) -> None:
    columns = _require_table_columns(
        connection,
        _BINDING_TABLE_NAME,
        (_BINDING_ID_COLUMN, _BINDING_KNOWLEDGE_BASE_COLUMN, _BINDING_PROFILE_COLUMN),
    )
    if _BINDING_UID_COLUMN in columns:
        return
    column = Column(_BINDING_UID_COLUMN, _BINDING_TABLE.c[_BINDING_UID_COLUMN].type, nullable=True)
    definition = str(CreateColumn(column).compile(dialect=connection.dialect))
    connection.execute(text(f"ALTER TABLE {_quote(connection, _BINDING_TABLE_NAME)} ADD COLUMN {definition}"))


def _mysql_make_uid_not_null(connection: Connection) -> None:
    columns = _table_columns(connection, _BINDING_TABLE_NAME)
    uid_column = columns[_BINDING_UID_COLUMN]
    if not bool(uid_column.get("nullable")):
        return
    column = Column(_BINDING_UID_COLUMN, _BINDING_TABLE.c[_BINDING_UID_COLUMN].type, nullable=False)
    definition = str(CreateColumn(column).compile(dialect=connection.dialect))
    connection.execute(text(f"ALTER TABLE {_quote(connection, _BINDING_TABLE_NAME)} MODIFY COLUMN {definition}"))


def _drop_mysql_foreign_key(connection: Connection, name: str) -> None:
    connection.execute(text(f"ALTER TABLE {_quote(connection, _BINDING_TABLE_NAME)} DROP FOREIGN KEY {_quote(connection, name)}"))


def _add_mysql_foreign_key(
    connection: Connection,
    name: str,
    signature: tuple[tuple[str, ...], str, tuple[str, ...], str | None],
) -> None:
    local_columns, parent_table, parent_columns, ondelete = signature
    local_sql = ", ".join(_quote(connection, column) for column in local_columns)
    parent_sql = ", ".join(_quote(connection, column) for column in parent_columns)
    action = ondelete or "RESTRICT"
    connection.execute(text(f"ALTER TABLE {_quote(connection, _BINDING_TABLE_NAME)} ADD CONSTRAINT {_quote(connection, name)} FOREIGN KEY ({local_sql}) REFERENCES {_quote(connection, parent_table)} ({parent_sql}) ON DELETE {action}"))


def _ensure_mysql_foreign_keys(connection: Connection) -> None:
    expected = dict(_binding_foreign_keys())
    actual = inspect(connection).get_foreign_keys(_BINDING_TABLE_NAME)
    kept: set[str] = set()
    for name, signature in expected.items():
        if any(item.get("name") == name and _foreign_key_signature(item) == signature for item in actual):
            kept.add(name)

    for item in actual:
        name = item.get("name")
        if not name:
            _raise_invalid_data(f"{_BINDING_TABLE_NAME}.foreign_key")
        if name not in kept:
            _drop_mysql_foreign_key(connection, str(name))

    actual = inspect(connection).get_foreign_keys(_BINDING_TABLE_NAME)
    for name, signature in expected.items():
        if any(item.get("name") == name and _foreign_key_signature(item) == signature for item in actual):
            continue
        _add_mysql_foreign_key(connection, name, signature)
        actual = inspect(connection).get_foreign_keys(_BINDING_TABLE_NAME)


def _ensure_mysql_unique_pair(connection: Connection) -> None:
    name, columns = _binding_unique_pair()
    _ensure_unique_key(connection, _BINDING_TABLE_NAME, columns, name)


def _migrate_mysql(connection: Connection) -> None:
    _ensure_parent_owner_keys(connection)
    if not _table_exists(connection, _BINDING_TABLE_NAME):
        _BINDING_TABLE.create(connection, checkfirst=False)
        _ensure_mysql_unique_pair(connection)
        _ensure_model_indexes(connection)
        _ensure_mysql_foreign_keys(connection)
        return

    _mysql_add_nullable_uid(connection)
    _clean_binding_data(connection)
    _mysql_make_uid_not_null(connection)
    _ensure_mysql_unique_pair(connection)
    _ensure_model_indexes(connection)
    _ensure_mysql_foreign_keys(connection)


def _migrate_sync(connection: Connection) -> None:
    if connection.dialect.name == "sqlite":
        _migrate_sqlite(connection)
        return
    if connection.dialect.name == "mysql":
        _migrate_mysql(connection)
        return
    raise RuntimeError(t(ERR_DATABASE_TYPE_UNSUPPORTED, database_type=connection.dialect.name))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    database_type = connection.dialect.name
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
    finally:
        if database_type == "sqlite":
            connection = await session.connection()
            await connection.execute(text(f"PRAGMA foreign_keys = {int(foreign_keys_enabled)}"))
