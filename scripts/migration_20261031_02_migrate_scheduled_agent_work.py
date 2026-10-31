from typing import Any

from sqlalchemy import JSON, Column, Integer, MetaData, String, Table, Text, inspect, select, text, update
from sqlalchemy.dialects.mysql import ENUM as MySQLEnum
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261031_02_migrate_scheduled_agent_work"

_WORK_ITEM = "session_reply_work_item"
_MESSAGE = "message"
_BATCH_SIZE = 200
_OLD_WORK_TYPES = {"SCHEDULED_TASK_SUMMARY", "scheduled_task_summary"}
_SCHEDULED_SOURCE_TYPES = {"SCHEDULED_TASK_RUN", "scheduled_task_run"}
_ACTIVE_STATUSES = {
    "READY_FOR_LLM",
    "RUNNING",
    "WAITING_EXTERNAL_WORK",
    "ready_for_llm",
    "running",
    "waiting_external_work",
}

_METADATA = MetaData()
_WORK = Table(
    _WORK_ITEM,
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
)
_MESSAGE_TABLE = Table(
    _MESSAGE,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("profile_id", Integer),
    Column("role", String(20)),
    Column("type", String(40)),
    Column("content", Text),
)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _is_role(item: Any, role: str) -> bool:
    if not isinstance(item, dict):
        return False
    value = item.get("role")
    return isinstance(value, str) and value.lower() == role


def _mysql_enum_columns(connection) -> dict[str, tuple[list[str], bool]]:
    columns: dict[str, tuple[list[str], bool]] = {}
    for column in inspect(connection).get_columns(_WORK_ITEM):
        column_type = column.get("type")
        if isinstance(column_type, MySQLEnum):
            columns[column["name"]] = (list(column_type.enums), bool(column.get("nullable")))
    return columns


def _mysql_enum_definition(values: list[str], *, nullable: bool) -> str:
    escaped = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"ENUM({escaped}) {'NULL' if nullable else 'NOT NULL'}"


async def _expand_mysql_enums(session: AsyncSession) -> tuple[list[str] | None, bool]:
    connection = await session.connection()
    if connection.dialect.name != "mysql":
        return None, False

    enum_columns = await connection.run_sync(_mysql_enum_columns)
    work_enum = enum_columns.get("work_type")
    source_enum = enum_columns.get("source_type")
    original_work_values = list(work_enum[0]) if work_enum is not None else None

    if work_enum is not None and "AGENT_WORK" not in work_enum[0]:
        expanded_work_values = [*work_enum[0], "AGENT_WORK"]
        await session.execute(
            text(
                "ALTER TABLE session_reply_work_item MODIFY COLUMN work_type "
                + _mysql_enum_definition(expanded_work_values, nullable=work_enum[1])
            )
        )

    if source_enum is not None and "PARENT_WORK" not in source_enum[0]:
        expanded_source_values = [*source_enum[0], "PARENT_WORK"]
        await session.execute(
            text(
                "ALTER TABLE session_reply_work_item MODIFY COLUMN source_type "
                + _mysql_enum_definition(expanded_source_values, nullable=source_enum[1])
            )
        )

    return original_work_values, work_enum[1] if work_enum is not None else False


async def _tighten_mysql_work_enum(
    session: AsyncSession,
    original_work_values: list[str] | None,
    *,
    nullable: bool,
) -> None:
    if original_work_values is None:
        return
    final_values = [value for value in original_work_values if value not in _OLD_WORK_TYPES]
    if "AGENT_WORK" not in final_values:
        final_values.append("AGENT_WORK")
    await session.execute(
        text(
            "ALTER TABLE session_reply_work_item MODIFY COLUMN work_type "
            + _mysql_enum_definition(final_values, nullable=nullable)
        )
    )


