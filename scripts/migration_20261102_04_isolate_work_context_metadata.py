from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, Table, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from scripts import migration_20261102_02_repair_confirmed_work_message_ownership as ownership

MIGRATION_ID = "20261102_04_isolate_work_context_metadata"

_WORK = ownership._WORK
_AUDIT = ownership._AUDIT
_MESSAGE = ownership._MESSAGE
_EVENT = ownership._EVENT
_BATCH_SIZE = ownership._BATCH_SIZE
_EVENT_TABLE = ownership._EVENT_TABLE
_STREAM_TABLE = ownership._STREAM_TABLE

_METADATA_FIELDS = (
    "request_purpose",
    "total_input_tokens",
    "total_cached_tokens",
    "total_output_tokens",
    "cache_hit_rate",
    "work_id",
    "work_sequence_no",
    "event_sequence_no",
)
_EVENT_ENVELOPE_FIELDS = {
    "type",
    "session_id",
    "uid",
    "work_id",
    "work_sequence_no",
    "event_sequence_no",
    "request_id",
    "request_ids",
    "event_id",
    "source",
    "profile_id",
    "response_id",
    "message_id",
    "task_id",
    "background_task_id",
    "trigger_message_id",
}
_INDEPENDENT_SOURCES = {"scheduled_task", "agent_work"}

_STREAM_EVENT = Table(
    _STREAM_TABLE,
    MetaData(),
    Column("id", Integer),
    Column("work_id", Integer),
    Column("event", JSON),
)


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _lower(value: Any) -> str:
    return value.lower() if isinstance(value, str) else str(value or "").lower()


async def _all_works(session: AsyncSession) -> list[dict[str, Any]]:
    works: list[dict[str, Any]] = []
    last_id = 0
    while True:
        rows = (await session.execute(select(_WORK).where(_WORK.c.id > last_id).order_by(_WORK.c.id.asc()).limit(_BATCH_SIZE))).mappings().all()
        if not rows:
            return works
        for row in rows:
            work_id = ownership._id(row["id"])
            if work_id is None or not row["uid"] or not row["session_id"]:
                continue
            work = dict(row)
            work["id"] = work_id
            works.append(work)
        last_id = ownership._id(rows[-1]["id"]) or last_id


async def _confirmed_work_has_independent_source(
    session: AsyncSession,
    work: dict[str, Any],
    *,
    audit_cache: dict[int, Any],
    message_cache: dict[int, Any],
) -> bool:
    if _lower(work.get("work_type")) != "confirmed_tool_execution" or _lower(work.get("source_type")) != "audit_record":
        return False
    audit_id = ownership._id(work.get("source_id"))
    if audit_id is None:
        return False
    await ownership._load_rows(session, _AUDIT, {audit_id}, audit_cache)
    audit = audit_cache.get(audit_id)
    if audit is None or not ownership._same_audit_owner(audit, work):
        return False
    message_id = ownership._id(audit.get("source_assistant_message_id"))
    if message_id is None:
        return False
    await ownership._load_rows(session, _MESSAGE, {message_id}, message_cache)
    message = message_cache.get(message_id)
    return message is not None and ownership._valid_source_message(message, work) and ownership._id(message.get("work_id")) is not None


async def _build_work_decisions(session: AsyncSession, works: list[dict[str, Any]]) -> dict[int, tuple[str, str, bool]]:
    audit_cache: dict[int, Any] = {}
    message_cache: dict[int, Any] = {}
    decisions: dict[int, tuple[str, str, bool]] = {}
    for work in works:
        state = _dict(work.get("execution_state"))
        independent = (
            _lower(work.get("work_type")) in {"agent_work", "background_task_resume", "background_tool_summary"}
            or isinstance(state.get("frozen_context_messages"), list)
            or await _confirmed_work_has_independent_source(
                session,
                work,
                audit_cache=audit_cache,
                message_cache=message_cache,
            )
        )
        decisions[work["id"]] = (work["uid"], work["session_id"], independent)
    return decisions


def _trim_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    trimmed = {}
    for field in _METADATA_FIELDS:
        if field in metadata:
            trimmed[field] = metadata[field]
    return trimmed


def _sanitize_event(event: Any) -> dict[str, Any] | None:
    if not isinstance(event, dict):
        return None
    sanitized = dict(event)
    metadata = event.get("llm_request_metadata")
    if isinstance(metadata, dict):
        trimmed = _trim_metadata(metadata)
        if trimmed != metadata:
            sanitized["llm_request_metadata"] = trimmed

    response = event.get("response")
    if isinstance(response, dict):
        response_metadata = response.get("llm_request_metadata")
        if isinstance(response_metadata, dict):
            trimmed = _trim_metadata(response_metadata)
            if trimmed != response_metadata:
                sanitized["response"] = {**response, "llm_request_metadata": trimmed}

    if _lower(event.get("type")) == "llm_request_metadata":
        sanitized = {}
        for field, value in event.items():
            if field in _EVENT_ENVELOPE_FIELDS or field in _METADATA_FIELDS:
                sanitized[field] = value
    return sanitized if sanitized != event else None


