"""Resolve the trusted user behind a system action."""

from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.account.user import user_crud
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.models.session_reply_work_item import SessionReplySourceType, SessionReplyWorkItem, SessionReplyWorkType
from app.models.user import User

__all__ = ["resolve_action_actor"]


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _parse_source_id(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if not isinstance(value, str):
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _work_actor_uid(work: SessionReplyWorkItem) -> str | None:
    state = work.execution_state if isinstance(work.execution_state, dict) else {}
    value = state.get("actor_uid")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def _agent_parent_work_id(work: SessionReplyWorkItem) -> int | None:
    if work.work_type != SessionReplyWorkType.AGENT_WORK:
        return None
    state = work.execution_state if isinstance(work.execution_state, dict) else {}
    agent_work = state.get("agent_work")
    if not isinstance(agent_work, dict):
        return None
    parent_work_id = agent_work.get("parent_work_id")
    if not _is_positive_int(parent_work_id):
        return None
    return parent_work_id


async def _audit_parent_work_id(
    db: AsyncSession,
    work: SessionReplyWorkItem,
) -> tuple[int | None, bool]:
    if work.work_type != SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION:
        return None, True
    if work.source_type != SessionReplySourceType.AUDIT_RECORD:
        return None, True

    audit_record_id = _parse_source_id(work.source_id)
    if audit_record_id is None:
        return None, True
    record = await audit_crud.get_record(db, audit_record_id)
    if record is None:
        return None, True
    if record.uid != work.uid or record.session_id != work.session_id:
        return None, False
    return (record.source_work_id if _is_positive_int(record.source_work_id) else None), True


def _same_work_scope(work: SessionReplyWorkItem, reference: SessionReplyWorkItem) -> bool:
    return work.uid == reference.uid and work.session_id == reference.session_id and work.profile_id == reference.profile_id


async def resolve_action_actor(
    db: AsyncSession,
    *,
    uid: str,
    actor_uid: str | None = None,
) -> User | None:
    if actor_uid is not None:
        return await user_crud.get_by_uid(db, actor_uid)

    db_info = getattr(db, "info", None)
    claim_info = db_info.get(SESSION_REPLY_WORK_CLAIM_INFO_KEY) if isinstance(db_info, dict) else None
    if claim_info is None:
        return await user_crud.get_by_uid(db, uid)
    if not isinstance(claim_info, (tuple, list)) or len(claim_info) != 2:
        return None

    work_id = claim_info[0]
    if not _is_positive_int(work_id):
        return None
    work = await session_reply_work_item_crud.get(db, work_id)
    if work is None or not _is_positive_int(work.id) or work.id != work_id or work.uid != uid:
        return None

    reference_work = work
    current_work = work
    visited_work_ids: set[int] = set()
    while True:
        if not _is_positive_int(current_work.id) or current_work.id in visited_work_ids:
            return await user_crud.get_by_uid(db, uid)
        if not _same_work_scope(current_work, reference=reference_work):
            return None
        visited_work_ids.add(current_work.id)

        resolved_actor_uid = _work_actor_uid(current_work)
        if resolved_actor_uid is not None:
            return await user_crud.get_by_uid(db, resolved_actor_uid)

        if current_work.work_type == SessionReplyWorkType.AGENT_WORK:
            parent_work_id = _agent_parent_work_id(current_work)
            scope_valid = True
        else:
            parent_work_id, scope_valid = await _audit_parent_work_id(db, current_work)
        if not scope_valid:
            return None
        if parent_work_id is None or parent_work_id in visited_work_ids:
            return await user_crud.get_by_uid(db, uid)

        parent_work = await session_reply_work_item_crud.get(db, parent_work_id)
        if parent_work is None:
            return await user_crud.get_by_uid(db, uid)
        if not _is_positive_int(parent_work.id) or parent_work.id != parent_work_id:
            return await user_crud.get_by_uid(db, uid)
        if not _same_work_scope(parent_work, reference=reference_work):
            return None
        current_work = parent_work
