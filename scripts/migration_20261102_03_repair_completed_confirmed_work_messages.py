from typing import Any

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from scripts import migration_20261102_02_repair_confirmed_work_message_ownership as ownership

MIGRATION_ID = "20261102_03_repair_completed_confirmed_work_messages"

_WORK = ownership._WORK
_AUDIT = ownership._AUDIT
_MESSAGE = ownership._MESSAGE
_SESSION = ownership._SESSION
_EVENT = ownership._EVENT
_STREAM = ownership._STREAM
_BATCH_SIZE = ownership._BATCH_SIZE
_SESSION_TABLE = ownership._SESSION_TABLE


async def _all_confirmed_works(session: AsyncSession) -> list[dict[str, Any]]:
    works: list[dict[str, Any]] = []
    last_id = 0
    while True:
        result = await session.execute(
            select(_WORK)
            .where(
                _WORK.c.id > last_id,
                func.lower(_WORK.c.work_type) == "confirmed_tool_execution",
                func.lower(_WORK.c.source_type) == "audit_record",
            )
            .order_by(_WORK.c.id.asc())
            .limit(_BATCH_SIZE)
        )
        rows = result.mappings().all()
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


def _candidate_ids(work: Any) -> set[int]:
    message_ids = set(ownership._candidate_ids(work))
    state = ownership._dict(work.get("execution_state"))
    response = state.get("response")
    response = response if isinstance(response, dict) else {}
    history = response.get("history")
    history_items = []
    if isinstance(history, list):
        history_items = history
    elif isinstance(history, dict):
        history_items = [history]
    for item in history_items:
        if not isinstance(item, dict):
            continue
        for key in ("id", "db_id"):
            message_id = ownership._id(item.get(key))
            if message_id is not None:
                message_ids.add(message_id)

    excluded = {ownership._id(work.get("result_message_id")), ownership._id(state.get("decision_message_id"))}
    return {message_id for message_id in message_ids if message_id not in excluded}


async def _validated_trace(
    session: AsyncSession,
    work: Any,
    *,
    audit_cache: dict[int, Any],
    work_cache: dict[int, Any],
    message_cache: dict[int, Any],
) -> dict[str, Any] | None:
    current_audit_id = ownership._id(work.get("source_id"))
    chain = [work["id"]]
    visited_audit_ids: set[int] = set()
    visited_work_ids: set[int] = {work["id"]}
    source_message_ids: set[int] = set()
    proposed_owners: dict[int, int] = {}
    reserved_ids: set[int] = set()

    while current_audit_id is not None and current_audit_id not in visited_audit_ids:
        visited_audit_ids.add(current_audit_id)
        await ownership._load_rows(session, _AUDIT, {current_audit_id}, audit_cache)
        audit = audit_cache.get(current_audit_id)
        if audit is None or not ownership._same_audit_owner(audit, work):
            return None

        source_message_id = ownership._id(audit.get("source_assistant_message_id"))
        if source_message_id is None:
            return None
        await ownership._load_rows(session, _MESSAGE, {source_message_id}, message_cache)
        source_message = message_cache.get(source_message_id)
        if source_message is None or not ownership._valid_source_message(source_message, work):
            return None
        source_message_ids.add(source_message_id)

        raw_source_work_id = audit.get("source_work_id")
        source_work_id = ownership._id(raw_source_work_id)
        if raw_source_work_id is not None and source_work_id is None:
            return None
        raw_message_work_id = source_message.get("work_id")
        message_work_id = ownership._id(raw_message_work_id)
        if raw_message_work_id is not None and message_work_id is None:
            return None

        if message_work_id is not None:
            if message_work_id in visited_work_ids:
                return None
            if source_work_id is not None and source_work_id != message_work_id:
                return None
            await ownership._load_rows(session, _WORK, {message_work_id}, work_cache)
            source_work = work_cache.get(message_work_id)
            if source_work is None:
                current_audit_id = None
                break
            if not ownership._same_owner(source_work, work):
                return None
            source_work_type = ownership._lower(source_work.get("work_type"))
            if source_work_type == "agent_work":
                ownership._reserve_work_ids(source_work, reserved_ids)
                chain.append(message_work_id)
                current_audit_id = None
                break
            if source_work_type != "confirmed_tool_execution" or ownership._lower(source_work.get("source_type")) != "audit_record":
                return None
            chain.append(message_work_id)
            visited_work_ids.add(message_work_id)
            ownership._reserve_work_ids(source_work, reserved_ids)
            current_audit_id = ownership._id(source_work.get("source_id"))
            if current_audit_id is None:
                return None
            continue

        if source_work_id is None or source_work_id in visited_work_ids:
            return None
        await ownership._load_rows(session, _WORK, {source_work_id}, work_cache)
        source_work = work_cache.get(source_work_id)
        if source_work is None or not ownership._same_owner(source_work, work):
            return None
        source_work_type = ownership._lower(source_work.get("work_type"))
        if source_work_type != "agent_work" and (source_work_type != "confirmed_tool_execution" or ownership._lower(source_work.get("source_type")) != "audit_record"):
            return None
        chain.append(source_work_id)
        visited_work_ids.add(source_work_id)
        ownership._reserve_work_ids(source_work, reserved_ids)
        proposed_owners[source_message_id] = source_work_id

        if source_work_type == "agent_work":
            current_audit_id = None
            break
        current_audit_id = ownership._id(source_work.get("source_id"))
        if current_audit_id is None:
            return None

    if current_audit_id is not None:
        return None
    if not proposed_owners and not source_message_ids:
        return None
    return {
        "independent": True,
        "source_message_ids": source_message_ids,
        "proposed_owners": proposed_owners,
        "reserved_ids": reserved_ids,
        "chain": tuple(chain),
    }