def _event_matches_work(
    row: Any,
    event: dict[str, Any],
    decisions: dict[int, tuple[str, str, bool]],
) -> bool:
    if event.get("uid") is not None and event.get("uid") != row["uid"]:
        return False
    if event.get("session_id") is not None and event.get("session_id") != row["session_id"]:
        return False

    work_id = ownership._id(event.get("work_id"))
    decision = decisions.get(work_id) if work_id is not None else None
    if decision is not None:
        return decision[0] == row["uid"] and decision[1] == row["session_id"] and decision[2]
    return _lower(event.get("source")) in _INDEPENDENT_SOURCES


async def _repair_work_responses(
    session: AsyncSession,
    works: list[dict[str, Any]],
    decisions: dict[int, tuple[str, str, bool]],
) -> None:
    for work in works:
        decision = decisions.get(work["id"])
        if decision is None or not decision[2]:
            continue
        state = work.get("execution_state")
        if not isinstance(state, dict):
            continue
        response = state.get("response")
        if not isinstance(response, dict) or not isinstance(response.get("llm_request_metadata"), dict):
            continue
        metadata = response["llm_request_metadata"]
        trimmed = _trim_metadata(metadata)
        if trimmed == metadata:
            continue
        new_state = {**state, "response": {**response, "llm_request_metadata": trimmed}}
        await session.execute(update(_WORK).where(_WORK.c.id == work["id"]).values(execution_state=new_state))


async def _repair_session_events(
    session: AsyncSession,
    columns: set[str],
    decisions: dict[int, tuple[str, str, bool]],
) -> None:
    if not {"id", "uid", "session_id", "event"}.issubset(columns):
        return
    last_id = 0
    while True:
        rows = (await session.execute(select(_EVENT.c.id, _EVENT.c.uid, _EVENT.c.session_id, _EVENT.c.event).where(_EVENT.c.id > last_id).order_by(_EVENT.c.id.asc()).limit(_BATCH_SIZE))).mappings().all()
        if not rows:
            return
        updates: list[tuple[int, str, str, dict[str, Any]]] = []
        for row in rows:
            event = row["event"]
            if not isinstance(event, dict) or not _event_matches_work(row, event, decisions):
                continue
            sanitized = _sanitize_event(event)
            if sanitized is not None:
                updates.append((ownership._id(row["id"]) or 0, row["uid"], row["session_id"], sanitized))
        for event_id, uid, session_id, event in updates:
            if event_id > 0:
                await session.execute(update(_EVENT).where(_EVENT.c.id == event_id, _EVENT.c.uid == uid, _EVENT.c.session_id == session_id).values(event=event))
        last_id = ownership._id(rows[-1]["id"]) or last_id


async def _repair_stream_events(
    session: AsyncSession,
    columns: set[str],
    decisions: dict[int, tuple[str, str, bool]],
) -> None:
    if not {"id", "work_id", "event"}.issubset(columns):
        return
    last_id = 0
    while True:
        rows = (await session.execute(select(_STREAM_EVENT.c.id, _STREAM_EVENT.c.work_id, _STREAM_EVENT.c.event).where(_STREAM_EVENT.c.id > last_id).order_by(_STREAM_EVENT.c.id.asc()).limit(_BATCH_SIZE))).mappings().all()
        if not rows:
            return
        for row in rows:
            work_id = ownership._id(row["work_id"])
            decision = decisions.get(work_id) if work_id is not None else None
            if decision is None or not decision[2] or not isinstance(row["event"], dict):
                continue
            event = row["event"]
            if event.get("uid") is not None and event.get("uid") != decision[0]:
                continue
            if event.get("session_id") is not None and event.get("session_id") != decision[1]:
                continue
            if event.get("work_id") is not None and ownership._id(event.get("work_id")) != work_id:
                continue
            sanitized = _sanitize_event(event)
            if sanitized is not None:
                await session.execute(update(_STREAM_EVENT).where(_STREAM_EVENT.c.id == row["id"]).values(event=sanitized))
        last_id = ownership._id(rows[-1]["id"]) or last_id


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    tables = await connection.run_sync(ownership._table_columns)
    if any(not set(table.c.keys()).issubset(tables.get(table_name, set())) for table_name, table in ownership._REQUIRED_TABLES.items()):
        return

    works = await _all_works(session)
    decisions = await _build_work_decisions(session, works)
    await _repair_work_responses(session, works, decisions)
    await _repair_session_events(session, tables.get(_EVENT_TABLE, set()), decisions)
    await _repair_stream_events(session, tables.get(_STREAM_TABLE, set()), decisions)
