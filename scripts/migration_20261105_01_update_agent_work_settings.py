from __future__ import annotations

import json
from typing import Any

from sqlalchemy import MetaData, Table, insert, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261105_01_update_agent_work_settings"

_BATCH_SIZE = 200
_OLD_CONCURRENCY_KEY = "session_reply_max_concurrency"
_NEW_CONCURRENCY_KEY = "agent_work_max_concurrency"
_SUBAGENT_CONCURRENCY_KEY = "subagent_max_concurrency"
_SCHEDULED_TASK_ORIGIN_KEY = "scheduled_task_origin"

_FOREGROUND = "FOREGROUND"
_SCHEDULED_TASK = "SCHEDULED_TASK"
_SUBAGENT = "SUBAGENT"

_WORK_TABLE = "session_reply_work_item"
_AUDIT_TABLE = "audit_record"
_MESSAGE_TABLE = "message"


def _reflect_table(sync_connection, table_name: str) -> Table | None:
    if table_name not in set(inspect(sync_connection).get_table_names()):
        return None
    return Table(table_name, MetaData(), autoload_with=sync_connection, resolve_fks=False)


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except (TypeError, ValueError):
            return None
    else:
        return None
    return parsed if parsed > 0 else None


def _upper(value: Any) -> str:
    return value.strip().upper() if isinstance(value, str) else ""


def _decode_mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return dict(value)
    if not isinstance(value, str):
        return None
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return None
    return dict(decoded) if isinstance(decoded, dict) else None


def _work_execution_state(work: dict[str, Any]) -> dict[str, Any]:
    state = _decode_mapping(work.get("execution_state"))
    if state is None:
        raise RuntimeError(f"{MIGRATION_ID}: work {work.get('id')!r} has invalid execution_state")
    return state


def _bounded_int(value: Any, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"{MIGRATION_ID}: {label} must be an integer between {minimum} and {maximum}")
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = int(value.strip())
        except (TypeError, ValueError):
            raise RuntimeError(f"{MIGRATION_ID}: {label} must be an integer between {minimum} and {maximum}")
    else:
        raise RuntimeError(f"{MIGRATION_ID}: {label} must be an integer between {minimum} and {maximum}")
    if not minimum <= parsed <= maximum:
        raise RuntimeError(f"{MIGRATION_ID}: {label} must be an integer between {minimum} and {maximum}")
    return parsed


async def _migrate_system_settings(session: AsyncSession, table: Table | None) -> None:
    if table is None:
        return
    required_columns = {"key", "value"}
    if not required_columns.issubset(table.c.keys()):
        raise RuntimeError(f"{MIGRATION_ID}: system_setting is missing key or value")

    result = await session.execute(select(table.c.key, table.c.value).where(table.c.key.in_([_OLD_CONCURRENCY_KEY, _NEW_CONCURRENCY_KEY])))
    rows = result.mappings().all()
    old_row = next((row for row in rows if row["key"] == _OLD_CONCURRENCY_KEY), None)
    new_row = next((row for row in rows if row["key"] == _NEW_CONCURRENCY_KEY), None)

    if new_row is not None:
        _bounded_int(
            new_row["value"],
            minimum=1,
            maximum=100,
            label=_NEW_CONCURRENCY_KEY,
        )
        if old_row is not None:
            await session.execute(table.delete().where(table.c.key == _OLD_CONCURRENCY_KEY))
        return

    if old_row is not None:
        _bounded_int(
            old_row["value"],
            minimum=1,
            maximum=100,
            label=_OLD_CONCURRENCY_KEY,
        )
        await session.execute(update(table).where(table.c.key == _OLD_CONCURRENCY_KEY).values(key=_NEW_CONCURRENCY_KEY))
        return

    await session.execute(insert(table).values(key=_NEW_CONCURRENCY_KEY, value="4"))


