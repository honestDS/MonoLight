from __future__ import annotations

import json
from typing import Any

from sqlalchemy import Index, MetaData, Table, inspect, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.schema import DropIndex

MIGRATION_ID = "20260805_remove_memory_importance_scope_v1"

_LEGACY_KEYS = frozenset({"importance", "scope"})
_JSON_COLUMNS = {
    "long_term_memory_mutation_job": ("payload", "result"),
    "long_term_memory_embedding_delta": ("snapshot",),
}
_COLUMN_REMOVALS = {
    "long_term_memory_record": ("importance", "scope"),
    "long_term_memory_revision": ("importance", "scope"),
}
_INDEX_REMOVALS = {
    "long_term_memory_record": frozenset(
        {
            "ix_long_term_memory_record_importance",
            "ix_long_term_memory_record_scope",
        }
    ),
    "long_term_memory_revision": frozenset({"ix_long_term_memory_revision_scope"}),
}


def _remove_legacy_keys(value: Any) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if key in _LEGACY_KEYS:
                changed = True
                continue
            normalized, item_changed = _remove_legacy_keys(item)
            cleaned[key] = normalized
            changed = changed or item_changed
        return cleaned, changed
    if isinstance(value, list):
        changed = False
        cleaned_items = []
        for item in value:
            normalized, item_changed = _remove_legacy_keys(item)
            cleaned_items.append(normalized)
            changed = changed or item_changed
        return cleaned_items, changed
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return value, False
        normalized, changed = _remove_legacy_keys(parsed)
        if not changed:
            return value, False
        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":")), True
    return value, False


def _reflect_table(connection: Any, table_name: str) -> Table | None:
    inspector = inspect(connection)
    if not inspector.has_table(table_name):
        return None
    return Table(table_name, MetaData(), autoload_with=connection)


def _clean_json_columns(connection: Any, table_name: str, column_names: tuple[str, ...]) -> None:
    table = _reflect_table(connection, table_name)
    if table is None or "id" not in table.c:
        return
    existing_columns = [name for name in column_names if name in table.c]
    if not existing_columns:
        return

    selected_columns = [table.c.id, *(table.c[name] for name in existing_columns)]
    for row in connection.execute(select(*selected_columns)).mappings():
        updates: dict[str, Any] = {}
        for name in existing_columns:
            normalized, changed = _remove_legacy_keys(row[name])
            if changed:
                updates[name] = normalized
        if updates:
            connection.execute(table.update().where(table.c.id == row["id"]).values(**updates))


def _drop_legacy_indexes(connection: Any, table_name: str, column_names: tuple[str, ...], index_names: frozenset[str]) -> None:
    table = _reflect_table(connection, table_name)
    if table is None:
        return

    for index_info in inspect(connection).get_indexes(table_name):
        name = index_info.get("name")
        columns = set(index_info.get("column_names") or ())
        if not isinstance(name, str) or (name not in index_names and not columns.intersection(column_names)):
            continue
        reflected_index = next((index for index in table.indexes if index.name == name), None)
        if reflected_index is None:
            index_columns = [table.c[column] for column in index_info.get("column_names") or () if column in table.c]
            if not index_columns:
                continue
            reflected_index = Index(name, *index_columns)
        connection.execute(DropIndex(reflected_index))


def _drop_columns(connection: Any, table_name: str, column_names: tuple[str, ...]) -> None:
    table = _reflect_table(connection, table_name)
    if table is None:
        return
    quote = connection.dialect.identifier_preparer.quote
    quoted_table = quote(table_name)
    for column_name in column_names:
        if column_name in table.c:
            connection.exec_driver_sql(f"ALTER TABLE {quoted_table} DROP COLUMN {quote(column_name)}")


def _migrate_sync(connection: Any) -> None:
    for table_name, column_names in _JSON_COLUMNS.items():
        _clean_json_columns(connection, table_name, column_names)

    for table_name, column_names in _COLUMN_REMOVALS.items():
        _drop_legacy_indexes(connection, table_name, column_names, _INDEX_REMOVALS.get(table_name, frozenset()))
        _drop_columns(connection, table_name, column_names)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    await connection.run_sync(_migrate_sync)
