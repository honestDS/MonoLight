import json
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, delete, func, inspect, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261101_01_repair_agent_work_delivery"

_WORK_TABLE = "session_reply_work_item"
_MESSAGE_TABLE = "message"
_SESSION_TABLE = "chat_session"
_EVENT_TABLE = "session_event"
_STREAM_TABLE = "session_reply_stream_event"
_BATCH_SIZE = 200
_TOOL_EVENT_TYPES = {"tool_results_update", "audit_tool_results_update", "tool_call", "tool_result", "tool_start", "tool_end", "agent_loop_start", "reasoning", "content", "turn_end", "input_dequeued", "llm_request_metadata"}

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
    Column("result_message_id", Integer),
    Column("execution_state", JSON),
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
    Column("content", Text),
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

_REQUIRED_WORK_COLUMNS = {"id", "uid", "session_id", "profile_id", "work_type", "source_type", "source_id", "status", "result_message_id", "execution_state"}
_REQUIRED_MESSAGE_COLUMNS = {"id", "uid", "session_id", "profile_id", "role", "type", "content", "work_id", "created_at"}


def _table_columns(sync_connection) -> dict[str, set[str]]:
    inspector = inspect(sync_connection)
    names = set(inspector.get_table_names())
    return {name: {str(column["name"]) for column in inspector.get_columns(name)} for name in (_WORK_TABLE, _MESSAGE_TABLE, _SESSION_TABLE, _EVENT_TABLE, _STREAM_TABLE) if name in names}


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
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return {}
    return value if isinstance(value, dict) else {}


def _ids(value: Any) -> set[int]:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            return set()
    if not isinstance(value, list):
        return set()
    return {message_id for item in value if (message_id := _id(item)) is not None}


def _lower(value: Any) -> str:
    return value.lower() if isinstance(value, str) else str(value or "").lower()


def _same_owner(row: Any, work: Any) -> bool:
    return row["uid"] == work["uid"] and row["session_id"] == work["session_id"] and row["profile_id"] == work["profile_id"]


async def _message_rows(session: AsyncSession, message_ids: set[int]) -> dict[int, Any]:
    rows: dict[int, Any] = {}
    ordered_ids = sorted(message_ids)
    for offset in range(0, len(ordered_ids), _BATCH_SIZE):
        result = await session.execute(select(_MESSAGE).where(_MESSAGE.c.id.in_(ordered_ids[offset : offset + _BATCH_SIZE])))
        for row in result.mappings():
            message_id = _id(row["id"])
            if message_id is not None:
                rows[message_id] = row
    return rows


def _trigger_ids(work: Any, spec: dict[str, Any]) -> set[int]:
    instruction_id = _id(spec.get("instruction_message_id"))
    source_id = _id(work["source_id"]) if _lower(work["source_type"]) == "scheduled_task_run" else None
    if source_id is not None and instruction_id is not None and source_id != instruction_id:
        return set()
    return {value for value in (source_id, instruction_id) if value is not None}


def _valid_trigger(message: Any, work: Any, spec: dict[str, Any]) -> bool:
    instruction = spec.get("instruction")
    return _same_owner(message, work) and _lower(message["role"]) == "user" and _lower(message["type"]) == "scheduled_task_trigger" and isinstance(instruction, str) and bool(instruction.strip()) and message["content"] == instruction