async def _reserve_source_decisions(
    session: AsyncSession,
    works: list[dict[str, Any]],
    *,
    audit_cache: dict[int, Any],
    reserved_ids: set[int],
) -> None:
    audit_ids = {ownership._id(work.get("source_id")) for work in works}
    audit_ids.discard(None)
    await ownership._load_rows(session, _AUDIT, audit_ids, audit_cache)
    for audit_id in audit_ids:
        audit = audit_cache.get(audit_id)
        if audit is not None:
            decision_message_id = ownership._id(audit.get("decision_message_id"))
            if decision_message_id is not None:
                reserved_ids.add(decision_message_id)


def _shared_candidate_owner(
    references: set[int],
    independent_work_ids: set[int],
    chains: dict[int, tuple[int, ...]],
) -> int | None:
    if not references or not references.issubset(independent_work_ids):
        return None
    earliest = [work_id for work_id in references if all(work_id in chains.get(reference_id, ()) for reference_id in references)]
    return earliest[0] if len(earliest) == 1 else None


def _event_message_ids(event: dict[str, Any]) -> set[int]:
    message_ids: set[int] = set()
    messages = event.get("messages")
    if not isinstance(messages, list):
        return message_ids
    for message in messages:
        if not isinstance(message, dict):
            continue
        for key in ("id", "db_id"):
            message_id = ownership._id(message.get(key))
            if message_id is not None:
                message_ids.add(message_id)
    return message_ids


async def _repair_unowned_audit_events(
    session: AsyncSession,
    session_keys: set[tuple[str, str]],
    event_columns: set[str],
    *,
    message_cache: dict[int, Any],
) -> None:
    if not session_keys or not {"id", "uid", "session_id", "event"}.issubset(event_columns):
        return
    last_id = 0
    while True:
        rows = (await session.execute(select(_EVENT.c.id, _EVENT.c.uid, _EVENT.c.session_id, _EVENT.c.event).where(_EVENT.c.id > last_id).order_by(_EVENT.c.id.asc()).limit(_BATCH_SIZE))).mappings().all()
        if not rows:
            return
        delete_ids: set[int] = set()
        for row in rows:
            event_id = ownership._id(row["id"])
            if event_id is None or (row["uid"], row["session_id"]) not in session_keys:
                continue
            event = ownership._dict(row["event"])
            if ownership._lower(event.get("type")) != "audit_tool_results_update" or ("work_id" in event and event.get("work_id") is not None):
                continue
            referenced_ids = _event_message_ids(event)
            if not referenced_ids:
                continue
            await ownership._load_rows(session, _MESSAGE, referenced_ids, message_cache)
            if all(
                (message := message_cache.get(message_id)) is not None and message["uid"] == row["uid"] and message["session_id"] == row["session_id"] and ownership._lower(message["role"]) == "tool" and ownership._lower(message["type"]) == "tool_result" and ownership._id(message["work_id"]) is not None
                for message_id in referenced_ids
            ):
                delete_ids.add(event_id)
        if delete_ids:
            await session.execute(delete(_EVENT).where(_EVENT.c.id.in_(sorted(delete_ids))))
        last_id = ownership._id(rows[-1]["id"]) or last_id


