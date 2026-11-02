import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    inspect,
    or_,
    select,
    text,
    update,
)
from sqlalchemy.dialects.mysql import ENUM as MySQLEnum
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261102_01_repair_legacy_reply_work_items"

_BACKGROUND_TASK = "background_task"
_WORK_ITEM = "session_reply_work_item"
_AUDIT_RECORD = "audit_record"
_MESSAGE = "message"
_BATCH_SIZE = 200

_BACKGROUND_SUMMARY_TYPES = {
    "background_tool_summary",
    "BACKGROUND_TOOL_SUMMARY",
}
_LEGACY_RESUME_WORK_TYPE = "background_task_resume"
_BACKGROUND_SOURCE_TYPES = {"background_task", "BACKGROUND_TASK"}
_SCHEDULED_WORK_TYPES = {"AGENT_WORK", "agent_work"}
_SCHEDULED_SOURCE_TYPES = {"SCHEDULED_TASK_RUN", "scheduled_task_run"}
_ACTIVE_STATUSES = {
    "READY_FOR_LLM",
    "RUNNING",
    "WAITING_EXTERNAL_WORK",
}
_INTERACTIVE_STATE_KEYS = {
    "dispatcher_checkpoint",
    "external_resume_context",
    "external_resume_pending",
    "active_audit_execution",
    "terminal_auto_resume",
    "terminal_auto_watch_group",
}
_CONFIRMATION_MESSAGE_TYPES = {"AUDIT_CONFIRMATION", "audit_confirmation"}

_METADATA = MetaData()
_TASK = Table(
    _BACKGROUND_TASK,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("profile_id", Integer),
    Column("extra", JSON),
)
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
    Column("dedupe_key", String(160)),
    Column("execution_state", JSON),
    Column("created_at", DateTime(timezone=True)),
    Column("result_message_id", Integer),
)
_AUDIT = Table(
    _AUDIT_RECORD,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("execution_scope", String(20)),
    Column("source_work_id", Integer),
)
_MESSAGE_TABLE = Table(
    _MESSAGE,
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("profile_id", Integer),
    Column("type", String(40)),
    Column("content", Text),
    Column("dedupe_key", String(64)),
)


def _table_names(sync_connection) -> set[str]:
    return set(inspect(sync_connection).get_table_names())


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = int(value.strip())
    except ValueError:
        return None
    return parsed if parsed > 0 else None


def _mysql_work_enum(sync_connection) -> tuple[list[str], bool] | None:
    inspector = inspect(sync_connection)
    if _WORK_ITEM not in set(inspector.get_table_names()):
        return None
    for column in inspector.get_columns(_WORK_ITEM):
        if column.get("name") != "work_type":
            continue
        column_type = column.get("type")
        if isinstance(column_type, MySQLEnum):
            return list(column_type.enums), bool(column.get("nullable"))
    return None


def _mysql_enum_definition(values: list[str], *, nullable: bool) -> str:
    escaped = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"ENUM({escaped}) {'NULL' if nullable else 'NOT NULL'}"


async def _expand_mysql_work_enum(session: AsyncSession) -> None:
    connection = await session.connection()
    if connection.dialect.name != "mysql":
        return

    work_enum = await connection.run_sync(_mysql_work_enum)
    if work_enum is None:
        return
    values, nullable = work_enum
    if "BACKGROUND_TASK_RESUME" in values:
        return
    expanded_values = [*values, "BACKGROUND_TASK_RESUME"]
    await session.execute(text("ALTER TABLE session_reply_work_item MODIFY COLUMN work_type " + _mysql_enum_definition(expanded_values, nullable=nullable)))


def _work_result_dedupe_key(row: Any) -> str:
    created_at = row["created_at"]
    created_text = created_at.replace(tzinfo=None).isoformat(timespec="microseconds") if isinstance(created_at, datetime) else str(created_at)
    state = row["execution_state"] if isinstance(row["execution_state"], dict) else {}
    payload: dict[str, Any] = {
        "dedupe_key": row["dedupe_key"],
        "created_at": created_text,
    }
    generation = state.get("terminal_resume_generation")
    if _positive_int(generation):
        payload["terminal_resume_generation"] = generation
    identity = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:32]
    return f"session-reply-work:{identity}:result"


def _confirmation_audit_record_id(content: Any) -> int | None:
    if isinstance(content, str):
        try:
            content = json.loads(content)
        except (TypeError, ValueError):
            return None
    if not isinstance(content, dict) or content.get("type") != "audit_confirmation":
        return None
    audit_record_id = content.get("audit_record_id")
    return audit_record_id if _positive_int(audit_record_id) else None