async def _repair_internal_messages(session: AsyncSession, work: Any, state: dict[str, Any]) -> None:
    spec = _dict(state.get("agent_work"))
    trigger_ids = _trigger_ids(work, spec)
    checkpoint = _dict(state.get("dispatcher_checkpoint"))
    marker_ids = _ids(state.get("external_tool_result_message_ids")) | _ids(checkpoint.get("pending_resume_message_ids"))
    messages = await _message_rows(session, trigger_ids | marker_ids)
    owned_ids = {message_id for message_id, message in messages.items() if message["work_id"] is None and (message_id in trigger_ids and _valid_trigger(message, work, spec) or message_id in marker_ids and _same_owner(message, work) and _lower(message["type"]) == "background_result")}
    if owned_ids:
        await session.execute(
            update(_MESSAGE)
            .where(
                _MESSAGE.c.id.in_(sorted(owned_ids)),
                _MESSAGE.c.uid == work["uid"],
                _MESSAGE.c.session_id == work["session_id"],
                _MESSAGE.c.profile_id == work["profile_id"],
                _MESSAGE.c.work_id.is_(None),
            )
            .values(work_id=work["id"])
        )


def _valid_final(message: Any, work: Any, spec: dict[str, Any], response: dict[str, Any]) -> bool:
    if not _same_owner(message, work) or message["work_id"] not in {None, work["id"]} or response.get("skip_reply") is True:
        return False
    if _lower(spec.get("report_target")) != "session":
        return False
    if _lower(work["status"]) == "succeeded":
        return _lower(message["role"]) == "assistant" and _lower(message["type"]) in {"text", "audit_confirmation"}
    return _lower(work["status"]) == "failed" and _lower(message["role"]) == "err" and _lower(message["type"]) == "text"


async def _detach_final(session: AsyncSession, work: Any, state: dict[str, Any]) -> None:
    result_id = _id(work["result_message_id"])
    if result_id is None:
        return
    spec = _dict(state.get("agent_work"))
    response = _dict(state.get("response"))
    message = (await _message_rows(session, {result_id})).get(result_id)
    if message is None or not _valid_final(message, work, spec, response):
        return
    await session.execute(
        update(_MESSAGE)
        .where(
            _MESSAGE.c.id == result_id,
            _MESSAGE.c.uid == work["uid"],
            _MESSAGE.c.session_id == work["session_id"],
            _MESSAGE.c.profile_id == work["profile_id"],
            or_(_MESSAGE.c.work_id.is_(None), _MESSAGE.c.work_id == work["id"]),
        )
        .values(work_id=None)
    )


def _event_message_ids(event: dict[str, Any]) -> set[int]:
    items = event.get("messages")
    if not isinstance(items, list):
        return set()
    return {message_id for item in items if isinstance(item, dict) for value in (item.get("id"), item.get("db_id")) if (message_id := _id(value)) is not None}


def _event_owned(event: dict[str, Any], work: Any) -> bool:
    return _id(event.get("work_id")) == work["id"] and (event.get("uid") is None or event.get("uid") == work["uid"]) and (event.get("session_id") is None or event.get("session_id") == work["session_id"])


def _audit_event_owned(event: dict[str, Any], messages: dict[int, Any], work: Any) -> bool:
    message_ids = _event_message_ids(event)
    return bool(message_ids) and all((message := messages.get(message_id)) is not None and _same_owner(message, work) and message["work_id"] == work["id"] and _lower(message["role"]) == "tool" and _lower(message["type"]) == "tool_result" for message_id in message_ids)


def _clear_history(event: dict[str, Any]) -> dict[str, Any]:
    result = {**event, "history": []}
    if isinstance(event.get("response"), dict):
        result["response"] = {**event["response"], "history": []}
    return result


