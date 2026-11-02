from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, delete, func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261102_02_repair_confirmed_work_message_ownership"

_WORK_TABLE = "session_reply_work_item"
_AUDIT_TABLE = "audit_record"
_MESSAGE_TABLE = "message"
_SESSION_TABLE = "chat_session"
_EVENT_TABLE = "session_event"
_STREAM_TABLE = "session_reply_stream_event"
_BATCH_SIZE = 200

_ACTIVE_STATUSES = {"ready_for_llm", "running", "waiting_external_work"}
_INTERNAL_EVENT_TYPES = {
    "tool_results_update",
    "tool_call",
    "tool_result",
    "tool_start",
    "tool_end",
    "reasoning",
    "content",
    "turn_end",
    "input_dequeued",
    "llm_request_metadata",
}
_FINAL_EVENT_TYPES = {"proactive_reply", "proactive_reply_error", "done"}

_METADATA = MetaData()
_WORK = Table(
    _WORK_TABLE,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("profile_id", Integer),
    Column("work_type", String(40)),
    Column("source_type", String(40)),
    Column("source_id", String(100)),
    Column("status", String(30)),
    Column("execution_state", JSON),
    Column("result_message_id", Integer),
)
_AUDIT = Table(
    _AUDIT_TABLE,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("source_work_id", Integer),
    Column("source_assistant_message_id", Integer),
    Column("decision_message_id", Integer),
)
_MESSAGE = Table(
    _MESSAGE_TABLE,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("profile_id", Integer),
    Column("role", String(20)),
    Column("type", String(40)),
    Column("work_id", Integer),
    Column("created_at", DateTime(timezone=True)),
)
_SESSION = Table(
    _SESSION_TABLE,
    _METADATA,
    Column("session_id", String(100)),
    Column("uid", String(100)),
    Column("latest_message_id", Integer),
    Column("last_message_at", DateTime(timezone=True)),
    Column("context_content_revision", Integer),
)
_EVENT = Table(
    _EVENT_TABLE,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("event", JSON),
)
_STREAM = Table(_STREAM_TABLE, _METADATA, Column("work_id", Integer))

_REQUIRED_TABLES = {
    _WORK_TABLE: _WORK,
    _AUDIT_TABLE: _AUDIT,
    _MESSAGE_TABLE: _MESSAGE,
}


def _table_columns(sync_connection) -> dict[str, set[str]]:
    inspector = inspect(sync_connection)
    names = set(inspector.get_table_names())
    return {
        table_name: {str(column["name"]) for column in inspector.get_columns(table_name)}
        for table_name in (
            _WORK_TABLE,
            _AUDIT_TABLE,
            _MESSAGE_TABLE,
            _SESSION_TABLE,
            _EVENT_TABLE,
            _STREAM_TABLE,
        )
        if table_name in names
    }