async def _migrate_background_work(session: AsyncSession) -> None:
    last_id = 0
    work_types = [*sorted(_BACKGROUND_SUMMARY_TYPES), _LEGACY_RESUME_WORK_TYPE]
    source_types = sorted(_BACKGROUND_SOURCE_TYPES)
    while True:
        rows = (
            (
                await session.execute(
                    select(
                        _WORK.c.id,
                        _WORK.c.uid,
                        _WORK.c.session_id,
                        _WORK.c.profile_id,
                        _WORK.c.work_type,
                        _WORK.c.source_type,
                        _WORK.c.source_id,
                        _WORK.c.status,
                        _WORK.c.dedupe_key,
                        _WORK.c.execution_state,
                    )
                    .where(
                        _WORK.c.id > last_id,
                        _WORK.c.work_type.in_(work_types),
                        _WORK.c.source_type.in_(source_types),
                    )
                    .order_by(_WORK.c.id.asc())
                    .limit(_BATCH_SIZE)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return

        task_ids = {task_id for task_id in (_parse_positive_int(row["source_id"]) for row in rows) if task_id is not None}
        tasks_by_id: dict[int, Any] = {}
        if task_ids:
            task_rows = (
                (
                    await session.execute(
                        select(
                            _TASK.c.id,
                            _TASK.c.uid,
                            _TASK.c.session_id,
                            _TASK.c.profile_id,
                            _TASK.c.extra,
                        ).where(_TASK.c.id.in_(sorted(task_ids)))
                    )
                )
                .mappings()
                .all()
            )
            tasks_by_id = {row["id"]: row for row in task_rows}

        for row in rows:
            work_id = row["id"]
            if not _positive_int(work_id):
                continue
            task_id = _parse_positive_int(row["source_id"])
            task = tasks_by_id.get(task_id)
            if task is None or task["uid"] != row["uid"] or task["session_id"] != row["session_id"] or task["profile_id"] != row["profile_id"]:
                continue

            work_type = row["work_type"]
            source_type = row["source_type"]
            values: dict[str, Any] = {}
            if work_type == _LEGACY_RESUME_WORK_TYPE:
                values["work_type"] = "BACKGROUND_TASK_RESUME"
                if source_type != "BACKGROUND_TASK":
                    values["source_type"] = "BACKGROUND_TASK"
                status = row["status"]
                if isinstance(status, str) and status != status.upper():
                    values["status"] = status.upper()
            else:
                status = row["status"]
                status_upper = str(status or "").upper()
                if status_upper not in _ACTIVE_STATUSES:
                    continue
                state = row["execution_state"] if isinstance(row["execution_state"], dict) else {}
                extra = task["extra"] if isinstance(task["extra"], dict) else {}
                if not (extra.get("auto_terminal_watch") is True or extra.get("reply_mode") == "resume_agent" or any(key in state for key in _INTERACTIVE_STATE_KEYS)):
                    continue
                values["work_type"] = "BACKGROUND_TASK_RESUME"
                values["dedupe_key"] = f"background-task-resume:{task_id}"
                if source_type != "BACKGROUND_TASK":
                    values["source_type"] = "BACKGROUND_TASK"
                if row["status"] != status_upper:
                    values["status"] = status_upper

            await session.execute(update(_WORK).where(_WORK.c.id == work_id, _WORK.c.work_type == work_type).values(**values))
        last_id = int(rows[-1]["id"])


async def _migrate_scheduled_audit_scopes(
    session: AsyncSession,
    *,
    include_confirmation_messages: bool,
) -> None:
    last_id = 0
    while True:
        rows = (
            (
                await session.execute(
                    select(
                        _WORK.c.id,
                        _WORK.c.uid,
                        _WORK.c.session_id,
                        _WORK.c.profile_id,
                        _WORK.c.work_type,
                        _WORK.c.source_type,
                        _WORK.c.dedupe_key,
                        _WORK.c.execution_state,
                        _WORK.c.created_at,
                        _WORK.c.result_message_id,
                    )
                    .where(
                        _WORK.c.id > last_id,
                        _WORK.c.work_type.in_(sorted(_SCHEDULED_WORK_TYPES)),
                        _WORK.c.source_type.in_(sorted(_SCHEDULED_SOURCE_TYPES)),
                    )
                    .order_by(_WORK.c.id.asc())
                    .limit(_BATCH_SIZE)
                )
            )
            .mappings()
            .all()
        )
        if not rows:
            return

        candidate_bindings: set[tuple[int, int]] = set()
        work_by_id = {row["id"]: row for row in rows if _positive_int(row["id"])}

        def add_candidate(audit_record_id: Any, work_id: Any) -> None:
            if not _positive_int(audit_record_id) or not _positive_int(work_id):
                return
            candidate_bindings.add((audit_record_id, work_id))

        confirmation_work_by_id: dict[int, list[Any]] = {}
        confirmation_work_by_key: dict[str, list[Any]] = {}
        for row in rows:
            work_id = row["id"]
            if not _positive_int(work_id):
                continue
            state = row["execution_state"] if isinstance(row["execution_state"], dict) else {}
            binding = state.get("active_audit_execution")
            if isinstance(binding, dict):
                add_candidate(binding.get("audit_record_id"), work_id)
            if include_confirmation_messages:
                result_message_id = _parse_positive_int(row["result_message_id"])
                if result_message_id is not None:
                    confirmation_work_by_id.setdefault(result_message_id, []).append(row)
                confirmation_work_by_key.setdefault(_work_result_dedupe_key(row), []).append(row)

        if include_confirmation_messages and confirmation_work_by_key:
            message_conditions = [
                _MESSAGE_TABLE.c.dedupe_key.in_(list(confirmation_work_by_key)),
            ]
            if confirmation_work_by_id:
                message_conditions.append(_MESSAGE_TABLE.c.id.in_(list(confirmation_work_by_id)))
            message_rows = (
                (
                    await session.execute(
                        select(
                            _MESSAGE_TABLE.c.id,
                            _MESSAGE_TABLE.c.uid,
                            _MESSAGE_TABLE.c.session_id,
                            _MESSAGE_TABLE.c.profile_id,
                            _MESSAGE_TABLE.c.content,
                            _MESSAGE_TABLE.c.dedupe_key,
                        ).where(
                            _MESSAGE_TABLE.c.type.in_(sorted(_CONFIRMATION_MESSAGE_TYPES)),
                            or_(*message_conditions),
                        )
                    )
                )
                .mappings()
                .all()
            )
            for message_row in message_rows:
                candidate_rows = [
                    *confirmation_work_by_id.get(message_row["id"], []),
                    *confirmation_work_by_key.get(message_row["dedupe_key"], []),
                ]
                for work_row in candidate_rows:
                    work_id = work_row["id"]
                    if message_row["uid"] != work_row["uid"] or message_row["session_id"] != work_row["session_id"] or message_row["profile_id"] != work_row["profile_id"]:
                        continue
                    add_candidate(_confirmation_audit_record_id(message_row["content"]), work_id)

        if candidate_bindings:
            audit_record_ids = {audit_id for audit_id, _ in candidate_bindings}
            audit_rows = (
                (
                    await session.execute(
                        select(
                            _AUDIT.c.id,
                            _AUDIT.c.uid,
                            _AUDIT.c.session_id,
                            _AUDIT.c.execution_scope,
                            _AUDIT.c.source_work_id,
                        ).where(_AUDIT.c.id.in_(sorted(audit_record_ids)))
                    )
                )
                .mappings()
                .all()
            )
            audits_by_id = {row["id"]: dict(row) for row in audit_rows}
            for audit_record_id, work_id in sorted(candidate_bindings):
                audit_row = audits_by_id.get(audit_record_id)
                work_row = work_by_id.get(work_id)
                if audit_row is None or work_row is None or audit_row["uid"] != work_row["uid"] or audit_row["session_id"] != work_row["session_id"]:
                    continue
                existing_source_work_id = audit_row["source_work_id"]
                if existing_source_work_id is not None and existing_source_work_id != work_id:
                    raise RuntimeError(f"audit record {audit_record_id} already references work {existing_source_work_id}, cannot bind work {work_id}")
                values: dict[str, Any] = {}
                if audit_row["execution_scope"] != "task":
                    values["execution_scope"] = "task"
                if existing_source_work_id != work_id:
                    values["source_work_id"] = work_id
                if values:
                    await session.execute(update(_AUDIT).where(_AUDIT.c.id == audit_record_id).values(**values))
                    audit_row["source_work_id"] = work_id
        last_id = int(rows[-1]["id"])


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    table_names = await connection.run_sync(_table_names)
    if _WORK_ITEM not in table_names:
        return

    await _expand_mysql_work_enum(session)

    if _BACKGROUND_TASK in table_names:
        await _migrate_background_work(session)

    if _AUDIT_RECORD in table_names:
        await _migrate_scheduled_audit_scopes(
            session,
            include_confirmation_messages=_MESSAGE in table_names,
        )
