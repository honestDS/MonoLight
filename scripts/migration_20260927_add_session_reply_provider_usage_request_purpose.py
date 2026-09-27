import json
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260927_add_session_reply_provider_usage_request_purpose"

USAGE_TABLE = "session_reply_provider_usage"
WORK_TABLE = "session_reply_work_item"
PURPOSE_COLUMN = "request_purpose"
LEGACY_BOOLEAN_COLUMN = "cache_metrics_included"
PURPOSE_INDEX = "ix_session_reply_provider_usage_request_purpose"

PURPOSE_MAIN_DIALOGUE = "main_dialogue"
PURPOSE_BACKGROUND_SUMMARY = "background_summary"
PURPOSE_SCHEDULED_SUMMARY = "scheduled_summary"
PURPOSE_AUDIT_AUXILIARY = "audit_auxiliary"
PURPOSE_CONFIRMED_AUXILIARY = "confirmed_auxiliary"
PURPOSE_LEGACY_UNKNOWN = "legacy_unknown"


def _decode_execution_state(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _classify_legacy_purpose(
    *,
    work_type: str | None,
    execution_state: Any,
    legacy_included: Any,
) -> str:
    if work_type == "background_tool_summary":
        return PURPOSE_BACKGROUND_SUMMARY
    if work_type == "scheduled_task_summary":
        return PURPOSE_SCHEDULED_SUMMARY
    if work_type == "foreground_reply":
        state = _decode_execution_state(execution_state)
        return PURPOSE_AUDIT_AUXILIARY if bool(state.get("audit_decision_response")) else PURPOSE_MAIN_DIALOGUE
    if work_type == "confirmed_tool_execution" and legacy_included in (False, 0):
        return PURPOSE_CONFIRMED_AUXILIARY
    return PURPOSE_LEGACY_UNKNOWN


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()

    def inspect_schema(sync_connection) -> tuple[set[str] | None, set[str], set[str]]:
        inspector = inspect(sync_connection)
        table_names = set(inspector.get_table_names())
        if USAGE_TABLE not in table_names:
            return None, set(), set()

        usage_columns = {str(column["name"]) for column in inspector.get_columns(USAGE_TABLE)}
        work_columns = {str(column["name"]) for column in inspector.get_columns(WORK_TABLE)} if WORK_TABLE in table_names else set()
        usage_indexes = {str(index["name"]) for index in inspector.get_indexes(USAGE_TABLE)}
        return usage_columns, work_columns, usage_indexes

    usage_columns, work_columns, usage_indexes = await connection.run_sync(inspect_schema)
    if usage_columns is None:
        return

    if PURPOSE_COLUMN not in usage_columns:
        await session.execute(text(f"ALTER TABLE {USAGE_TABLE} ADD COLUMN {PURPOSE_COLUMN} VARCHAR(40) NOT NULL DEFAULT '{PURPOSE_LEGACY_UNKNOWN}'"))
        usage_columns.add(PURPOSE_COLUMN)

    if {"id", "work_id"} <= usage_columns and {"id", "work_type", "execution_state"} <= work_columns:
        legacy_column_sql = f"u.{LEGACY_BOOLEAN_COLUMN}" if LEGACY_BOOLEAN_COLUMN in usage_columns else "NULL"
        rows = (
            await session.execute(
                text(f"SELECT u.id AS usage_id, w.work_type AS work_type, w.execution_state AS execution_state, {legacy_column_sql} AS legacy_included FROM {USAGE_TABLE} u JOIN {WORK_TABLE} w ON w.id = u.work_id WHERE u.{PURPOSE_COLUMN} = :legacy_unknown"),
                {"legacy_unknown": PURPOSE_LEGACY_UNKNOWN},
            )
        ).mappings()

        for row in rows:
            purpose = _classify_legacy_purpose(
                work_type=row["work_type"],
                execution_state=row["execution_state"],
                legacy_included=row["legacy_included"],
            )
            await session.execute(
                text(f"UPDATE {USAGE_TABLE} SET {PURPOSE_COLUMN} = :purpose WHERE id = :usage_id"),
                {"purpose": purpose, "usage_id": row["usage_id"]},
            )

    if PURPOSE_INDEX not in usage_indexes:
        await session.execute(text(f"CREATE INDEX {PURPOSE_INDEX} ON {USAGE_TABLE} ({PURPOSE_COLUMN})"))
