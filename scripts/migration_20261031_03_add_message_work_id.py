import json
from typing import Any

from sqlalchemy import JSON, Column, Index, Integer, MetaData, String, Table, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261031_03_add_message_work_id"

MESSAGE_TABLE = "message"
WORK_TABLE = "session_reply_work_item"
WORK_ID_COLUMN = "work_id"
WORK_ID_INDEX = "ix_message_work_id"

_METADATA = MetaData()
_WORK = Table(
    WORK_TABLE,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("profile_id", Integer),
    Column("work_type", String(40)),
    Column("source_type", String(40)),
    Column("source_id", String(100)),
    Column("input_message_ids", JSON),
    Column("result_message_id", Integer),
    Column("execution_state", JSON),
)
_MESSAGE = Table(
    MESSAGE_TABLE,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("profile_id", Integer),
    Column(WORK_ID_COLUMN, Integer),
)


def _table_names(sync_connection) -> set[str]:
    return set(inspect(sync_connection).get_table_names())


def _message_columns(sync_connection) -> set[str] | None:
    if MESSAGE_TABLE not in _table_names(sync_connection):
        return None
    return {str(column["name"]) for column in inspect(sync_connection).get_columns(MESSAGE_TABLE)}


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_positive_int(value: Any) -> int | None:
    if _positive_int(value):
        return value
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _add_message_id(message_ids: set[int], value: Any) -> None:
    parsed = _parse_positive_int(value)
    if parsed is not None:
        message_ids.add(parsed)


def _add_message_ids_from_items(message_ids: set[int], items: Any) -> None:
    for item in _as_list(items):
        if isinstance(item, dict):
            _add_message_id(message_ids, item.get("id"))


def _collect_work_message_ids(row: Any) -> set[int]:
    message_ids: set[int] = set()
    _add_message_id(message_ids, row["result_message_id"])
    if str(row["source_type"] or "").lower() == "scheduled_task_run":
        _add_message_id(message_ids, row["source_id"])
    for value in _as_list(row["input_message_ids"]):
        _add_message_id(message_ids, value)

    state = _as_dict(row["execution_state"])
    agent_work = _as_dict(state.get("agent_work"))
    _add_message_id(message_ids, agent_work.get("instruction_message_id"))
    response = _as_dict(state.get("response"))
    _add_message_id(message_ids, response.get("message_id"))
    for key in ("external_tool_result_message_ids", "pending_resume_message_ids"):
        for value in _as_list(state.get(key)):
            _add_message_id(message_ids, value)

    checkpoint = _as_dict(state.get("dispatcher_checkpoint"))
    for key in ("messages", "turn_messages", "history"):
        _add_message_ids_from_items(message_ids, checkpoint.get(key))
    _add_message_ids_from_items(message_ids, response.get("history"))
    return message_ids


async def _backfill_work_message_ids(session: AsyncSession) -> None:
    connection = await session.connection()
    table_names = await connection.run_sync(_table_names)
    if WORK_TABLE not in table_names:
        return

    work_rows = (
        await session.execute(
            select(
                _WORK.c.id,
                _WORK.c.uid,
                _WORK.c.session_id,
                _WORK.c.profile_id,
                _WORK.c.work_type,
                _WORK.c.source_type,
                _WORK.c.source_id,
                _WORK.c.input_message_ids,
                _WORK.c.result_message_id,
                _WORK.c.execution_state,
            ).where(_WORK.c.work_type.in_(("AGENT_WORK", "agent_work")))
        )
    ).mappings()

    for work in work_rows:
        if not _positive_int(work["id"]):
            continue
        message_ids = _collect_work_message_ids(work)
        if not message_ids:
            continue
        valid_message_ids = (
            await session.execute(
                select(_MESSAGE.c.id).where(
                    _MESSAGE.c.id.in_(sorted(message_ids)),
                    _MESSAGE.c.uid == work["uid"],
                    _MESSAGE.c.session_id == work["session_id"],
                    _MESSAGE.c.profile_id == work["profile_id"],
                    _MESSAGE.c.work_id.is_(None),
                )
            )
        ).scalars()
        valid_message_ids = [message_id for message_id in valid_message_ids if _positive_int(message_id)]
        if valid_message_ids:
            await session.execute(update(_MESSAGE).where(_MESSAGE.c.id.in_(valid_message_ids), _MESSAGE.c.work_id.is_(None)).values(work_id=work["id"]))


def _create_work_id_index(sync_connection) -> None:
    metadata = MetaData()
    message = Table(
        MESSAGE_TABLE,
        metadata,
        Column(WORK_ID_COLUMN, Integer, nullable=True),
    )
    Index(WORK_ID_INDEX, message.c[WORK_ID_COLUMN], unique=False).create(sync_connection, checkfirst=True)


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    message_columns = await connection.run_sync(_message_columns)
    if message_columns is None:
        return

    if WORK_ID_COLUMN not in message_columns:
        await session.execute(text(f"ALTER TABLE {MESSAGE_TABLE} ADD COLUMN {WORK_ID_COLUMN} INTEGER NULL"))

    await _backfill_work_message_ids(session)
    await connection.run_sync(_create_work_id_index)
