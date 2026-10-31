import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import JSON, Column, DateTime, Integer, MetaData, String, Table, Text, inspect, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

MIGRATION_ID = "20261031_01_split_background_reply_modes"

_BACKGROUND_TASK = "background_task"
_WORK_ITEM = "session_reply_work_item"
_AUDIT_RECORD = "audit_record"
_MESSAGE = "message"
_BATCH_SIZE = 200

_METADATA = MetaData()
_TASK = Table(
    _BACKGROUND_TASK,
    _METADATA,
    Column("id", Integer),
    Column("auto_reply", Integer),
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
    Column("dedupe_key", String(160)),
    Column("status", String(30)),
    Column("execution_state", JSON),
    Column("created_at", DateTime(timezone=True)),
)

_AUDIT = Table(
    _AUDIT_RECORD,
    _METADATA,
    Column("id", Integer),
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


def _table_columns(sync_connection) -> dict[str, set[str]]:
    inspector = inspect(sync_connection)
    names = set(inspector.get_table_names())
    return {
        table: {str(column["name"]) for column in inspector.get_columns(table)}
        for table in (_BACKGROUND_TASK, _WORK_ITEM, _AUDIT_RECORD, _MESSAGE)
        if table in names
    }


def _table_index_names(sync_connection, table_name: str) -> set[str]:
    inspector = inspect(sync_connection)
    if table_name not in set(inspector.get_table_names()):
        return set()
    return {
        str(index["name"])
        for index in inspector.get_indexes(table_name)
        if index.get("name")
    }


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _reply_mode(extra: dict[str, Any]) -> str:
    if extra.get("auto_terminal_watch") is True:
        return "resume_agent"
    if _positive_int(extra.get("origin_work_id")):
        return "resume_origin"
    return "summary"


async def _migrate_background_tasks(session: AsyncSession) -> set[int]:
    resume_agent_task_ids: set[int] = set()
    last_id = 0
    while True:
        rows = (
            await session.execute(
                select(_TASK.c.id, _TASK.c.extra)
                .where(_TASK.c.id > last_id)
                .order_by(_TASK.c.id.asc())
                .limit(_BATCH_SIZE)
            )
        ).mappings().all()
        if not rows:
            return resume_agent_task_ids
        for row in rows:
            task_id = row["id"]
            if not _positive_int(task_id):
                continue
            extra = dict(row["extra"]) if isinstance(row["extra"], dict) else {}
            changed = False
            if "request_context" not in extra and isinstance(extra.get("submission_context"), list):
                extra["request_context"] = extra["submission_context"]
                changed = True
            if "submission_context" in extra:
                extra.pop("submission_context", None)
                changed = True
            mode = _reply_mode(extra)
            if mode == "resume_agent":
                resume_agent_task_ids.add(task_id)
            if extra.get("reply_mode") != mode:
                extra["reply_mode"] = mode
                changed = True
            if changed:
                await session.execute(update(_TASK).where(_TASK.c.id == task_id).values(extra=extra))
        last_id = int(rows[-1]["id"])


async def _migrate_resume_work_items(session: AsyncSession, task_ids: set[int]) -> None:
    last_id = 0
    active_statuses = {"ready_for_llm", "running", "waiting_external_work"}
    interactive_state_keys = {
        "dispatcher_checkpoint",
        "external_resume_context",
        "external_resume_pending",
        "active_audit_execution",
        "terminal_auto_resume",
        "terminal_auto_watch_group",
    }
    while True:
        rows = (
            await session.execute(
                select(
                    _WORK.c.id,
                    _WORK.c.source_id,
                    _WORK.c.status,
                    _WORK.c.execution_state,
                )
                .where(
                    _WORK.c.id > last_id,
                    _WORK.c.work_type == "background_tool_summary",
                    _WORK.c.source_type == "background_task",
                )
                .order_by(_WORK.c.id.asc())
                .limit(_BATCH_SIZE)
            )
        ).mappings().all()
        if not rows:
            return
        for row in rows:
            work_id = row["id"]
            source_id = row["source_id"]
            status = str(row["status"] or "").lower()
            state = row["execution_state"] if isinstance(row["execution_state"], dict) else {}
            try:
                task_id = int(source_id)
            except (TypeError, ValueError):
                task_id = None
            should_resume = (
                status in active_statuses
                and (
                    task_id in task_ids
                    or any(key in state for key in interactive_state_keys)
                )
            )
            if not should_resume or not _positive_int(work_id) or not _positive_int(task_id):
                continue
            await session.execute(
                update(_WORK)
                .where(
                    _WORK.c.id == work_id,
                    _WORK.c.work_type == "background_tool_summary",
                )
                .values(
                    work_type="background_task_resume",
                    dedupe_key=f"background-task-resume:{task_id}",
                )
            )
        last_id = int(rows[-1]["id"])


def _work_result_dedupe_key(row: Any) -> str:
    created_at = row["created_at"]
    created_text = (
        created_at.replace(tzinfo=None).isoformat(timespec="microseconds")
        if isinstance(created_at, datetime)
        else str(created_at)
    )
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


async def _migrate_scheduled_audit_scopes(
    session: AsyncSession,
    *,
    include_confirmation_messages: bool,
) -> None:
    last_id = 0
    while True:
        rows = (
            await session.execute(
                select(
                    _WORK.c.id,
                    _WORK.c.uid,
                    _WORK.c.session_id,
                    _WORK.c.profile_id,
                    _WORK.c.dedupe_key,
                    _WORK.c.execution_state,
                    _WORK.c.created_at,
                )
                .where(
                    _WORK.c.id > last_id,
                    _WORK.c.work_type == "scheduled_task_summary",
                )
                .order_by(_WORK.c.id.asc())
                .limit(_BATCH_SIZE)
            )
        ).mappings().all()
        if not rows:
            return

        audit_record_ids: set[int] = set()
        source_work_by_audit_record_id: dict[int, int] = {}
        conflicting_source_work_ids: set[int] = set()
        confirmation_work_by_key: dict[str, Any] = {}

        def bind_source_work(audit_record_id: Any, work_id: Any) -> None:
            if not _positive_int(audit_record_id) or not _positive_int(work_id):
                return
            audit_record_ids.add(audit_record_id)
            existing_work_id = source_work_by_audit_record_id.get(audit_record_id)
            if existing_work_id is None:
                source_work_by_audit_record_id[audit_record_id] = work_id
            elif existing_work_id != work_id:
                conflicting_source_work_ids.add(audit_record_id)

        for row in rows:
            state = row["execution_state"] if isinstance(row["execution_state"], dict) else {}
            binding = state.get("active_audit_execution")
            audit_record_id = binding.get("audit_record_id") if isinstance(binding, dict) else None
            bind_source_work(audit_record_id, row["id"])
            if include_confirmation_messages:
                confirmation_work_by_key[_work_result_dedupe_key(row)] = row

        if confirmation_work_by_key:
            message_rows = (
                await session.execute(
                    select(
                        _MESSAGE_TABLE.c.uid,
                        _MESSAGE_TABLE.c.session_id,
                        _MESSAGE_TABLE.c.profile_id,
                        _MESSAGE_TABLE.c.content,
                        _MESSAGE_TABLE.c.dedupe_key,
                    ).where(
                        _MESSAGE_TABLE.c.type == "audit_confirmation",
                        _MESSAGE_TABLE.c.dedupe_key.in_(list(confirmation_work_by_key)),
                    )
                )
            ).mappings().all()
            for message_row in message_rows:
                work_row = confirmation_work_by_key.get(message_row["dedupe_key"])
                if work_row is None:
                    continue
                if (
                    message_row["uid"] != work_row["uid"]
                    or message_row["session_id"] != work_row["session_id"]
                    or message_row["profile_id"] != work_row["profile_id"]
                ):
                    continue
                audit_record_id = _confirmation_audit_record_id(message_row["content"])
                bind_source_work(audit_record_id, work_row["id"])

        if audit_record_ids:
            await session.execute(
                update(_AUDIT)
                .where(_AUDIT.c.id.in_(sorted(audit_record_ids)))
                .values(execution_scope="task")
            )
            for audit_record_id, source_work_id in source_work_by_audit_record_id.items():
                if audit_record_id in conflicting_source_work_ids:
                    continue
                await session.execute(
                    update(_AUDIT)
                    .where(_AUDIT.c.id == audit_record_id)
                    .values(source_work_id=source_work_id)
                )
        last_id = int(rows[-1]["id"])


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    columns = await connection.run_sync(_table_columns)

    audit_columns = columns.get(_AUDIT_RECORD)
    if audit_columns is not None and "execution_scope" not in audit_columns:
        await session.execute(text("ALTER TABLE audit_record ADD COLUMN execution_scope VARCHAR(20) NOT NULL DEFAULT 'foreground'"))
        audit_columns.add("execution_scope")
    if audit_columns is not None and "source_work_id" not in audit_columns:
        await session.execute(text("ALTER TABLE audit_record ADD COLUMN source_work_id INTEGER"))
        audit_columns.add("source_work_id")
    if audit_columns is not None:
        audit_index_names = await connection.run_sync(_table_index_names, _AUDIT_RECORD)
        if "ix_audit_record_source_work_id" not in audit_index_names:
            await session.execute(text("CREATE INDEX ix_audit_record_source_work_id ON audit_record (source_work_id)"))

    work_columns = columns.get(_WORK_ITEM, set())
    message_columns = columns.get(_MESSAGE, set())
    scheduled_scope_columns = {
        "id",
        "uid",
        "session_id",
        "profile_id",
        "work_type",
        "dedupe_key",
        "execution_state",
        "created_at",
    }
    confirmation_message_columns = {
        "uid",
        "session_id",
        "profile_id",
        "type",
        "content",
        "dedupe_key",
    }
    if audit_columns is not None and scheduled_scope_columns.issubset(work_columns):
        await _migrate_scheduled_audit_scopes(
            session,
            include_confirmation_messages=confirmation_message_columns.issubset(message_columns),
        )

    task_columns = columns.get(_BACKGROUND_TASK, set())
    resume_work_columns = {
        "id",
        "work_type",
        "source_type",
        "source_id",
        "dedupe_key",
        "status",
        "execution_state",
    }
    if {"id", "extra"}.issubset(task_columns) and resume_work_columns.issubset(work_columns):
        resume_agent_task_ids = await _migrate_background_tasks(session)
        await _migrate_resume_work_items(session, resume_agent_task_ids)
