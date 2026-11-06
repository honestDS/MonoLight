import json
from copy import deepcopy
from typing import Any

from sqlalchemy import delete, exists, inspect, text
from sqlalchemy.dialects.mysql import ENUM as MySQLEnum
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.utils.dispatcher.checkpoint_message import checkpoint_message_hash
from app.models.background_task import BackgroundTask
from app.models.message import Message, MessageRole, MessageType
from app.models.session_event import SessionEvent
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplyCheckpointMessage,
    SessionReplyCheckpointMessageReference,
    SessionReplyWorkItem,
)
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261106_01_isolate_subagent_result_messages"

_BATCH_SIZE = 200
_REFERENCE_KINDS = ("checkpoint", "frozen")
_INTERNAL_CONTEXT_TYPES = {MessageType.SUBAGENT_RESULT.name, MessageType.SUBAGENT_RESULT.value}


def _mysql_message_enum(sync_connection) -> tuple[list[str], bool] | None:
    for column in inspect(sync_connection).get_columns(Message.__tablename__):
        if column.get("name") == "type" and isinstance(column.get("type"), MySQLEnum):
            return [str(value) for value in column["type"].enums], bool(column.get("nullable"))
    return None


def _mysql_enum_definition(values: list[str], *, nullable: bool) -> str:
    escaped = ", ".join("'" + value.replace("'", "''") + "'" for value in values)
    return f"ENUM({escaped}) {'NULL' if nullable else 'NOT NULL'}"


async def _expand_mysql_message_enum(session: AsyncSession) -> None:
    connection = await session.connection()
    if connection.dialect.name != "mysql":
        return
    current = await connection.run_sync(_mysql_message_enum)
    if current is None:
        return
    values, nullable = current
    if MessageType.SUBAGENT_RESULT.name in values:
        return
    values.append(MessageType.SUBAGENT_RESULT.name)
    await session.execute(text(f"ALTER TABLE {Message.__tablename__} MODIFY COLUMN type " + _mysql_enum_definition(values, nullable=nullable)))


def _message_type_value(raw_type: Any) -> str | None:
    if isinstance(raw_type, MessageType):
        return raw_type.value
    if not isinstance(raw_type, str):
        return None
    if raw_type in MessageType.__members__:
        return MessageType[raw_type].value
    try:
        return MessageType(raw_type).value
    except ValueError:
        return None


def _context_message_id(message: Any) -> int | None:
    if not isinstance(message, dict):
        return None
    for key in ("id", "db_id"):
        value = message.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value > 0:
            return value
    return None


async def _message_types(
    session: AsyncSession,
    *,
    uid: str,
    session_id: str,
    arrays: list[list[Any]],
) -> dict[int, str]:
    message_ids: set[int] = set()
    for messages in arrays:
        for message in messages:
            message_id = _context_message_id(message)
            if message_id is not None:
                message_ids.add(message_id)
    types_by_id: dict[int, str] = {}
    ordered_ids = sorted(message_ids)
    for offset in range(0, len(ordered_ids), _BATCH_SIZE):
        result = await session.execute(
            select(Message.id, Message.type).where(
                Message.id.in_(ordered_ids[offset : offset + _BATCH_SIZE]),
                Message.uid == uid,
                Message.session_id == session_id,
            )
        )
        for row in result:
            message_type = _message_type_value(row[1])
            if row[0] is not None and message_type is not None:
                types_by_id[row[0]] = message_type
    return types_by_id


async def _rewrite_specs(
    session: AsyncSession,
    *,
    uid: str,
    session_id: str,
    specs: list[tuple[dict[str, Any], str, bool]],
) -> bool:
    if not specs:
        return False
    arrays = [container[key] for container, key, _remove_internal in specs]
    types_by_id = await _message_types(session, uid=uid, session_id=session_id, arrays=arrays)
    changed = False
    for (container, key, remove_internal), messages in zip(specs, arrays, strict=True):
        rewritten: list[Any] = []
        for message in messages:
            if not isinstance(message, dict):
                rewritten.append(message)
                continue
            updated_message = message
            message_id = _context_message_id(message)
            message_type = types_by_id.get(message_id) if message_id is not None else None
            if message_type is not None and message.get("type") != message_type:
                updated_message = dict(message)
                updated_message["type"] = message_type
                changed = True
            if remove_internal and _message_type_value(updated_message.get("type")) in _INTERNAL_CONTEXT_TYPES:
                changed = True
                continue
            rewritten.append(updated_message)
        container[key] = rewritten
    return changed


