from __future__ import annotations

import json
from typing import Any

from sqlalchemy import MetaData, Table, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261104_03_add_session_usage_reporting"

_BATCH_SIZE = 200
_MAIN_DIALOGUE = "main_dialogue"
_SUMMARY_SAFETY_MARGIN_TOKENS = 256
_METADATA_KEYS = {"llm_request_metadata", "latest_llm_request_metadata"}
_TABLE_NAMES = (
    "chat_session",
    "profile",
    "provider_usage_detail",
    "session_reply_work_item",
    "session_reply_work_checkpoint",
    "session_reply_stream_event",
    "session_event",
)


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _raise_invalid(label: str, detail: str) -> None:
    raise RuntimeError(f"{MIGRATION_ID}: {label}: {detail}")


def _decode_object(value: Any, label: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise RuntimeError(f"{MIGRATION_ID}: {label} is not valid JSON") from exc
        if isinstance(decoded, dict):
            return dict(decoded)
    _raise_invalid(label, "must be a JSON object")


def _decode_container(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return value
    if isinstance(decoded, (dict, list)):
        return decoded
    return value


def _optional_object(value: Any) -> dict[str, Any] | None:
    if value is None:
        return {}
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return None
        return dict(decoded) if isinstance(decoded, dict) else None
    return None


def _normalize_input_source(metadata: dict[str, Any]) -> None:
    if "context_window_tokens" not in metadata or "max_output_tokens" not in metadata:
        return
    input_tokens = metadata.get("input_tokens")
    source = metadata.get("input_tokens_source")
    if source == "provider" and _is_positive_int(input_tokens):
        return
    if source == "estimated":
        return
    metadata["input_tokens_source"] = "estimated"


def _read_explicit_totals(metadata: dict[str, Any], label: str) -> tuple[int, int, int, bool, bool]:
    has_input = "total_input_tokens" in metadata
    has_cached = "total_cached_tokens" in metadata
    if has_input != has_cached:
        _raise_invalid(label, "total_input_tokens and total_cached_tokens must be present together")

    total_input = 0
    total_cached = 0
    if has_input:
        total_input = metadata.get("total_input_tokens")
        total_cached = metadata.get("total_cached_tokens")
        if not _is_non_negative_int(total_input) or not _is_non_negative_int(total_cached):
            _raise_invalid(label, "explicit total input or cached tokens are invalid")
        if total_cached > total_input:
            _raise_invalid(label, "explicit total cached tokens exceed total input tokens")

    total_output = 0
    has_output = "total_output_tokens" in metadata
    if has_output:
        total_output = metadata.get("total_output_tokens")
        if not _is_non_negative_int(total_output):
            _raise_invalid(label, "explicit total output tokens are invalid")

    return total_input, total_cached, total_output, has_input, has_output


def _legacy_totals(metadata: dict[str, Any], *, allow_legacy: bool) -> tuple[int, int, int]:
    if not allow_legacy:
        return 0, 0, 0

    input_tokens = metadata.get("input_tokens")
    if metadata.get("input_tokens_source") == "provider" and _is_positive_int(input_tokens):
        cached_tokens = metadata.get("cached_tokens")
        if _is_non_negative_int(cached_tokens):
            cached_tokens = min(cached_tokens, input_tokens)
        else:
            cached_tokens = 0
        legacy_input = input_tokens
        legacy_cached = cached_tokens
    else:
        legacy_input = 0
        legacy_cached = 0

    output_tokens = metadata.get("output_tokens")
    legacy_output = output_tokens if _is_non_negative_int(output_tokens) else 0
    return legacy_input, legacy_cached, legacy_output


def _existing_reported_totals(
    metadata: dict[str, Any],
    *,
    total_input_tokens: int,
    total_cached_tokens: int,
) -> tuple[int, int] | None:
    reported_input = metadata.get("cache_reported_input_tokens")
    reported_cached = metadata.get("cache_reported_cached_tokens")
    if not _is_non_negative_int(reported_input) or not _is_non_negative_int(reported_cached):
        return None
    if reported_cached > reported_input or reported_input > total_input_tokens or reported_cached > total_cached_tokens:
        return None
    return reported_input, reported_cached


def _summary_trigger_tokens(metadata: dict[str, Any], threshold_percent: int | None) -> int | None:
    if threshold_percent is None:
        return None
    context_window_tokens = metadata.get("context_window_tokens")
    max_output_tokens = metadata.get("max_output_tokens")
    if not _is_positive_int(context_window_tokens) or not _is_non_negative_int(max_output_tokens):
        return None
    capacity = context_window_tokens - max_output_tokens - _SUMMARY_SAFETY_MARGIN_TOKENS
    if capacity <= 0:
        return None
    return max(1, capacity * threshold_percent // 100)


def _convert_metadata(
    metadata: dict[str, Any],
    *,
    detail_totals: tuple[int, int, int, int, int],
    threshold_percent: int | None,
    main_session: bool,
    label: str,
) -> dict[str, Any]:
    converted = dict(metadata)
    _normalize_input_source(converted)

    current_input, current_cached, current_output, has_current_totals, has_current_output = _read_explicit_totals(converted, label)
    allow_legacy = not main_session or converted.get("request_purpose") in {None, _MAIN_DIALOGUE}
    legacy_input, legacy_cached, legacy_output = _legacy_totals(converted, allow_legacy=allow_legacy)
    detail_input, detail_cached, detail_output, detail_reported_input, detail_reported_cached = detail_totals

    if has_current_totals:
        total_input = current_input
        total_cached = current_cached
    else:
        total_input = legacy_input
        total_cached = legacy_cached
    if has_current_output:
        total_output = current_output
    else:
        total_output = legacy_output
    if main_session:
        total_input = max(total_input, detail_input)
        total_cached = max(total_cached, detail_cached)
        total_output = max(total_output, detail_output)
    if not has_current_totals and total_cached > total_input:
        _raise_invalid(label, "converted total cached tokens exceed total input tokens")

    existing_reported = _existing_reported_totals(
        converted,
        total_input_tokens=total_input,
        total_cached_tokens=total_cached,
    )
    if existing_reported is not None:
        reported_input, reported_cached = existing_reported
    elif main_session:
        reported_input, reported_cached = detail_reported_input, detail_reported_cached
    else:
        reported_input, reported_cached = 0, 0

    converted["total_input_tokens"] = total_input
    converted["total_cached_tokens"] = total_cached
    converted["total_output_tokens"] = total_output
    converted["cache_reported_input_tokens"] = reported_input
    converted["cache_reported_cached_tokens"] = reported_cached
    if reported_input > 0:
        converted["cache_hit_rate"] = reported_cached / reported_input
    else:
        converted["cache_hit_rate"] = None
    if total_input > 0:
        converted["cache_coverage"] = reported_input / total_input
    else:
        converted["cache_coverage"] = None

    has_request_fields = "context_window_tokens" in converted and "max_output_tokens" in converted
    if main_session and has_request_fields:
        converted["summary_trigger_tokens"] = _summary_trigger_tokens(converted, threshold_percent)
    elif not main_session and has_request_fields:
        summary_trigger_tokens = converted.get("summary_trigger_tokens")
        if not _is_positive_int(summary_trigger_tokens):
            converted["summary_trigger_tokens"] = None

    return converted


def _reflect_tables(sync_connection) -> dict[str, Table]:
    table_names = set(inspect(sync_connection).get_table_names())
    metadata = MetaData()
    tables: dict[str, Table] = {}
    for table_name in _TABLE_NAMES:
        if table_name in table_names:
            tables[table_name] = Table(table_name, metadata, autoload_with=sync_connection, resolve_fks=False)
    return tables


def _has_columns(table: Table | None, names: tuple[str, ...]) -> bool:
    if table is None:
        return False
    for name in names:
        if name not in table.c:
            return False
    return True


async def _usage_totals(
    session: AsyncSession,
    detail: Table | None,
    *,
    session_id: Any,
    uid: Any,
) -> tuple[int, int, int, int, int]:
    required_columns = ("provider_request_id", "type", "uid", "session_id", "channel_key", "input_tokens", "cached_tokens", "output_tokens")
    if not _has_columns(detail, required_columns):
        return 0, 0, 0, 0, 0
    assert detail is not None

    selected_columns = [
        detail.c.provider_request_id,
        detail.c.channel_key,
        detail.c.input_tokens,
        detail.c.cached_tokens,
        detail.c.output_tokens,
    ]
    input_total = 0
    cached_total = 0
    output_total = 0
    reported_input_total = 0
    reported_cached_total = 0
    offset = 0
    while True:
        result = await session.execute(
            select(*selected_columns)
            .where(
                detail.c.session_id == session_id,
                detail.c.uid == uid,
                detail.c.type == _MAIN_DIALOGUE,
            )
            .order_by(detail.c.provider_request_id.asc())
            .offset(offset)
            .limit(_BATCH_SIZE)
        )
        rows = result.mappings().all()
        if not rows:
            break
        for row in rows:
            input_tokens = row["input_tokens"]
            cached_tokens = row["cached_tokens"]
            output_tokens = row["output_tokens"]
            channel_key = row["channel_key"]
            if _is_non_negative_int(input_tokens):
                input_total += input_tokens
            if _is_non_negative_int(output_tokens):
                output_total += output_tokens
            if _is_non_negative_int(input_tokens) and _is_non_negative_int(cached_tokens) and cached_tokens <= input_tokens:
                cached_total += cached_tokens
                if input_tokens > 0 and isinstance(channel_key, str) and channel_key.strip():
                    reported_input_total += input_tokens
                    reported_cached_total += cached_tokens
        offset += len(rows)
        if len(rows) < _BATCH_SIZE:
            break

    return input_total, cached_total, output_total, reported_input_total, reported_cached_total


async def _profile_threshold(
    session: AsyncSession,
    profile: Table | None,
    *,
    uid: Any,
    profile_override_id: Any,
    profile_id: Any,
    cache: dict[tuple[Any, int], int | None],
) -> int | None:
    if not _has_columns(profile, ("id", "configs")):
        return None
    selected_profile_id = profile_override_id if _is_positive_int(profile_override_id) else profile_id
    if not _is_positive_int(selected_profile_id):
        return None
    assert profile is not None

    cache_key = (uid, selected_profile_id)
    if cache_key in cache:
        return cache[cache_key]

    conditions = [profile.c.id == selected_profile_id]
    if "uid" in profile.c:
        conditions.append(profile.c.uid == uid)
    result = await session.execute(select(profile.c.configs).where(*conditions).limit(1))
    configs_value = result.scalar_one_or_none()
    configs = _optional_object(configs_value)
    threshold_percent: int | None = None
    if configs is not None:
        other = configs.get("other")
        if other is None:
            threshold_percent = 90
        elif isinstance(other, dict):
            if "context_summary_threshold_percent" not in other:
                threshold_percent = 90
            else:
                value = other.get("context_summary_threshold_percent")
                if _is_positive_int(value) and value <= 100:
                    threshold_percent = value

    cache[cache_key] = threshold_percent
    return threshold_percent


def _convert_metadata_value(value: Any, label: str) -> Any:
    if value is None:
        return None
    metadata = _decode_object(value, label)
    if metadata is None:
        return value
    return _convert_metadata(
        metadata,
        detail_totals=(0, 0, 0, 0, 0),
        threshold_percent=None,
        main_session=False,
        label=label,
    )


def _transform_nested(value: Any, label: str) -> Any:
    if isinstance(value, list):
        transformed_list = []
        for index, item in enumerate(value):
            transformed_list.append(_transform_nested(item, f"{label}[{index}]"))
        return transformed_list
    if not isinstance(value, dict):
        return value

    transformed = dict(value)
    for key, child in value.items():
        child_label = f"{label}.{key}"
        if key in _METADATA_KEYS:
            transformed[key] = _convert_metadata_value(child, child_label)
        elif isinstance(child, (dict, list)):
            transformed[key] = _transform_nested(child, child_label)
    return transformed


def _transform_event(value: Any, label: str) -> Any:
    value = _decode_container(value)
    if isinstance(value, dict) and value.get("type") == "llm_request_metadata":
        return _convert_metadata_value(value, label)
    return _transform_nested(value, label)


async def _migrate_sessions(
    session: AsyncSession,
    chat_session: Table,
    detail: Table | None,
    profile: Table | None,
) -> None:
    if not _has_columns(chat_session, ("session_id", "uid", "llm_request_metadata")):
        return

    selected_columns = [chat_session.c.session_id, chat_session.c.uid, chat_session.c.llm_request_metadata]
    for optional_name in ("profile_override_id", "profile_id"):
        if optional_name in chat_session.c:
            selected_columns.append(chat_session.c[optional_name])

    profile_cache: dict[tuple[Any, int], int | None] = {}
    offset = 0
    while True:
        result = await session.execute(select(*selected_columns).order_by(chat_session.c.session_id.asc()).offset(offset).limit(_BATCH_SIZE))
        rows = result.mappings().all()
        if not rows:
            return
        for row in rows:
            original_metadata = row["llm_request_metadata"]
            if original_metadata is None:
                continue
            session_id = row["session_id"]
            uid = row["uid"]
            metadata = _decode_object(original_metadata, f"chat_session {session_id!r} llm_request_metadata")
            if metadata is None:
                continue
            detail_totals = await _usage_totals(session, detail, session_id=session_id, uid=uid)
            threshold_percent = await _profile_threshold(
                session,
                profile,
                uid=uid,
                profile_override_id=row.get("profile_override_id"),
                profile_id=row.get("profile_id"),
                cache=profile_cache,
            )
            converted = _convert_metadata(
                metadata,
                detail_totals=detail_totals,
                threshold_percent=threshold_percent,
                main_session=True,
                label=f"chat_session {session_id!r} llm_request_metadata",
            )
            if not isinstance(original_metadata, dict) or converted != original_metadata:
                await session.execute(update(chat_session).where(chat_session.c.session_id == session_id, chat_session.c.uid == uid).values(llm_request_metadata=converted))
        offset += len(rows)
        if len(rows) < _BATCH_SIZE:
            return


async def _migrate_snapshot_table(
    session: AsyncSession,
    table: Table | None,
    *,
    id_column_name: str,
    value_column_name: str,
    event_root: bool,
) -> None:
    if not _has_columns(table, (id_column_name, value_column_name)):
        return
    assert table is not None
    id_column = table.c[id_column_name]
    value_column = table.c[value_column_name]
    offset = 0
    while True:
        result = await session.execute(select(id_column, value_column).order_by(id_column.asc()).offset(offset).limit(_BATCH_SIZE))
        rows = result.mappings().all()
        if not rows:
            return
        for row in rows:
            row_id = row[id_column_name]
            original_value = row[value_column_name]
            label = f"{table.name} {row_id!r}.{value_column_name}"
            transformed = _transform_event(original_value, label) if event_root else _transform_nested(_decode_container(original_value), label)
            if transformed != original_value:
                await session.execute(update(table).where(id_column == row_id).values(**{value_column_name: transformed}))
        offset += len(rows)
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
        await _migrate_sessions(
            session,
            tables.get("chat_session"),
            tables.get("provider_usage_detail"),
            tables.get("profile"),
        )
        await _migrate_snapshot_table(
            session,
            tables.get("session_reply_work_item"),
            id_column_name="id",
            value_column_name="execution_state",
            event_root=False,
        )
        await _migrate_snapshot_table(
            session,
            tables.get("session_reply_work_checkpoint"),
            id_column_name="work_id",
            value_column_name="checkpoint",
            event_root=False,
        )
        await _migrate_snapshot_table(
            session,
            tables.get("session_reply_stream_event"),
            id_column_name="id",
            value_column_name="event",
            event_root=True,
        )
        await _migrate_snapshot_table(
            session,
            tables.get("session_event"),
            id_column_name="id",
            value_column_name="event",
            event_root=True,
        )
    except BaseException:
        await session.rollback()
        raise