def _update_profile_configs(configs: dict[str, Any], profile_id: Any) -> dict[str, Any]:
    updated = dict(configs)
    raw_tool = configs.get("tool")
    if "tool" in configs and not isinstance(raw_tool, dict):
        raise RuntimeError(f"{MIGRATION_ID}: profile {profile_id} has invalid tool config type")
    tool = dict(raw_tool) if isinstance(raw_tool, dict) else {}

    nested_exists = _SUBAGENT_CONCURRENCY_KEY in tool
    flat_exists = _SUBAGENT_CONCURRENCY_KEY in configs
    if nested_exists:
        selected_value = _bounded_int(
            tool[_SUBAGENT_CONCURRENCY_KEY],
            minimum=1,
            maximum=20,
            label=f"profile {profile_id}.tool.{_SUBAGENT_CONCURRENCY_KEY}",
        )
    elif flat_exists:
        selected_value = _bounded_int(
            configs[_SUBAGENT_CONCURRENCY_KEY],
            minimum=1,
            maximum=20,
            label=f"profile {profile_id}.{_SUBAGENT_CONCURRENCY_KEY}",
        )
    else:
        selected_value = 4

    tool[_SUBAGENT_CONCURRENCY_KEY] = selected_value
    updated["tool"] = tool
    if flat_exists:
        del updated[_SUBAGENT_CONCURRENCY_KEY]
    return updated


async def _migrate_profiles(session: AsyncSession, table: Table | None) -> None:
    if table is None:
        return
    required_columns = {"id", "configs"}
    if not required_columns.issubset(table.c.keys()):
        raise RuntimeError(f"{MIGRATION_ID}: profile is missing id or configs")

    last_id = 0
    while True:
        rows = (await session.execute(select(table.c.id, table.c.configs).where(table.c.id > last_id).order_by(table.c.id.asc()).limit(_BATCH_SIZE))).mappings().all()
        if not rows:
            return

        for row in rows:
            profile_id = row["id"]
            configs = _decode_mapping(row["configs"])
            if configs is None:
                raise RuntimeError(f"{MIGRATION_ID}: profile {profile_id} has invalid configs JSON object")
            updated = _update_profile_configs(configs, profile_id)
            if updated != configs:
                await session.execute(update(table).where(table.c.id == profile_id).values(configs=updated))

        last_id = rows[-1]["id"]


def _same_work_owner(candidate: dict[str, Any], work: dict[str, Any]) -> bool:
    return candidate.get("uid") == work.get("uid") and candidate.get("session_id") == work.get("session_id") and candidate.get("profile_id") == work.get("profile_id")


def _same_audit_owner(audit: dict[str, Any], work: dict[str, Any]) -> bool:
    return audit.get("uid") == work.get("uid") and audit.get("session_id") == work.get("session_id")


async def _load_row(
    session: AsyncSession,
    table: Table | None,
    row_id: int,
    cache: dict[int, dict[str, Any] | None],
) -> dict[str, Any] | None:
    if table is None:
        return None
    if row_id in cache:
        return cache[row_id]
    result = await session.execute(select(table).where(table.c.id == row_id))
    row = result.mappings().first()
    loaded = dict(row) if row is not None else None
    cache[row_id] = loaded
    return loaded


async def _approved_audit_for_decision(
    session: AsyncSession,
    table: Table | None,
    *,
    uid: Any,
    session_id: Any,
    decision_message_id: int,
    cache: dict[tuple[Any, Any, int], dict[str, Any] | None],
) -> dict[str, Any] | None:
    if table is None or not {"uid", "session_id", "decision_message_id", "decision"}.issubset(table.c.keys()):
        return None
    cache_key = (uid, session_id, decision_message_id)
    if cache_key in cache:
        return cache[cache_key]
    result = await session.execute(
        select(table)
        .where(
            table.c.uid == uid,
            table.c.session_id == session_id,
            table.c.decision_message_id == decision_message_id,
        )
        .order_by(table.c.id.asc())
    )
    approved: list[dict[str, Any]] = []
    for row in result.mappings().all():
        audit = dict(row)
        if _upper(audit.get("decision")) == "APPROVE":
            approved.append(audit)
    if len(approved) > 1:
        raise RuntimeError(f"{MIGRATION_ID}: decision message {decision_message_id} has multiple approved audit records")
    selected = approved[0] if approved else None
    cache[cache_key] = selected
    return selected