def _display_specs(event: dict[str, Any]) -> list[tuple[dict[str, Any], str, bool]]:
    specs: list[tuple[dict[str, Any], str, bool]] = []
    if isinstance(event.get("history"), list):
        specs.append((event, "history", True))
    response = event.get("response")
    if isinstance(response, dict) and isinstance(response.get("history"), list):
        specs.append((response, "history", True))
    if isinstance(event.get("messages"), list):
        specs.append((event, "messages", True))
    return specs


async def _rewrite_display_event(
    session: AsyncSession,
    event: Any,
    *,
    uid: str,
    session_id: str,
) -> tuple[Any, bool]:
    if not isinstance(event, dict):
        return event, False
    updated = deepcopy(event)
    changed = await _rewrite_specs(session, uid=uid, session_id=session_id, specs=_display_specs(updated))
    return updated, changed


def _state_specs(state: dict[str, Any]) -> list[tuple[dict[str, Any], str, bool]]:
    specs: list[tuple[dict[str, Any], str, bool]] = []
    response = state.get("response")
    if isinstance(response, dict) and isinstance(response.get("history"), list):
        specs.append((response, "history", True))
    external_context = state.get("external_resume_context")
    if not isinstance(external_context, list):
        return specs
    for payload in external_context:
        if not isinstance(payload, dict):
            continue
        if isinstance(payload.get("request_context"), list):
            specs.append((payload, "request_context", False))
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            for task_payload in tasks:
                if isinstance(task_payload, dict) and isinstance(task_payload.get("request_context"), list):
                    specs.append((task_payload, "request_context", False))
    return specs


async def _validate_checkpoint_hashes(session: AsyncSession) -> None:
    referenced = exists(select(1).select_from(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.message_id == SessionReplyCheckpointMessage.id))
    last_id = 0
    while True:
        result = await session.execute(select(SessionReplyCheckpointMessage).where(SessionReplyCheckpointMessage.id > last_id, referenced).order_by(SessionReplyCheckpointMessage.id.asc()).limit(_BATCH_SIZE))
        blobs = result.scalars().all()
        if not blobs:
            return
        for blob in blobs:
            if checkpoint_message_hash(blob.payload) != blob.content_hash:
                raise RuntimeError(f"{MIGRATION_ID}: checkpoint blob {blob.id} content hash does not match its payload")
        last_id = int(blobs[-1].id)


async def _migrate_messages(session: AsyncSession) -> None:
    last_id = 0
    while True:
        result = await session.execute(
            select(Message)
            .where(
                Message.id > last_id,
                Message.role == MessageRole.USER,
                Message.type == MessageType.BACKGROUND_TASK_RESULT,
                Message.dedupe_key.startswith("subagent-result:"),
            )
            .order_by(Message.id.asc())
            .limit(_BATCH_SIZE)
        )
        messages = result.scalars().all()
        if not messages:
            return
        changed = False
        for message in messages:
            if not isinstance(message.dedupe_key, str) or not message.dedupe_key.startswith("subagent-result:"):
                continue
            try:
                payload = json.loads(message.content or "")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and payload.get("type") == "external_work_resume" and payload.get("source") == "subagent":
                message.type = MessageType.SUBAGENT_RESULT
                changed = True
        if changed:
            await session.flush()
        last_id = int(messages[-1].id)


async def _reference_ids(session: AsyncSession, work_id: int) -> set[int]:
    result = await session.execute(
        select(SessionReplyCheckpointMessageReference.message_id).where(
            SessionReplyCheckpointMessageReference.work_id == work_id,
            SessionReplyCheckpointMessageReference.kind.in_(_REFERENCE_KINDS),
        )
    )
    return set(result.scalars().all())


async def _delete_unreferenced_blobs(session: AsyncSession, blob_ids: set[int]) -> None:
    if not blob_ids:
        return
    await session.flush()
    referenced = exists(select(1).select_from(SessionReplyCheckpointMessageReference).where(SessionReplyCheckpointMessageReference.message_id == SessionReplyCheckpointMessage.id))
    ordered_ids = sorted(blob_ids)
    for offset in range(0, len(ordered_ids), _BATCH_SIZE):
        await session.execute(
            delete(SessionReplyCheckpointMessage).where(
                SessionReplyCheckpointMessage.id.in_(ordered_ids[offset : offset + _BATCH_SIZE]),
                ~referenced,
            )
        )