async def _refresh_sessions(
    session: AsyncSession,
    session_keys: set[tuple[str, str]],
    session_columns: set[str],
) -> None:
    if not session_keys or not {"uid", "session_id"}.issubset(session_columns):
        return
    for uid, session_id in session_keys:
        values: dict[str, Any] = {}
        if "latest_message_id" in session_columns or "last_message_at" in session_columns:
            latest_id, latest_at = (
                await session.execute(
                    select(func.max(_MESSAGE.c.id), func.max(_MESSAGE.c.created_at)).where(
                        _MESSAGE.c.uid == uid,
                        _MESSAGE.c.session_id == session_id,
                        _MESSAGE.c.work_id.is_(None),
                    )
                )
            ).one()
            if "latest_message_id" in session_columns:
                values["latest_message_id"] = latest_id
            if "last_message_at" in session_columns:
                values["last_message_at"] = latest_at
        if values:
            await session.execute(update(_SESSION).where(_SESSION.c.uid == uid, _SESSION.c.session_id == session_id).values(**values))

        optional_updates: list[str] = []
        if "context_summary" in session_columns:
            optional_updates.append("context_summary = NULL")
        if "context_summary_message_id" in session_columns:
            optional_updates.append("context_summary_message_id = NULL")
        if "context_summary_revision" in session_columns:
            optional_updates.append("context_summary_revision = COALESCE(context_summary_revision, 0) + 1")
        if "context_content_revision" in session_columns:
            optional_updates.append("context_content_revision = COALESCE(context_content_revision, 0) + 1")
        if optional_updates:
            await session.execute(
                text(f"UPDATE {_SESSION_TABLE} SET {', '.join(optional_updates)} WHERE uid = :uid AND session_id = :session_id"),
                {"uid": uid, "session_id": session_id},
            )


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    tables = await connection.run_sync(ownership._table_columns)
    if any(not set(table.c.keys()).issubset(tables.get(table_name, set())) for table_name, table in ownership._REQUIRED_TABLES.items()):
        return

    works = await _all_confirmed_works(session)
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
    for work in works:
        ownership._reserve_work_ids(work, reserved_ids)
    await _reserve_source_decisions(session, works, audit_cache=audit_cache, reserved_ids=reserved_ids)

    source_excluded_ids: set[int] = set()
    source_proposals: dict[int, set[int]] = {}
    independent_work_ids: set[int] = set()
    traces: dict[int, dict[str, Any]] = {}
    for work in works:
        trace = await _validated_trace(
            session,
            work,
            audit_cache=audit_cache,
            work_cache=work_cache,
            message_cache=message_cache,
        )
        if trace is None:
            continue
        work_id = work["id"]
        independent_work_ids.add(work_id)
        traces[work_id] = trace
        source_excluded_ids.update(trace["source_message_ids"])
        reserved_ids.update(trace["reserved_ids"])
        for message_id, source_work_id in trace["proposed_owners"].items():
            source_proposals.setdefault(message_id, set()).add(source_work_id)

    if not independent_work_ids:
        return

    await ownership._load_rows(session, _MESSAGE, set(candidate_refs) | set(source_proposals), message_cache)
    affected_sessions: set[tuple[str, str]] = set()

    source_assignments: dict[int, set[int]] = {}
    for message_id, owner_ids in source_proposals.items():
        if len(owner_ids) != 1 or message_id in reserved_ids:
            continue
        owner_id = next(iter(owner_ids))
        owner = work_cache.get(owner_id)
        message = message_cache.get(message_id)
        if owner is not None and message is not None and message["work_id"] is None and ownership._valid_source_message(message, owner):
            source_assignments.setdefault(owner_id, set()).add(message_id)
    for owner_id, message_ids in source_assignments.items():
        owner = work_cache[owner_id]
        if await ownership._assign_messages(session, message_ids=message_ids, work=owner):
            for message_id in message_ids:
                if message := message_cache.get(message_id):
                    message["work_id"] = owner["id"]
            affected_sessions.add((owner["uid"], owner["session_id"]))

    chains = {work_id: trace["chain"] for work_id, trace in traces.items()}
    candidate_assignments: dict[int, set[int]] = {}
    for message_id, references in candidate_refs.items():
        if message_id in reserved_ids or message_id in source_excluded_ids:
            continue
        owner_id = _shared_candidate_owner(references, independent_work_ids, chains)
        if owner_id is None:
            continue
        message = message_cache.get(message_id)
        owner = work_cache.get(owner_id)
        if message is not None and owner is not None and message["work_id"] is None and ownership._same_owner(message, owner) and ownership._candidate_kind(message):
            candidate_assignments.setdefault(owner_id, set()).add(message_id)
    for owner_id, message_ids in candidate_assignments.items():
        owner = work_cache[owner_id]
        if await ownership._assign_messages(session, message_ids=message_ids, work=owner):
            for message_id in message_ids:
                if message := message_cache.get(message_id):
                    message["work_id"] = owner["id"]
            affected_sessions.add((owner["uid"], owner["session_id"]))

    event_columns = tables.get(ownership._EVENT_TABLE, set())
    stream_columns = tables.get(ownership._STREAM_TABLE, set())
    independent_sessions = {(work_cache[work_id]["uid"], work_cache[work_id]["session_id"]) for work_id in independent_work_ids}
    for work_id in independent_work_ids:
        work = work_cache[work_id]
        await ownership._clear_work_response(session, work)
        await ownership._repair_events(session, work, event_columns)
        if "work_id" in stream_columns:
            await session.execute(delete(_STREAM).where(_STREAM.c.work_id == work_id))

    await _repair_unowned_audit_events(session, independent_sessions, event_columns, message_cache=message_cache)
    await _refresh_sessions(session, affected_sessions, tables.get(ownership._SESSION_TABLE, set()))