async def _resolve_work_category(
    session: AsyncSession,
    work_table: Table,
    audit_table: Table | None,
    message_table: Table | None,
    work: dict[str, Any],
    *,
    work_cache: dict[int, dict[str, Any] | None],
    audit_cache: dict[int, dict[str, Any] | None],
    message_cache: dict[int, dict[str, Any] | None],
    decision_cache: dict[tuple[Any, Any, int], dict[str, Any] | None],
    visited_work_ids: set[int] | None = None,
    visited_audit_ids: set[int] | None = None,
) -> str | None:
    work_id = _positive_int(work.get("id"))
    if work_id is None:
        return None
    visited_work_ids = set() if visited_work_ids is None else set(visited_work_ids)
    visited_audit_ids = set() if visited_audit_ids is None else set(visited_audit_ids)
    if work_id in visited_work_ids:
        return None
    visited_work_ids.add(work_id)

    work_type = _upper(work.get("work_type"))
    source_type = _upper(work.get("source_type"))
    if work_type == "AGENT_WORK":
        if source_type == "PARENT_WORK":
            return _SUBAGENT
        return _SCHEDULED_TASK
    if work_type != "CONFIRMED_TOOL_EXECUTION" or source_type != "AUDIT_RECORD":
        return _FOREGROUND

    state = _work_execution_state(work)
    persisted_category = _upper(state.get("work_category"))
    if persisted_category in {_SCHEDULED_TASK, _SUBAGENT}:
        return persisted_category

    if state.get(_SCHEDULED_TASK_ORIGIN_KEY) is True:
        return _SCHEDULED_TASK
    if audit_table is None:
        return None

    decision_id = _positive_int(state.get("decision_message_id"))
    source_audit = None
    if decision_id is not None:
        source_audit = await _approved_audit_for_decision(
            session,
            audit_table,
            uid=work.get("uid"),
            session_id=work.get("session_id"),
            decision_message_id=decision_id,
            cache=decision_cache,
        )
        if source_audit is not None and not _same_audit_owner(source_audit, work):
            return None
    if source_audit is None:
        audit_id = _positive_int(work.get("source_id"))
        if audit_id is None or audit_id in visited_audit_ids:
            return None
        source_audit = await _load_row(session, audit_table, audit_id, audit_cache)
        if source_audit is None or not _same_audit_owner(source_audit, work):
            return None

    source_audit_id = _positive_int(source_audit.get("id"))
    if source_audit_id is None or source_audit_id in visited_audit_ids:
        return None
    visited_audit_ids.add(source_audit_id)

    categories: set[str] = set()
    source_work_id = _positive_int(source_audit.get("source_work_id"))
    if source_work_id is not None and source_work_id != work_id:
        source_work = await _load_row(session, work_table, source_work_id, work_cache)
        if source_work is not None and _same_work_owner(source_work, work):
            source_category = await _resolve_work_category(
                session,
                work_table,
                audit_table,
                message_table,
                source_work,
                work_cache=work_cache,
                audit_cache=audit_cache,
                message_cache=message_cache,
                decision_cache=decision_cache,
                visited_work_ids=visited_work_ids,
                visited_audit_ids=visited_audit_ids,
            )
            if source_category is not None:
                categories.add(source_category)

    source_message_id = _positive_int(source_audit.get("source_assistant_message_id"))
    if source_message_id is not None and message_table is not None:
        source_message = await _load_row(session, message_table, source_message_id, message_cache)
        if source_message is not None and _same_work_owner(source_message, work) and _upper(source_message.get("role")) == "ASSISTANT" and _upper(source_message.get("type")) == "TOOL_CALL":
            message_work_id = _positive_int(source_message.get("work_id"))
            if message_work_id is not None and message_work_id != work_id:
                source_work = await _load_row(session, work_table, message_work_id, work_cache)
                if source_work is not None and _same_work_owner(source_work, work):
                    source_category = await _resolve_work_category(
                        session,
                        work_table,
                        audit_table,
                        message_table,
                        source_work,
                        work_cache=work_cache,
                        audit_cache=audit_cache,
                        message_cache=message_cache,
                        decision_cache=decision_cache,
                        visited_work_ids=visited_work_ids,
                        visited_audit_ids=visited_audit_ids,
                    )
                    if source_category is not None:
                        categories.add(source_category)

    if len(categories) > 1:
        raise RuntimeError(f"{MIGRATION_ID}: confirmed work {work_id} has conflicting source ownership")
    return next(iter(categories)) if categories else None


