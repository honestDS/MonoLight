import hashlib
import json
from typing import Any

from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20260916_rename_memory_and_knowledge_tool"
MESSAGE_TABLE = "message"
AUDIT_RECORD_TABLE = "audit_record"
AUDIT_TOOL_DETAIL_TABLE = "audit_tool_detail"
OLD_TOOL_NAME = "manage_longterm_memory"
NEW_TOOL_NAME = "manage_memory_and_knowledge"
MIGRATION_BATCH_SIZE = 500


def _get_table_names(sync_connection) -> set[str]:
    return set(inspect(sync_connection).get_table_names())


def _canonical_json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False, sort_keys=True, separators=(",", ":"))


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _build_round_hash(*, tool_calls: list[dict[str, Any]], uid: str, session_id: str, working_directory: str) -> str:
    round_payload = {
        "session_id": session_id,
        "tool_calls": [
            {
                "arguments": dict(tool_call.get("arguments") or {}),
                "id": tool_call["id"],
                "name": tool_call["name"],
                "turn_index": turn_index,
            }
            for turn_index, tool_call in enumerate(tool_calls)
        ],
        "uid": uid,
        "working_directory": working_directory,
    }
    return _sha256_text(_canonical_json_dumps(round_payload))


def _rename_tool_calls(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    tool_calls = payload.get("tool_calls")
    if not isinstance(tool_calls, list):
        return False

    changed = False
    for tool_call in tool_calls:
        if isinstance(tool_call, dict) and tool_call.get("name") == OLD_TOOL_NAME:
            tool_call["name"] = NEW_TOOL_NAME
            changed = True
    return changed


async def _migrate_audit_snapshot(
    session: AsyncSession,
    *,
    source_message_id: int,
    tool_calls: list[dict[str, Any]],
) -> None:
    result = await session.execute(
        text(
            f"""
            SELECT id, uid, session_id, working_directory
            FROM {AUDIT_RECORD_TABLE}
            WHERE source_assistant_message_id = :source_message_id
            """
        ),
        {"source_message_id": source_message_id},
    )
    for record in result.mappings().all():
        await session.execute(
            text(
                f"""
                UPDATE {AUDIT_TOOL_DETAIL_TABLE}
                SET tool_name = :new_tool_name
                WHERE audit_record_id = :audit_record_id
                  AND tool_name = :old_tool_name
                """
            ),
            {
                "audit_record_id": record["id"],
                "old_tool_name": OLD_TOOL_NAME,
                "new_tool_name": NEW_TOOL_NAME,
            },
        )
        round_hash = _build_round_hash(
            tool_calls=tool_calls,
            uid=str(record["uid"]),
            session_id=str(record["session_id"]),
            working_directory=str(record["working_directory"]),
        )
        await session.execute(
            text(f"UPDATE {AUDIT_RECORD_TABLE} SET round_arguments_hash = :round_hash WHERE id = :audit_record_id"),
            {"round_hash": round_hash, "audit_record_id": record["id"]},
        )


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    table_names = await connection.run_sync(_get_table_names)
    if MESSAGE_TABLE not in table_names:
        return

    has_audit_tables = AUDIT_RECORD_TABLE in table_names and AUDIT_TOOL_DETAIL_TABLE in table_names
    last_message_id = 0
    while True:
        result = await session.execute(
            text(
                f"""
                SELECT id, content
                FROM {MESSAGE_TABLE}
                WHERE id > :last_message_id
                  AND type IN (:message_type_name, :message_type_value)
                  AND content LIKE :tool_name_pattern
                ORDER BY id
                LIMIT :batch_size
                """
            ),
            {
                "last_message_id": last_message_id,
                "message_type_name": "TOOL_CALL",
                "message_type_value": "tool_call",
                "tool_name_pattern": f"%{OLD_TOOL_NAME}%",
                "batch_size": MIGRATION_BATCH_SIZE,
            },
        )
        rows = result.mappings().all()
        if not rows:
            break

        for row in rows:
            last_message_id = int(row["id"])
            content = row["content"]
            if not isinstance(content, str):
                continue
            try:
                payload = json.loads(content)
            except (TypeError, ValueError):
                continue
            if not _rename_tool_calls(payload):
                continue

            await session.execute(
                text(f"UPDATE {MESSAGE_TABLE} SET content = :content WHERE id = :message_id"),
                {
                    "content": _canonical_json_dumps(payload),
                    "message_id": row["id"],
                },
            )

            tool_calls = payload.get("tool_calls")
            if has_audit_tables and isinstance(tool_calls, list):
                await _migrate_audit_snapshot(
                    session,
                    source_message_id=int(row["id"]),
                    tool_calls=[item for item in tool_calls if isinstance(item, dict)],
                )