def _id(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if value.startswith("+"):
        value = value[1:]
    if not value or not value.isascii() or not value.isdigit():
        return None
    parsed = int(value)
    return parsed if parsed > 0 else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _lower(value: Any) -> str:
    return value.lower() if isinstance(value, str) else str(value or "").lower()


def _same_owner(row: Any, work: Any) -> bool:
    return row["uid"] == work["uid"] and row["session_id"] == work["session_id"] and row["profile_id"] == work["profile_id"]


def _same_audit_owner(row: Any, work: Any) -> bool:
    return row["uid"] == work["uid"] and row["session_id"] == work["session_id"]


def _valid_source_message(message: Any, work: Any) -> bool:
    return _same_owner(message, work) and _lower(message["role"]) == "assistant" and _lower(message["type"]) == "tool_call"


async def _load_rows(
    session: AsyncSession,
    table: Table,
    ids: set[int],
    cache: dict[int, Any],
) -> None:
    missing_ids = sorted(message_id for message_id in ids if message_id not in cache)
    for offset in range(0, len(missing_ids), _BATCH_SIZE):
        result = await session.execute(select(table).where(table.c.id.in_(missing_ids[offset : offset + _BATCH_SIZE])))
        for row in result.mappings():
            row_id = _id(row["id"])
            if row_id is not None:
                cache[row_id] = dict(row)
                cache[row_id]["id"] = row_id


async def _active_works(session: AsyncSession) -> list[dict[str, Any]]:
    works: list[dict[str, Any]] = []
    last_id = 0
    while True:
        result = await session.execute(
            select(_WORK)
            .where(
                _WORK.c.id > last_id,
                func.lower(_WORK.c.work_type) == "confirmed_tool_execution",
                func.lower(_WORK.c.source_type) == "audit_record",
                func.lower(_WORK.c.status).in_(sorted(_ACTIVE_STATUSES)),
            )
            .order_by(_WORK.c.id.asc())
            .limit(_BATCH_SIZE)
        )
        rows = result.mappings().all()
        if not rows:
            return works
        for row in rows:
            work_id = _id(row["id"])
            if work_id is None or not row["uid"] or not row["session_id"]:
                continue
            work = dict(row)
            work["id"] = work_id
            works.append(work)
        last_id = _id(rows[-1]["id"]) or last_id


def _candidate_ids(work: Any) -> set[int]:
    state = work["execution_state"]
    if not isinstance(state, dict):
        return set()
    checkpoint = state.get("dispatcher_checkpoint")
    checkpoint = checkpoint if isinstance(checkpoint, dict) else {}
    message_ids: set[int] = set()

    def add(value: Any) -> None:
        message_id = _id(value)
        if message_id is not None:
            message_ids.add(message_id)

    for key in ("messages", "turn_messages"):
        items = checkpoint.get(key)
        if isinstance(items, list):
            for item in items:
                if isinstance(item, dict):
                    add(item.get("id"))
    pending_resume_ids = checkpoint.get("pending_resume_message_ids")
    if isinstance(pending_resume_ids, list):
        for value in pending_resume_ids:
            add(value)
    external_ids = state.get("external_tool_result_message_ids")
    if isinstance(external_ids, list):
        for value in external_ids:
            add(value)
    for key in ("pending_tools", "terminal_watch_waits"):
        slots = state.get(key)
        if isinstance(slots, list):
            for slot in slots:
                if isinstance(slot, dict):
                    add(slot.get("tool_result_message_id"))

    excluded = {_id(work["result_message_id"]), _id(state.get("decision_message_id"))}
    return {message_id for message_id in message_ids if message_id not in excluded}


def _reserve_work_ids(work: Any, reserved_ids: set[int]) -> None:
    result_message_id = _id(work["result_message_id"])
    if result_message_id is not None:
        reserved_ids.add(result_message_id)
    state = _dict(work["execution_state"])
    decision_message_id = _id(state.get("decision_message_id"))
    if decision_message_id is not None:
        reserved_ids.add(decision_message_id)


async def _trace_source(
    session: AsyncSession,
    work: Any,
    *,
    audit_cache: dict[int, Any],
    work_cache: dict[int, Any],
    message_cache: dict[int, Any],
) -> dict[str, Any]:
    current_audit_id = _id(work["source_id"])
    visited_audit_ids: set[int] = set()
    visited_work_ids: set[int] = set()
    source_message_ids: set[int] = set()
    proposed_owners: dict[int, int] = {}
    reserved_ids: set[int] = set()
    independent = False

    while current_audit_id is not None and current_audit_id not in visited_audit_ids:
        visited_audit_ids.add(current_audit_id)
        await _load_rows(session, _AUDIT, {current_audit_id}, audit_cache)
        audit = audit_cache.get(current_audit_id)
        if audit is None or not _same_audit_owner(audit, work):
            break
        decision_message_id = _id(audit["decision_message_id"])
        if decision_message_id is not None:
            reserved_ids.add(decision_message_id)

        source_message_id = _id(audit["source_assistant_message_id"])
        if source_message_id is None:
            break
        await _load_rows(session, _MESSAGE, {source_message_id}, message_cache)
        source_message = message_cache.get(source_message_id)
        if source_message is None or not _valid_source_message(source_message, work):
            break
        source_message_ids.add(source_message_id)
        if source_message["work_id"] is not None:
            independent = True
            break

        source_work_id = _id(audit["source_work_id"])
        if source_work_id is None or source_work_id in visited_work_ids:
            break
        visited_work_ids.add(source_work_id)
        await _load_rows(session, _WORK, {source_work_id}, work_cache)
        source_work = work_cache.get(source_work_id)
        if source_work is None or not _same_owner(source_work, work):
            break
        _reserve_work_ids(source_work, reserved_ids)
        proposed_owners[source_message_id] = source_work_id

        source_work_type = _lower(source_work["work_type"])
        if source_work_type == "agent_work":
            independent = True
            break
        if source_work_type != "confirmed_tool_execution" or _lower(source_work["source_type"]) != "audit_record":
            break
        current_audit_id = _id(source_work["source_id"])

    return {
        "independent": independent,
        "source_message_ids": source_message_ids,
        "proposed_owners": proposed_owners if independent else {},
        "reserved_ids": reserved_ids,
    }


def _candidate_kind(message: Any) -> bool:
    role = _lower(message["role"])
    message_type = _lower(message["type"])
    return (role == "assistant" and message_type == "tool_call") or (role == "tool" and message_type == "tool_result") or (role in {"user", "assistant"} and message_type in {"background_result", "background_task_result"})


async def _assign_messages(session: AsyncSession, *, message_ids: set[int], work: Any) -> bool:
    changed = False
    ordered_ids = sorted(message_ids)
    for offset in range(0, len(ordered_ids), _BATCH_SIZE):
        result = await session.execute(
            update(_MESSAGE)
            .where(
                _MESSAGE.c.id.in_(ordered_ids[offset : offset + _BATCH_SIZE]),
                _MESSAGE.c.uid == work["uid"],
                _MESSAGE.c.session_id == work["session_id"],
                _MESSAGE.c.profile_id == work["profile_id"],
                _MESSAGE.c.work_id.is_(None),
            )
            .values(work_id=work["id"])
        )
        changed = changed or result.rowcount is not None and result.rowcount > 0
    return changed


def _clear_history(event: dict[str, Any]) -> dict[str, Any]:
    cleared = dict(event)
    if "history" in event:
        cleared["history"] = []
    response = event.get("response")
    if isinstance(response, dict) and "history" in response:
        cleared["response"] = {**response, "history": []}
    return cleared


async def _repair_events(session: AsyncSession, work: Any, columns: set[str]) -> None:
    if not {"id", "uid", "session_id", "event"}.issubset(columns):
        return
    last_id = 0
    while True:
        rows = (await session.execute(select(_EVENT.c.id, _EVENT.c.event).where(_EVENT.c.uid == work["uid"], _EVENT.c.session_id == work["session_id"], _EVENT.c.id > last_id).order_by(_EVENT.c.id.asc()).limit(_BATCH_SIZE))).mappings().all()
        if not rows:
            return
        delete_ids: set[int] = set()
        updates: dict[int, dict[str, Any]] = {}
        for row in rows:
            event_id = _id(row["id"])
            event = _dict(row["event"])
            if event_id is None or _id(event.get("work_id")) != work["id"]:
                continue
            if event.get("uid") not in (None, work["uid"]) or event.get("session_id") not in (None, work["session_id"]):
                continue
            event_type = _lower(event.get("type"))
            if event_type in _INTERNAL_EVENT_TYPES or event_type.startswith("agent_loop"):
                delete_ids.add(event_id)
            elif event_type in _FINAL_EVENT_TYPES and (("history" in event and event.get("history") != []) or isinstance(event.get("response"), dict) and "history" in event["response"] and event["response"].get("history") != []):
                updates[event_id] = _clear_history(event)
        if delete_ids:
            await session.execute(delete(_EVENT).where(_EVENT.c.id.in_(sorted(delete_ids)), _EVENT.c.uid == work["uid"], _EVENT.c.session_id == work["session_id"]))
        for event_id, event in updates.items():
            if event_id not in delete_ids:
                await session.execute(update(_EVENT).where(_EVENT.c.id == event_id, _EVENT.c.uid == work["uid"], _EVENT.c.session_id == work["session_id"]).values(event=event))
        last_id = _id(rows[-1]["id"]) or last_id


async def _clear_work_response(session: AsyncSession, work: Any) -> None:
    state = work["execution_state"]
    if not isinstance(state, dict):
        return
    response = state.get("response")
    if not isinstance(response, dict) or "history" not in response or response.get("history") == []:
        return
    await session.execute(update(_WORK).where(_WORK.c.id == work["id"]).values(execution_state={**state, "response": {**response, "history": []}}))


async def _refresh_sessions(session: AsyncSession, keys: set[tuple[str, str]], columns: set[str]) -> None:
    if not keys or not {"uid", "session_id"}.issubset(columns):
        return
    has_activity = {"latest_message_id", "last_message_at"}.issubset(columns)
    has_revision = "context_content_revision" in columns
    if not has_activity and not has_revision:
        return
    for uid, session_id in keys:
        values: dict[str, Any] = {}
        if has_activity:
            latest_id, latest_at = (
                await session.execute(
                    select(func.max(_MESSAGE.c.id), func.max(_MESSAGE.c.created_at)).where(
                        _MESSAGE.c.uid == uid,
                        _MESSAGE.c.session_id == session_id,
                        _MESSAGE.c.work_id.is_(None),
                    )
                )
            ).one()
            values.update(latest_message_id=latest_id, last_message_at=latest_at)
        if has_revision:
            values["context_content_revision"] = func.coalesce(_SESSION.c.context_content_revision, 0) + 1
        await session.execute(update(_SESSION).where(_SESSION.c.uid == uid, _SESSION.c.session_id == session_id).values(**values))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    tables = await connection.run_sync(_table_columns)
    if any(not set(table.c.keys()).issubset(tables.get(table_name, set())) for table_name, table in _REQUIRED_TABLES.items()):
        return

    works = await _active_works(session)
    if not works:
        return
    work_cache = {work["id"]: work for work in works}
    audit_cache: dict[int, Any] = {}
    message_cache: dict[int, Any] = {}
    candidate_ids_by_work = {work["id"]: _candidate_ids(work) for work in works}
    candidate_refs: dict[int, set[int]] = {}
    for work_id, message_ids in candidate_ids_by_work.items():
        for message_id in message_ids:
            candidate_refs.setdefault(message_id, set()).add(work_id)

    reserved_ids: set[int] = set()
    source_excluded_ids: set[int] = set()
    source_proposals: dict[int, set[int]] = {}
    independent_work_ids: set[int] = set()
    for work in works:
        _reserve_work_ids(work, reserved_ids)
        trace = await _trace_source(
            session,
            work,
            audit_cache=audit_cache,
            work_cache=work_cache,
            message_cache=message_cache,
        )
        source_excluded_ids.update(trace["source_message_ids"])
        reserved_ids.update(trace["reserved_ids"])
        if not trace["independent"]:
            continue
        independent_work_ids.add(work["id"])
        for message_id, source_work_id in trace["proposed_owners"].items():
            source_proposals.setdefault(message_id, set()).add(source_work_id)

    await _load_rows(session, _MESSAGE, set(candidate_refs), message_cache)
    source_assignments: dict[int, set[int]] = {}
    for message_id, owner_ids in source_proposals.items():
        if len(owner_ids) != 1 or message_id in reserved_ids:
            continue
        owner_id = next(iter(owner_ids))
        owner = work_cache.get(owner_id)
        message = message_cache.get(message_id)
        if owner is not None and message is not None and message["work_id"] is None:
            source_assignments.setdefault(owner_id, set()).add(message_id)

    affected_sessions: set[tuple[str, str]] = set()
    for owner_id, message_ids in source_assignments.items():
        owner = work_cache[owner_id]
        if await _assign_messages(session, message_ids=message_ids, work=owner):
            affected_sessions.add((owner["uid"], owner["session_id"]))

    candidate_assignments: dict[int, set[int]] = {}
    for work in works:
        if work["id"] not in independent_work_ids:
            continue
        for message_id in candidate_ids_by_work[work["id"]]:
            if message_id in reserved_ids or message_id in source_excluded_ids or candidate_refs.get(message_id) != {work["id"]}:
                continue
            message = message_cache.get(message_id)
            if message is None or message["work_id"] is not None or not _same_owner(message, work) or not _candidate_kind(message):
                continue
            candidate_assignments.setdefault(work["id"], set()).add(message_id)
    for work_id, message_ids in candidate_assignments.items():
        work = work_cache[work_id]
        if await _assign_messages(session, message_ids=message_ids, work=work):
            affected_sessions.add((work["uid"], work["session_id"]))

    event_columns = tables.get(_EVENT_TABLE, set())
    stream_columns = tables.get(_STREAM_TABLE, set())
    for work in works:
        if work["id"] not in independent_work_ids:
            continue
        await _clear_work_response(session, work)
        await _repair_events(session, work, event_columns)
        if "work_id" in stream_columns:
            await session.execute(delete(_STREAM).where(_STREAM.c.work_id == work["id"]))

    await _refresh_sessions(session, affected_sessions, tables.get(_SESSION_TABLE, set()))