async def _migrate_work_items(
    session: AsyncSession,
    work_table: Table,
    audit_table: Table | None,
    message_table: Table | None,
) -> None:
    work_cache: dict[int, dict[str, Any] | None] = {}
    audit_cache: dict[int, dict[str, Any] | None] = {}
    message_cache: dict[int, dict[str, Any] | None] = {}
    decision_cache: dict[tuple[Any, Any, int], dict[str, Any] | None] = {}
    last_id = 0
    while True:
        rows = (await session.execute(select(work_table).where(work_table.c.id > last_id).order_by(work_table.c.id.asc()).limit(_BATCH_SIZE))).mappings().all()
        if not rows:
            return

        page: list[dict[str, Any]] = []
        for row in rows:
            work = dict(row)
            work_cache[work["id"]] = work
            page.append(work)

        for work in page:
            state = _work_execution_state(work)
            work["execution_state"] = state
            target_category = (
                await _resolve_work_category(
                    session,
                    work_table,
                    audit_table,
                    message_table,
                    work,
                    work_cache=work_cache,
                    audit_cache=audit_cache,
                    message_cache=message_cache,
                    decision_cache=decision_cache,
                )
                or _FOREGROUND
            )
            migrated_state = dict(state)
            migrated_state.pop(_SCHEDULED_TASK_ORIGIN_KEY, None)
            migrated_state["work_category"] = target_category.lower()
            if migrated_state != state:
                await session.execute(update(work_table).where(work_table.c.id == work["id"]).values(execution_state=migrated_state))
                work["execution_state"] = migrated_state

        last_id = page[-1]["id"]


async def migrate(session: AsyncSession) -> None:
    try:
        await ensure_sqlite_outer_transaction(session)
        dialect_name = session.get_bind().dialect.name
        if dialect_name not in {"sqlite", "mysql"}:
            raise RuntimeError(f"{MIGRATION_ID}: unsupported database dialect: {dialect_name}")

        connection = await session.connection()
        work_table = await connection.run_sync(lambda sync_connection: _reflect_table(sync_connection, _WORK_TABLE))
        audit_table = await connection.run_sync(lambda sync_connection: _reflect_table(sync_connection, _AUDIT_TABLE))
        message_table = await connection.run_sync(lambda sync_connection: _reflect_table(sync_connection, _MESSAGE_TABLE))
        system_table = await connection.run_sync(lambda sync_connection: _reflect_table(sync_connection, "system_setting"))
        profile_table = await connection.run_sync(lambda sync_connection: _reflect_table(sync_connection, "profile"))
        await _migrate_system_settings(session, system_table)
        await _migrate_profiles(session, profile_table)
        if work_table is not None:
            await _migrate_work_items(session, work_table, audit_table, message_table)
    except BaseException:
        await session.rollback()
        raise