async def _repair_events(session: AsyncSession, work: Any, state: dict[str, Any], columns: set[str]) -> None:
    if not {"id", "uid", "session_id", "event"}.issubset(columns):
        return
    spec = _dict(state.get("agent_work"))
    response = _dict(state.get("response"))
    skip_reply = response.get("skip_reply") is True
    parent_target = _lower(spec.get("report_target")) == "parent_work"
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
            if event_id is None or not event or not (event.get("uid") is None or event.get("uid") == work["uid"]) or not (event.get("session_id") is None or event.get("session_id") == work["session_id"]):
                continue
            event_type = _lower(event.get("type"))
            owned = _event_owned(event, work)
            if not owned and event_type == "audit_tool_results_update":
                owned = _audit_event_owned(event, await _message_rows(session, _event_message_ids(event)), work)
            if not owned:
                continue
            event_skip = event.get("skip_reply") is True or _dict(event.get("response")).get("skip_reply") is True
            if event_type in _TOOL_EVENT_TYPES:
                delete_ids.add(event_id)
            elif event_type in {"proactive_reply", "proactive_reply_error"}:
                if parent_target or skip_reply or event_skip:
                    delete_ids.add(event_id)
                else:
                    updates[event_id] = _clear_history(event)
            elif event_type == "done" and (parent_target or skip_reply or event_skip):
                delete_ids.add(event_id)
        if delete_ids:
            await session.execute(delete(_EVENT).where(_EVENT.c.id.in_(sorted(delete_ids)), _EVENT.c.uid == work["uid"], _EVENT.c.session_id == work["session_id"]))
        for event_id, event in updates.items():
            if event_id not in delete_ids:
                await session.execute(update(_EVENT).where(_EVENT.c.id == event_id, _EVENT.c.uid == work["uid"], _EVENT.c.session_id == work["session_id"]).values(event=event))
        last_id = _id(rows[-1]["id"]) or last_id


async def _refresh_activity(session: AsyncSession, keys: set[tuple[str, str]], columns: set[str]) -> None:
    if not keys or not {"uid", "session_id", "latest_message_id", "last_message_at"}.issubset(columns):
        return
    for uid, session_id in keys:
        latest_id, latest_at = (await session.execute(select(func.max(_MESSAGE.c.id), func.max(_MESSAGE.c.created_at)).where(_MESSAGE.c.uid == uid, _MESSAGE.c.session_id == session_id, _MESSAGE.c.work_id.is_(None)))).one()
        await session.execute(update(_SESSION).where(_SESSION.c.uid == uid, _SESSION.c.session_id == session_id).values(latest_message_id=latest_id, last_message_at=latest_at))


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    tables = await connection.run_sync(_table_columns)
    if not _REQUIRED_WORK_COLUMNS.issubset(tables.get(_WORK_TABLE, set())) or not _REQUIRED_MESSAGE_COLUMNS.issubset(tables.get(_MESSAGE_TABLE, set())):
        return

    event_columns = tables.get(_EVENT_TABLE, set())
    stream_columns = tables.get(_STREAM_TABLE, set())
    affected_sessions: set[tuple[str, str]] = set()
    last_id = 0
    while True:
        rows = (await session.execute(select(_WORK).where(_WORK.c.id > last_id, func.lower(_WORK.c.work_type) == "agent_work").order_by(_WORK.c.id.asc()).limit(_BATCH_SIZE))).mappings().all()
        if not rows:
            break
        for row in rows:
            work_id = _id(row["id"])
            if work_id is None or not row["uid"] or not row["session_id"]:
                continue
            work = dict(row)
            work["id"] = work_id
            state = _dict(work["execution_state"])
            spec = _dict(state.get("agent_work"))
            affected_sessions.add((work["uid"], work["session_id"]))
            await _repair_internal_messages(session, work, state)
            if _lower(spec.get("report_target")) == "session" and isinstance(work["execution_state"], dict) and isinstance(state.get("response"), dict):
                response = _dict(state["response"])
                if response.get("history") != []:
                    state["response"] = {**response, "history": []}
                    await session.execute(update(_WORK).where(_WORK.c.id == work_id).values(execution_state=state))
            await _detach_final(session, work, state)
            if event_columns:
                await _repair_events(session, work, state, event_columns)
            if "work_id" in stream_columns:
                await session.execute(delete(_STREAM).where(_STREAM.c.work_id == work_id))
        last_id = _id(rows[-1]["id"]) or last_id
    await _refresh_activity(session, affected_sessions, tables.get(_SESSION_TABLE, set()))
