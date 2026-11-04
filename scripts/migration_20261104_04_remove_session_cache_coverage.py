from __future__ import annotations

import json
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261104_04_remove_session_cache_coverage"

_BATCH_SIZE = 200
_DEPRECATED_FIELDS = (
    "cache_coverage",
    "cache_reported_input_tokens",
    "cache_reported_cached_tokens",
)
_WORK_METADATA_FIELDS = ("llm_request_metadata", "latest_llm_request_metadata")
_TABLE_COLUMNS = (
    ("chat_session", "session_id", "llm_request_metadata"),
    ("session_reply_work_item", "id", "execution_state"),
    ("session_reply_work_checkpoint", "work_id", "checkpoint"),
    ("session_reply_stream_event", "id", "event"),
    ("session_event", "id", "event"),
)


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _decode_json_object(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    if isinstance(decoded, dict):
        return decoded
    return None


def _clean_metadata(value: Any, label: str) -> Any:
    metadata = _decode_json_object(value)
    if metadata is None:
        return value
    if not any(field in metadata for field in _DEPRECATED_FIELDS):
        return value

    total_input_tokens = metadata.get("total_input_tokens")
    total_cached_tokens = metadata.get("total_cached_tokens")
    if not _is_non_negative_int(total_input_tokens) or not _is_non_negative_int(total_cached_tokens):
        raise RuntimeError(f"{MIGRATION_ID}: {label}: total_input_tokens and total_cached_tokens must be non-negative integers")
    if total_cached_tokens > total_input_tokens:
        raise RuntimeError(f"{MIGRATION_ID}: {label}: total_cached_tokens must not exceed total_input_tokens")

    cleaned = dict(metadata)
    for field in _DEPRECATED_FIELDS:
        cleaned.pop(field, None)
    cleaned["cache_hit_rate"] = total_cached_tokens / total_input_tokens if total_input_tokens > 0 else None
    return cleaned


def _transform_value(value: Any, column_name: str, label: str) -> Any:
    if column_name == "llm_request_metadata":
        return _clean_metadata(value, label)

    root = _decode_json_object(value)
    if root is None:
        return value
    if column_name == "event" and root.get("type") == "llm_request_metadata":
        return _clean_metadata(value, label)

    if column_name in {"execution_state", "checkpoint"}:
        metadata_fields = _WORK_METADATA_FIELDS
    elif column_name == "event":
        metadata_fields = ("llm_request_metadata",)
    else:
        return value

    transformed = root
    for field in metadata_fields:
        original_metadata = root.get(field)
        cleaned = _clean_metadata(original_metadata, f"{label}.{field}")
        if cleaned is original_metadata:
            continue
        if transformed is root:
            transformed = dict(root)
        transformed[field] = cleaned

    response = root.get("response")
    if isinstance(response, dict):
        original_metadata = response.get("llm_request_metadata")
        cleaned = _clean_metadata(original_metadata, f"{label}.response.llm_request_metadata")
        if cleaned is not original_metadata:
            if transformed is root:
                transformed = dict(root)
            transformed["response"] = {**response, "llm_request_metadata": cleaned}

    if transformed is root:
        return value
    return transformed


def _reflect_tables(sync_connection) -> dict[str, Table]:
    table_names = set(inspect(sync_connection).get_table_names())
    metadata = MetaData()
    tables: dict[str, Table] = {}
    for table_name, _, _ in _TABLE_COLUMNS:
        if table_name in table_names:
            tables[table_name] = Table(table_name, metadata, autoload_with=sync_connection, resolve_fks=False)
    return tables


async def _migrate_table(
    session: AsyncSession,
    table: Table | None,
    *,
    key_column_name: str,
    value_column_name: str,
) -> None:
    if table is None or key_column_name not in table.c or value_column_name not in table.c:
        return

    key_column = table.c[key_column_name]
    value_column = table.c[value_column_name]
    last_key: Any = None
    while True:
        statement = select(key_column, value_column)
        if last_key is not None:
            statement = statement.where(key_column > last_key)
        statement = statement.order_by(key_column.asc()).limit(_BATCH_SIZE)
        rows = (await session.execute(statement)).mappings().all()
        if not rows:
            return

        for row in rows:
            row_key = row[key_column_name]
            original_value = row[value_column_name]
            label = f"{table.name} {key_column_name}={row_key!r} {value_column_name}"
            transformed = _transform_value(original_value, value_column_name, label)
            if transformed is not original_value:
                await session.execute(update(table).where(key_column == row_key).values(**{value_column_name: transformed}))

        last_key = rows[-1][key_column_name]
        if len(rows) < _BATCH_SIZE:
            return


async def migrate(session: AsyncSession) -> None:
    try:
        await ensure_sqlite_outer_transaction(session)
        dialect_name = session.get_bind().dialect.name
        if dialect_name not in {"sqlite", "mysql"}:
            raise RuntimeError(f"{MIGRATION_ID}: unsupported database dialect: {dialect_name}")

        connection = await session.connection()
        tables = await connection.run_sync(_reflect_tables)
        for table_name, key_column_name, value_column_name in _TABLE_COLUMNS:
            await _migrate_table(
                session,
                tables.get(table_name),
                key_column_name=key_column_name,
                value_column_name=value_column_name,
            )
    except BaseException:
        await session.rollback()
        raise
