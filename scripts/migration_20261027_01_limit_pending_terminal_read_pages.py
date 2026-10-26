import hashlib
import json
from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261027_01_limit_pending_terminal_read_pages"

_BATCH_SIZE = 500
_MAX_READ_BYTES = 16 * 1024
_ACTIVE_STATUSES = ("pending", "processing")
_TABLE_NAME = "terminal_control_command"

_TERMINAL_CONTROL_COMMAND = Table(
    _TABLE_NAME,
    MetaData(),
    Column("id", Integer),
    Column("action", String(20)),
    Column("payload", JSON),
    Column("payload_hash", String(64)),
    Column("status", String(20)),
)


def _table_columns(sync_connection) -> set[str] | None:
    inspector = inspect(sync_connection)
    if not inspector.has_table(_TABLE_NAME):
        return None
    return {str(column["name"]) for column in inspector.get_columns(_TABLE_NAME)}


def _limit_payload(payload: object) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None

    max_bytes = payload.get("max_bytes")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= _MAX_READ_BYTES:
        return None

    updated_payload = dict(payload)
    updated_payload["max_bytes"] = _MAX_READ_BYTES
    return updated_payload


def _payload_hash(payload: dict[str, Any]) -> str | None:
    try:
        serialized_payload = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        return hashlib.sha256(serialized_payload.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, UnicodeError):
        return None


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    columns = await connection.run_sync(_table_columns)
    if columns is None or not {"id", "action", "payload", "payload_hash", "status"}.issubset(columns):
        return

    last_id: int | None = None
    while True:
        statement = (
            select(_TERMINAL_CONTROL_COMMAND.c.id, _TERMINAL_CONTROL_COMMAND.c.payload)
            .where(
                func.lower(_TERMINAL_CONTROL_COMMAND.c.action) == "read",
                func.lower(_TERMINAL_CONTROL_COMMAND.c.status).in_(_ACTIVE_STATUSES),
            )
            .order_by(_TERMINAL_CONTROL_COMMAND.c.id.asc())
            .limit(_BATCH_SIZE)
        )
        if last_id is not None:
            statement = statement.where(_TERMINAL_CONTROL_COMMAND.c.id > last_id)

        rows = (await session.execute(statement)).all()
        if not rows:
            return

        for row in rows:
            payload = _limit_payload(row.payload)
            if payload is None:
                continue

            payload_hash = _payload_hash(payload)
            if payload_hash is None:
                continue

            await session.execute(
                update(_TERMINAL_CONTROL_COMMAND)
                .where(
                    _TERMINAL_CONTROL_COMMAND.c.id == row.id,
                    func.lower(_TERMINAL_CONTROL_COMMAND.c.action) == "read",
                    func.lower(_TERMINAL_CONTROL_COMMAND.c.status).in_(_ACTIVE_STATUSES),
                )
                .values(payload=payload, payload_hash=payload_hash)
            )

        last_id = rows[-1].id
