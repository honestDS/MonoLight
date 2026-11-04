from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import MetaData, Table, insert, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.time import get_local_time
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261103_03_consolidate_provider_usage"

_DETAIL_TABLE = "provider_usage_detail"
_DAILY_TABLE = "provider_usage_daily"
_WORK_TABLE = "session_reply_work_item"
_AUDIT_TABLE = "audit_record"
_SOURCE_TABLE = "session_reply_provider_usage"
_BATCH_SIZE = 200


def _reflect_tables(sync_connection) -> dict[str, Table]:
    table_names = set(inspect(sync_connection).get_table_names())
    metadata = MetaData()
    tables: dict[str, Table] = {}
    for table_name in (_DETAIL_TABLE, _DAILY_TABLE, _WORK_TABLE, _AUDIT_TABLE, _SOURCE_TABLE):
        if table_name in table_names:
            tables[table_name] = Table(table_name, metadata, autoload_with=sync_connection, resolve_fks=False)
    return tables


def _lower(value: Any) -> str:
    if not isinstance(value, str):
        value = getattr(value, "value", None)
    return value.strip().lower() if isinstance(value, str) else ""


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


async def _is_scheduled_work(
    session: AsyncSession,
    work: Mapping[str, Any] | None,
    work_table: Table,
    audit: Table,
) -> bool:
    visited_work_ids: set[int] = set()
    while work is not None:
        work_id = work.get("id")
        if not isinstance(work_id, int) or isinstance(work_id, bool) or work_id <= 0 or work_id in visited_work_ids:
            return False
        visited_work_ids.add(work_id)
        work_type = _lower(work.get("work_type"))
        source_type = _lower(work.get("source_type"))
        if work_type == "agent_work":
            return source_type == "scheduled_task_run"
        if work_type != "confirmed_tool_execution" or source_type != "audit_record":
            return False
        if _as_dict(work.get("execution_state")).get("scheduled_task_origin") is True:
            return True
        source_id = work.get("source_id")
        if isinstance(source_id, bool) or not isinstance(source_id, (int, str)):
            return False
        try:
            audit_id = int(source_id)
        except (TypeError, ValueError):
            return False
        if audit_id <= 0:
            return False

        result = await session.execute(
            select(
                work_table.c.id,
                work_table.c.uid,
                work_table.c.session_id,
                work_table.c.profile_id,
                work_table.c.work_type,
                work_table.c.source_type,
                work_table.c.source_id,
                work_table.c.execution_state,
            )
            .select_from(work_table.join(audit, audit.c.source_work_id == work_table.c.id))
            .where(
                audit.c.id == audit_id,
                audit.c.uid == work.get("uid"),
                audit.c.session_id == work.get("session_id"),
                work_table.c.uid == work.get("uid"),
                work_table.c.session_id == work.get("session_id"),
                work_table.c.profile_id == work.get("profile_id"),
            )
        )
        work = result.mappings().first()
    return False


def _utc_naive(value: Any, *, local_timezone, label: str) -> datetime:
    if value is None:
        raise RuntimeError(f"{MIGRATION_ID}: missing trusted datetime for {label}")
    if isinstance(value, str):
        text_value = value.strip()
        if text_value.endswith("Z"):
            text_value = f"{text_value[:-1]}+00:00"
        try:
            value = datetime.fromisoformat(text_value)
        except ValueError as error:
            raise RuntimeError(f"{MIGRATION_ID}: invalid datetime for {label}: {value!r}") from error
    if not isinstance(value, datetime):
        raise RuntimeError(f"{MIGRATION_ID}: invalid datetime for {label}: {value!r}")
    if value.tzinfo is None or value.utcoffset() is None:
        if local_timezone is None:
            raise RuntimeError(f"{MIGRATION_ID}: configured local timezone is unavailable")
        value = value.replace(tzinfo=local_timezone)
    try:
        return value.astimezone(UTC).replace(tzinfo=None)
    except (OverflowError, ValueError) as error:
        raise RuntimeError(f"{MIGRATION_ID}: invalid datetime for {label}: {value!r}") from error


