from typing import Any

from sqlalchemy import JSON, Boolean, Column, DateTime, Integer, MetaData, String, Table, func, inspect, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.utils.time import get_local_time

MIGRATION_ID = "20261030_01_finish_stopped_terminal_waits"

_BATCH_SIZE = 100
_WAIT_KEYS = ("pending_tools", "terminal_watch_waits", "external_resume_context")
_METADATA = MetaData()
_WORK = Table(
    "session_reply_work_item",
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("profile_id", Integer),
    Column("status", String(40)),
    Column("result_message_id", Integer),
    Column("execution_state", JSON),
    Column("event_sent", Boolean),
    Column("locked_by", String(100)),
    Column("lock_until", Integer),
    Column("updated_at", DateTime(timezone=True)),
)
_TASK = Table(
    "background_task",
    _METADATA,
    Column("id", Integer),
    Column("uid", String(100)),
    Column("session_id", String(100)),
    Column("profile_id", Integer),
    Column("tool_name", String(100)),
    Column("status", String(30)),
)


def _table_columns(sync_connection) -> dict[str, set[str]]:
    inspector = inspect(sync_connection)
    table_names = set(inspector.get_table_names())
    return {table_name: {str(column["name"]) for column in inspector.get_columns(table_name)} for table_name in ("session_reply_work_item", "background_task") if table_name in table_names}


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


async def _stopped_task_ids(session: AsyncSession, row: Any) -> set[int]:
    result = await session.execute(
        select(_WORK.c.execution_state).where(
            _WORK.c.uid == row["uid"],
            _WORK.c.session_id == row["session_id"],
            _WORK.c.profile_id == row["profile_id"],
            func.lower(_WORK.c.status) == "succeeded",
            _WORK.c.event_sent.is_(True),
            _WORK.c.result_message_id.is_not(None),
        )
    )
    requested: set[int] = set()
    for state in result.scalars():
        if isinstance(state, dict) and isinstance(state.get("stopped_task_ids"), list):
            requested.update(task_id for task_id in state["stopped_task_ids"] if _positive_int(task_id))
    if not requested:
        return set()

    result = await session.execute(
        select(_TASK.c.id).where(
            _TASK.c.id.in_(requested),
            _TASK.c.uid == row["uid"],
            _TASK.c.session_id == row["session_id"],
            _TASK.c.profile_id == row["profile_id"],
            _TASK.c.tool_name == "terminal_watch",
            func.lower(_TASK.c.status).in_(("succeeded", "failed", "cancelled")),
        )
    )
    return {task_id for task_id in result.scalars() if _positive_int(task_id)}


def _clean_list(values: list[Any], stopped_ids: set[int]) -> tuple[list[Any], bool]:
    remaining: list[Any] = []
    changed = False
    for value in values:
        task_id = value.get("task_id") if isinstance(value, dict) else value
        if _positive_int(task_id) and task_id in stopped_ids:
            changed = True
        else:
            remaining.append(value)
    return remaining, changed


def _clean_context(values: list[Any], stopped_ids: set[int]) -> tuple[list[Any], bool]:
    remaining: list[Any] = []
    changed = False
    for value in values:
        if not isinstance(value, dict):
            remaining.append(value)
            continue
        tasks = value.get("tasks")
        if not isinstance(tasks, list):
            task_id = value.get("task_id")
            if _positive_int(task_id) and task_id in stopped_ids:
                changed = True
            else:
                remaining.append(value)
            continue
        remaining_tasks = [item for item in tasks if not (isinstance(item, dict) and _positive_int(item.get("task_id")) and item["task_id"] in stopped_ids)]
        top_task_id = value.get("task_id")
        top_task_stopped = _positive_int(top_task_id) and top_task_id in stopped_ids
        if top_task_stopped and not remaining_tasks:
            changed = True
            continue
        if not top_task_stopped and len(remaining_tasks) == len(tasks):
            remaining.append(value)
            continue
        updated = dict(value)
        updated["tasks"] = remaining_tasks
        if top_task_stopped:
            first_remaining_payload = next(
                (item for item in remaining_tasks if isinstance(item, dict) and _positive_int(item.get("task_id"))),
                None,
            )
            if first_remaining_payload is not None:
                updated = {**value, **first_remaining_payload}
                updated["tasks"] = remaining_tasks
        remaining.append(updated)
        changed = True
    return remaining, changed