async def _migrate_work(session: AsyncSession, work: SessionReplyWorkItem) -> None:
    checkpoint = await session_reply_work_item_crud.get_checkpoint(session, work.id)
    frozen_context = await session_reply_work_item_crud.get_frozen_context(session, work)
    checkpoint_before = deepcopy(checkpoint)
    frozen_before = deepcopy(frozen_context)
    state_before = deepcopy(work.execution_state)
    state = deepcopy(state_before)

    specs: list[tuple[dict[str, Any], str, bool]] = []
    if isinstance(checkpoint, dict):
        for key in ("messages", "turn_messages"):
            if key in checkpoint:
                specs.append((checkpoint, key, False))
    frozen_holder: dict[str, Any] | None = None
    if frozen_context is not None:
        frozen_holder = {"messages": frozen_context}
        specs.append((frozen_holder, "messages", False))
    if isinstance(state, dict):
        specs.extend(_state_specs(state))
    await _rewrite_specs(session, uid=work.uid, session_id=work.session_id, specs=specs)

    checkpoint_changed = checkpoint != checkpoint_before
    frozen_changed = frozen_holder is not None and frozen_holder["messages"] != frozen_before
    old_blob_ids = await _reference_ids(session, work.id) if checkpoint_changed or frozen_changed else set()

    if checkpoint_changed and checkpoint is not None:
        await session_reply_work_item_crud.save_checkpoint(session, work.id, checkpoint)
    if frozen_changed and frozen_holder is not None:
        state["frozen_context_message_ids"] = await session_reply_work_item_crud.save_frozen_context(session, work.id, frozen_holder["messages"])
    if state != state_before:
        work.execution_state = state
        session.add(work)
        await session.flush()
    await _delete_unreferenced_blobs(session, old_blob_ids)


async def _migrate_work_items(session: AsyncSession) -> None:
    last_id = 0
    while True:
        result = await session.execute(select(SessionReplyWorkItem).where(SessionReplyWorkItem.id > last_id).order_by(SessionReplyWorkItem.id.asc()).limit(_BATCH_SIZE))
        works = result.scalars().all()
        if not works:
            return
        for work in works:
            await _migrate_work(session, work)
        last_id = int(works[-1].id)


async def _migrate_background_tasks(session: AsyncSession) -> None:
    last_id = 0
    while True:
        result = await session.execute(select(BackgroundTask).where(BackgroundTask.id > last_id).order_by(BackgroundTask.id.asc()).limit(_BATCH_SIZE))
        tasks = result.scalars().all()
        if not tasks:
            return
        dirty = False
        for task in tasks:
            if not isinstance(task.extra, dict):
                continue
            extra = deepcopy(task.extra)
            specs = []
            for key in ("request_context", "frozen_context_messages"):
                if isinstance(extra.get(key), list):
                    specs.append((extra, key, False))
            if await _rewrite_specs(session, uid=task.uid, session_id=task.session_id, specs=specs):
                task.extra = extra
                session.add(task)
                dirty = True
        if dirty:
            await session.flush()
        last_id = int(tasks[-1].id)


async def _migrate_session_events(session: AsyncSession) -> None:
    last_id = 0
    while True:
        result = await session.execute(select(SessionEvent).where(SessionEvent.id > last_id).order_by(SessionEvent.id.asc()).limit(_BATCH_SIZE))
        events = result.scalars().all()
        if not events:
            return
        dirty = False
        for event in events:
            updated, changed = await _rewrite_display_event(
                session,
                event.event,
                uid=event.uid,
                session_id=event.session_id,
            )
            if changed:
                event.event = updated
                session.add(event)
                dirty = True
        if dirty:
            await session.flush()
        last_id = int(events[-1].id)


async def _migrate_stream_events(session: AsyncSession) -> None:
    last_id = 0
    while True:
        result = await session.execute(
            select(SessionReplyStreamEvent, SessionReplyWorkItem.uid, SessionReplyWorkItem.session_id).join(SessionReplyWorkItem, SessionReplyStreamEvent.work_id == SessionReplyWorkItem.id).where(SessionReplyStreamEvent.id > last_id).order_by(SessionReplyStreamEvent.id.asc()).limit(_BATCH_SIZE)
        )
        rows = result.all()
        if not rows:
            return
        dirty = False
        for stream_event, uid, session_id in rows:
            updated, changed = await _rewrite_display_event(
                session,
                stream_event.event,
                uid=uid,
                session_id=session_id,
            )
            if changed:
                stream_event.event = updated
                session.add(stream_event)
                dirty = True
        if dirty:
            await session.flush()
        last_id = int(rows[-1][0].id)


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)
    await _expand_mysql_message_enum(session)
    await _validate_checkpoint_hashes(session)
    await _migrate_messages(session)
    await _migrate_work_items(session)
    await _migrate_background_tasks(session)
    await _migrate_session_events(session)
    await _migrate_stream_events(session)
