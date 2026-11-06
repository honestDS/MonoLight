import json
from typing import Any, NoReturn

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.models.message import MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import SubagentToolCall
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261106_02_register_pending_subagent_results"

_BATCH_SIZE = 200
_ACTIVE_PARENT_STATUSES = (
    SessionReplyWorkStatus.READY_FOR_LLM,
    SessionReplyWorkStatus.RUNNING,
    SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
)
_SUBAGENT_RESULT_PREFIX = "subagent-result:"


def _raise_migration_error(work_id: int, detail: str) -> NoReturn:
    raise RuntimeError(f"{MIGRATION_ID}: work item {work_id}: {detail}")


def _positive_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _pending_subagent_results(messages: Any, turn_messages: Any) -> list[dict[str, Any]]:
    if not isinstance(messages, list):
        return []

    latest_assistant_id: int | None = None
    if isinstance(turn_messages, list):
        for message in turn_messages:
            if not isinstance(message, dict) or message.get("role") != MessageRole.ASSISTANT.value:
                continue
            message_id = _positive_int(message.get("id"))
            if message_id is not None and (latest_assistant_id is None or message_id > latest_assistant_id):
                latest_assistant_id = message_id

    if latest_assistant_id is not None:
        pending: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            if message.get("role") != MessageRole.USER.value or message.get("type") != MessageType.SUBAGENT_RESULT.value:
                continue
            message_id = _positive_int(message.get("id"))
            if message_id is None or message_id > latest_assistant_id:
                pending.append(message)
        return pending

    trailing: list[dict[str, Any]] = []
    for message in reversed(messages):
        if not isinstance(message, dict):
            break
        if message.get("role") != MessageRole.USER.value or message.get("type") != MessageType.SUBAGENT_RESULT.value:
            break
        trailing.append(message)
    trailing.reverse()
    return trailing


def _call_id_from_dedupe_key(dedupe_key: Any) -> int | None:
    if not isinstance(dedupe_key, str) or not dedupe_key.startswith(_SUBAGENT_RESULT_PREFIX):
        return None
    raw_id = dedupe_key[len(_SUBAGENT_RESULT_PREFIX) :]
    if not raw_id or not raw_id.isascii() or not raw_id.isdigit():
        return None
    return _positive_int(int(raw_id))


def _validate_resume_content(work_id: int, marker_id: int, content: Any, child_work_id: int) -> None:
    try:
        payload = json.loads(content or "")
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{MIGRATION_ID}: work item {work_id}: marker {marker_id} has invalid JSON content") from exc

    results = payload.get("results") if isinstance(payload, dict) else None
    result_child_work_id = results[0].get("child_work_id") if isinstance(results, list) and results and isinstance(results[0], dict) else None
    valid_payload = isinstance(payload, dict) and payload.get("type") == "external_work_resume" and payload.get("source") == "subagent" and isinstance(results, list) and bool(results) and isinstance(results[0], dict) and _positive_int(result_child_work_id) == child_work_id
    if not valid_payload:
        raise RuntimeError(f"{MIGRATION_ID}: work item {work_id}: marker {marker_id} has an invalid subagent resume payload")


async def _lock_parent(session: AsyncSession, work: SessionReplyWorkItem) -> bool:
    if work.id is None:
        return False

    result = await session.execute(
        update(SessionReplyWorkItem)
        .where(
            SessionReplyWorkItem.id == work.id,
            SessionReplyWorkItem.uid == work.uid,
            SessionReplyWorkItem.session_id == work.session_id,
            SessionReplyWorkItem.profile_id == work.profile_id,
            SessionReplyWorkItem.work_type == work.work_type,
            SessionReplyWorkItem.source_type == work.source_type,
            SessionReplyWorkItem.source_id == work.source_id,
            SessionReplyWorkItem.dedupe_key == work.dedupe_key,
            SessionReplyWorkItem.subagent_id.is_(None),
            SessionReplyWorkItem.status.in_(_ACTIVE_PARENT_STATUSES),
            SessionReplyWorkItem.execution_state["has_child_work"].as_boolean().is_(True),
        )
        .values(updated_at=SessionReplyWorkItem.updated_at)
        .execution_options(synchronize_session=False)
    )
    if (result.rowcount or 0) != 1:
        return False

    await session.refresh(work)
    return True