def _clean_state(state: dict[str, Any], stopped_ids: set[int]) -> tuple[dict[str, Any], bool, bool]:
    updated = dict(state)
    changed = False
    has_remaining = False
    for key in ("pending_tools", "terminal_watch_waits"):
        values = state.get(key)
        if not isinstance(values, list):
            has_remaining = has_remaining or key in state
            continue
        remaining, list_changed = _clean_list(values, stopped_ids)
        if remaining:
            updated[key] = remaining
            has_remaining = True
        else:
            updated.pop(key, None)
        changed = changed or list_changed

    context = state.get("external_resume_context")
    if not isinstance(context, list):
        has_remaining = has_remaining or "external_resume_context" in state
    else:
        remaining_context, context_changed = _clean_context(context, stopped_ids)
        if remaining_context:
            updated["external_resume_context"] = remaining_context
            has_remaining = True
        else:
            updated.pop("external_resume_context", None)
        changed = changed or context_changed

    auto_task_ids = state.get("terminal_auto_watch_task_ids")
    if isinstance(auto_task_ids, list):
        remaining_auto_task_ids = [task_id for task_id in auto_task_ids if not (_positive_int(task_id) and task_id in stopped_ids)]
        if remaining_auto_task_ids:
            updated["terminal_auto_watch_task_ids"] = remaining_auto_task_ids
            has_remaining = True
        else:
            updated.pop("terminal_auto_watch_task_ids", None)
        changed = changed or len(remaining_auto_task_ids) != len(auto_task_ids)
    elif "terminal_auto_watch_task_ids" in state:
        has_remaining = True

    if not updated.get("external_resume_context"):
        updated.pop("external_resume_pending", None)
        updated.pop("terminal_auto_resume", None)
        checkpoint = updated.get("dispatcher_checkpoint")
        if isinstance(checkpoint, dict):
            checkpoint = dict(checkpoint)
            checkpoint.pop("deferred_external_results", None)
            updated["dispatcher_checkpoint"] = checkpoint

    return updated, changed, has_remaining


async def migrate(session: AsyncSession) -> None:
    connection = await session.connection()
    columns = await connection.run_sync(_table_columns)
    required = {
        "session_reply_work_item": {"id", "uid", "session_id", "profile_id", "status", "result_message_id", "execution_state", "event_sent", "locked_by", "lock_until", "updated_at"},
        "background_task": {"id", "uid", "session_id", "profile_id", "tool_name", "status"},
    }
    if any(not required_table.issubset(columns.get(table_name, set())) for table_name, required_table in required.items()):
        return

    last_id: int | None = None
    while True:
        statement = (
            select(
                _WORK.c.id,
                _WORK.c.uid,
                _WORK.c.session_id,
                _WORK.c.profile_id,
                _WORK.c.execution_state,
            )
            .where(func.lower(_WORK.c.status) == "waiting_external_work")
            .order_by(_WORK.c.id.asc())
            .limit(_BATCH_SIZE)
        )
        if last_id is not None:
            statement = statement.where(_WORK.c.id > last_id)
        rows = (await session.execute(statement)).mappings().all()
        if not rows:
            return
        for row in rows:
            state = row["execution_state"]
            if not isinstance(state, dict):
                continue
            stopped_ids = await _stopped_task_ids(session, row)
            if not stopped_ids:
                continue
            updated_state, changed, has_remaining = _clean_state(state, stopped_ids)
            if not changed:
                continue
            values: dict[str, Any] = {"execution_state": updated_state, "updated_at": get_local_time()}
            if not has_remaining:
                values.update(status="CANCELLED", locked_by=None, lock_until=None)
            await session.execute(
                update(_WORK)
                .where(
                    _WORK.c.id == row["id"],
                    _WORK.c.uid == row["uid"],
                    _WORK.c.session_id == row["session_id"],
                    _WORK.c.profile_id == row["profile_id"],
                    func.lower(_WORK.c.status) == "waiting_external_work",
                )
                .values(**values)
            )
        last_id = rows[-1]["id"]