def _bucket_key(usage_date: date, usage_type: str) -> str:
    dimensions = [usage_date.isoformat(), usage_type, None, None, None, None, None, None]
    serialized = json.dumps(dimensions, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


async def _write_daily(session: AsyncSession, daily: Table, detail: Mapping[str, Any], usage_date: date) -> None:
    values = {
        "bucket_key": _bucket_key(usage_date, detail["type"]),
        "usage_date": usage_date,
        "type": detail["type"],
        "channel_key": None,
        "channel_id": None,
        "channel_name": None,
        "model_id": None,
        "response_model_id": None,
        "protocol": None,
        "input_tokens": detail["input_tokens"],
        "cached_tokens": detail["cached_tokens"],
        "output_tokens": detail["output_tokens"],
        "image_count": 0,
        "rerank_search_units": 0,
        "request_count": 1,
        "input_tokens_reported_count": 1,
        "cached_tokens_reported_count": 1,
        "output_tokens_reported_count": 1,
        "image_count_reported_count": 0,
        "rerank_search_units_reported_count": 0,
    }
    result = await session.execute(select(daily).where(daily.c.bucket_key == values["bucket_key"]))
    existing = result.mappings().first()
    if existing is None:
        await session.execute(insert(daily).values(**values))
        return

    fields = (
        "input_tokens",
        "cached_tokens",
        "output_tokens",
        "image_count",
        "rerank_search_units",
        "request_count",
        "input_tokens_reported_count",
        "cached_tokens_reported_count",
        "output_tokens_reported_count",
        "image_count_reported_count",
        "rerank_search_units_reported_count",
    )
    await session.execute(update(daily).where(daily.c.bucket_key == values["bucket_key"]).values(**{field: daily.c[field] + values[field] for field in fields}))


async def _migrate_source(
    session: AsyncSession,
    source: Table,
    detail: Table,
    daily: Table,
    work: Table,
    audit: Table,
    local_timezone,
) -> None:
    source_created_at = source.c.get("created_at")
    last_id = 0
    while True:
        columns = [
            source.c.id,
            source.c.provider_request_id,
            source.c.work_id,
            source.c.session_id,
            source.c.uid,
            source.c.input_tokens,
            source.c.cached_tokens,
            source.c.request_purpose,
            source.c.output_tokens,
        ]
        if source_created_at is not None:
            columns.append(source_created_at)
        result = await session.execute(select(*columns).where(source.c.id > last_id).order_by(source.c.id.asc()).limit(_BATCH_SIZE))
        rows = result.mappings().all()
        if not rows:
            return

        work_ids = {row["work_id"] for row in rows}
        result = await session.execute(
            select(
                work.c.id,
                work.c.uid,
                work.c.session_id,
                work.c.profile_id,
                work.c.source_id,
                work.c.source_type,
                work.c.work_type,
                work.c.execution_state,
                work.c.created_at,
            ).where(work.c.id.in_(sorted(work_ids)))
        )
        work_by_id = {row["id"]: row for row in result.mappings().all()}
        scheduled_by_work_id: dict[int, bool] = {}

        for row in rows:
            work_id = row["work_id"]
            work_row = work_by_id.get(work_id)
            usage_type = _lower(row["request_purpose"])
            if usage_type not in {
                "main_dialogue",
                "background_summary",
                "scheduled_summary",
                "scheduled_task",
                "audit_auxiliary",
                "confirmed_auxiliary",
                "legacy_unknown",
            }:
                usage_type = "legacy_unknown"
            if usage_type == "main_dialogue" and work_row is not None:
                is_scheduled = scheduled_by_work_id.get(work_id)
                if is_scheduled is None:
                    is_scheduled = await _is_scheduled_work(session, work_row, work, audit)
                    scheduled_by_work_id[work_id] = is_scheduled
                if is_scheduled:
                    usage_type = "scheduled_task"

            detail_values = {
                "provider_request_id": row["provider_request_id"],
                "type": usage_type,
                "channel_key": None,
                "channel_id": None,
                "channel_name": None,
                "model_id": None,
                "response_model_id": None,
                "protocol": None,
                "uid": row["uid"],
                "session_id": row["session_id"],
                "work_id": row["work_id"],
                "input_tokens": row["input_tokens"],
                "cached_tokens": row["cached_tokens"],
                "output_tokens": row["output_tokens"],
                "image_count": None,
                "rerank_search_units": None,
            }
            result = await session.execute(select(detail).where(detail.c.provider_request_id == row["provider_request_id"]))
            existing = result.mappings().first()
            if existing is not None:
                existing_identity = (
                    existing["type"],
                    existing["work_id"],
                    existing["session_id"],
                    existing["uid"],
                    existing["input_tokens"],
                    existing["cached_tokens"],
                    existing["output_tokens"],
                )
                incoming_identity = (
                    detail_values["type"],
                    detail_values["work_id"],
                    detail_values["session_id"],
                    detail_values["uid"],
                    detail_values["input_tokens"],
                    detail_values["cached_tokens"],
                    detail_values["output_tokens"],
                )
                if existing_identity != incoming_identity:
                    raise RuntimeError(f"{MIGRATION_ID}: provider request {row['provider_request_id']!r} has conflicting ownership or token totals")
                continue

            created_at = row.get("created_at") if source_created_at is not None else None
            if created_at is None and work_row is not None:
                created_at = work_row["created_at"]
            detail_values["created_at"] = _utc_naive(
                created_at,
                local_timezone=local_timezone,
                label=f"{_SOURCE_TABLE} row {row['id']}.created_at",
            )
            await session.execute(insert(detail).values(**detail_values))
            await _write_daily(session, daily, detail_values, detail_values["created_at"].date())

        last_id = rows[-1]["id"]


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)
    try:
        dialect_name = session.get_bind().dialect.name
        if dialect_name not in {"sqlite", "mysql"}:
            raise RuntimeError(f"{MIGRATION_ID}: unsupported database dialect: {dialect_name}")

        connection = await session.connection()
        tables = await connection.run_sync(_reflect_tables)
        source = tables.get(_SOURCE_TABLE)
        if source is None:
            return

        detail = tables.get(_DETAIL_TABLE)
        daily = tables.get(_DAILY_TABLE)
        work = tables.get(_WORK_TABLE)
        audit = tables.get(_AUDIT_TABLE)
        if detail is None or daily is None or work is None or audit is None:
            raise RuntimeError(f"{MIGRATION_ID}: provider usage target, work, or audit tables are missing")

        await _migrate_source(
            session,
            source,
            detail,
            daily,
            work,
            audit,
            get_local_time().tzinfo,
        )
    except BaseException:
        await session.rollback()
        raise