async def _valid_marker_ids(
    session: AsyncSession,
    work: SessionReplyWorkItem,
    snapshots: list[dict[str, Any]],
    expected_work_id: int | None,
) -> list[int]:
    if work.id is None:
        return []

    marker_ids: list[int] = []
    seen_marker_ids: set[int] = set()
    for snapshot in snapshots:
        marker_id = _positive_int(snapshot.get("id"))
        if marker_id is None:
            _raise_migration_error(work.id, "a trailing subagent result has no valid message id")
        if marker_id in seen_marker_ids:
            continue
        seen_marker_ids.add(marker_id)

        marker = await message_crud.get(session, marker_id)
        if marker is None or marker.id != marker_id:
            _raise_migration_error(work.id, f"marker {marker_id} does not exist")

        call_id = _call_id_from_dedupe_key(marker.dedupe_key)
        if call_id is None:
            _raise_migration_error(work.id, f"marker {marker_id} has no valid subagent call dedupe key")
        call = await session.get(SubagentToolCall, call_id)
        if call is None:
            _raise_migration_error(work.id, f"marker {marker_id} references missing subagent call {call_id}")
        if call.parent_work_id != work.id:
            continue
        if call.delivered is not True:
            _raise_migration_error(work.id, f"marker {marker_id} references undelivered subagent call {call_id}")
        message_matches = marker.dedupe_key == f"{_SUBAGENT_RESULT_PREFIX}{call.id}" and marker.uid == work.uid and marker.session_id == work.session_id and marker.profile_id == work.profile_id and marker.work_id == expected_work_id and marker.role == MessageRole.USER and marker.type == MessageType.SUBAGENT_RESULT
        if not message_matches:
            _raise_migration_error(work.id, f"marker {marker_id} has inconsistent message ownership or type")

        _validate_resume_content(work.id, marker_id, marker.content, call.child_work_id)
        marker_ids.append(marker_id)

    return marker_ids


async def _migrate_work(session: AsyncSession, work: SessionReplyWorkItem) -> None:
    if work.id is None or work.subagent_id is not None:
        return
    if not await _lock_parent(session, work):
        return

    checkpoint = await session_reply_work_item_crud.get_checkpoint(session, work.id)
    if checkpoint is None:
        return
    if not isinstance(checkpoint, dict):
        _raise_migration_error(work.id, "checkpoint is not an object")

    snapshots = _pending_subagent_results(checkpoint.get("messages"), checkpoint.get("turn_messages"))
    if not snapshots:
        return

    expected_work_id = await session_reply_work_item_crud.get_message_work_id(session, work)
    marker_ids = await _valid_marker_ids(session, work, snapshots, expected_work_id)
    if not marker_ids:
        return

    metadata = await session_reply_work_item_crud.get_checkpoint_metadata(session, work.id)
    if metadata is None:
        _raise_migration_error(work.id, "checkpoint metadata is missing")

    existing_ids = metadata.get("pending_resume_message_ids")
    if existing_ids is None:
        pending_ids: list[Any] = []
    elif isinstance(existing_ids, list):
        pending_ids = list(existing_ids)
    else:
        _raise_migration_error(work.id, "pending_resume_message_ids is not a list")

    updated_ids = list(pending_ids)
    for marker_id in marker_ids:
        if marker_id not in updated_ids:
            updated_ids.append(marker_id)
    if updated_ids == pending_ids:
        return

    metadata["pending_resume_message_ids"] = updated_ids
    await session_reply_work_item_crud.save_checkpoint_metadata(session, work.id, metadata)


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)

    last_id = 0
    while True:
        result = await session.execute(
            select(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id > last_id,
                SessionReplyWorkItem.subagent_id.is_(None),
                SessionReplyWorkItem.status.in_(_ACTIVE_PARENT_STATUSES),
                SessionReplyWorkItem.execution_state["has_child_work"].as_boolean().is_(True),
            )
            .order_by(SessionReplyWorkItem.id.asc())
            .limit(_BATCH_SIZE)
            .execution_options(populate_existing=True)
        )
        works = list(result.scalars().all())
        if not works:
            return

        for work in works:
            await _migrate_work(session, work)
        last_id = int(works[-1].id)