def _strip_checkpoint_history(
    checkpoint: dict[str, Any],
    *,
    instruction_message_id: int,
    active: bool,
    work_id: int,
) -> dict[str, Any]:
    migrated = dict(checkpoint)
    messages = migrated.get("messages")
    if not isinstance(messages, list) or not messages:
        return migrated

    trigger_index = next(
        (
            index
            for index, item in enumerate(messages)
            if isinstance(item, dict)
            and item.get("id") == instruction_message_id
            and _is_role(item, "user")
        ),
        None,
    )
    if trigger_index is None:
        if active:
            raise RuntimeError(
                f"cannot isolate active scheduled work {work_id}: trigger message is missing from dispatcher checkpoint"
            )
        migrated["messages"] = []
        migrated["turn_messages"] = []
        return migrated

    system_prefix = [item for item in messages[:trigger_index] if _is_role(item, "system")]
    migrated["messages"] = [*system_prefix, *messages[trigger_index:]]
    return migrated


async def migrate(session: AsyncSession) -> None:
    original_mysql_work_values, mysql_work_nullable = await _expand_mysql_enums(session)
    last_id = 0
    while True:
        rows = (
            await session.execute(
                select(
                    _WORK.c.id,
                    _WORK.c.uid,
                    _WORK.c.session_id,
                    _WORK.c.profile_id,
                    _WORK.c.source_type,
                    _WORK.c.source_id,
                    _WORK.c.status,
                    _WORK.c.execution_state,
                )
                .where(
                    _WORK.c.id > last_id,
                    _WORK.c.work_type.in_(sorted(_OLD_WORK_TYPES)),
                )
                .order_by(_WORK.c.id.asc())
                .limit(_BATCH_SIZE)
            )
        ).mappings().all()
        if not rows:
            break

        trigger_ids = {_parse_positive_int(row["source_id"]) for row in rows}
        trigger_ids.discard(None)
        message_rows = (
            await session.execute(
                select(
                    _MESSAGE_TABLE.c.id,
                    _MESSAGE_TABLE.c.uid,
                    _MESSAGE_TABLE.c.session_id,
                    _MESSAGE_TABLE.c.profile_id,
                    _MESSAGE_TABLE.c.role,
                    _MESSAGE_TABLE.c.type,
                    _MESSAGE_TABLE.c.content,
                ).where(_MESSAGE_TABLE.c.id.in_(sorted(trigger_ids)))
            )
        ).mappings().all()
        messages_by_id = {row["id"]: row for row in message_rows}

        for row in rows:
            work_id = row["id"]
            trigger_id = _parse_positive_int(row["source_id"])
            if not _positive_int(work_id) or not _positive_int(trigger_id):
                raise RuntimeError(f"scheduled work has invalid identity: work_id={work_id!r}, source_id={row['source_id']!r}")
            if row["source_type"] not in _SCHEDULED_SOURCE_TYPES:
                raise RuntimeError(f"scheduled work {work_id} has unexpected source_type={row['source_type']!r}")

            trigger = messages_by_id.get(trigger_id)
            if (
                trigger is None
                or trigger["uid"] != row["uid"]
                or trigger["session_id"] != row["session_id"]
                or trigger["profile_id"] != row["profile_id"]
                or str(trigger["role"]).lower() != "user"
                or str(trigger["type"]).lower() != "scheduled_task_trigger"
                or not isinstance(trigger["content"], str)
                or not trigger["content"].strip()
            ):
                raise RuntimeError(f"scheduled work {work_id} has no valid trigger message {trigger_id}")

            state = dict(row["execution_state"]) if isinstance(row["execution_state"], dict) else {}
            checkpoint = state.get("dispatcher_checkpoint")
            if isinstance(checkpoint, dict):
                state["dispatcher_checkpoint"] = _strip_checkpoint_history(
                    checkpoint,
                    instruction_message_id=trigger_id,
                    active=row["status"] in _ACTIVE_STATUSES,
                    work_id=work_id,
                )
            state["execution_scope"] = "task"
            state["frozen_context_messages"] = []
            state["agent_work"] = {
                "instruction": trigger["content"],
                "execution_scope": "task",
                "report_target": "session",
                "parent_work_id": None,
                "instruction_message_id": trigger_id,
            }

            await session.execute(
                update(_WORK)
                .where(
                    _WORK.c.id == work_id,
                    _WORK.c.work_type.in_(sorted(_OLD_WORK_TYPES)),
                )
                .values(
                    work_type="AGENT_WORK",
                    execution_state=state,
                )
            )

        last_id = int(rows[-1]["id"])

    await _tighten_mysql_work_enum(
        session,
        original_mysql_work_values,
        nullable=mysql_work_nullable,
    )
