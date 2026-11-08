from typing import Any, NoReturn

from sqlalchemy import func, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.constants import AGENT_WORK_TOOL_NAME, GOAL_EXECUTION_PHASE_RUNNING
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.utils.time import get_local_time
from app.models.session_reply_work_item import (
    SessionReplySequence,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
)
from app.models.subagent import Subagent, SubagentToolCall, SubagentWork
from app.providers.database import ensure_sqlite_outer_transaction
from app.providers.database.time import get_database_timestamp

MIGRATION_ID = "20261107_02_release_implicit_subagent_waits"

_BATCH_SIZE = 200
_CHILD_CALL_OPERATIONS = {"create", "send_message", "query", "wait"}


def _raise_migration_error(work_id: int, detail: str) -> NoReturn:
    raise RuntimeError(f"{MIGRATION_ID}: work item {work_id}: {detail}")


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _non_empty_text(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


async def _allocate_sequence_no(session: AsyncSession, session_id: str) -> int:
    sequence_result = await session.execute(select(SessionReplySequence).where(SessionReplySequence.session_id == session_id).with_for_update())
    sequence = sequence_result.scalar_one_or_none()
    max_result = await session.execute(
        select(func.max(SessionReplyWorkItem.sequence_no)).where(
            SessionReplyWorkItem.session_id == session_id,
        )
    )
    max_sequence = max_result.scalar_one() or 0
    if sequence is None:
        allocated = int(max_sequence) + 1
        session.add(
            SessionReplySequence(
                session_id=session_id,
                next_sequence_no=allocated + 1,
                updated_at=get_local_time(),
            )
        )
    else:
        current_next = sequence.next_sequence_no if _positive_int(sequence.next_sequence_no) else 1
        allocated = max(int(max_sequence) + 1, current_next)
        sequence.next_sequence_no = allocated + 1
        sequence.updated_at = get_local_time()
        session.add(sequence)
    await session.flush()
    return allocated


def _candidate_state(work: SessionReplyWorkItem) -> dict[str, Any] | None:
    if work.id is None:
        raise RuntimeError(f"{MIGRATION_ID}: waiting work item has no id")
    state = work.execution_state
    if not isinstance(state, dict):
        _raise_migration_error(work.id, "execution_state is not an object")

    for key in ("pending_tools", "terminal_watch_waits"):
        if key not in state:
            continue
        slots = state[key]
        if not isinstance(slots, list):
            _raise_migration_error(work.id, f"{key} is not a list")
        if slots:
            return None

    if "child_work_waits" not in state:
        return None
    child_work_waits = state["child_work_waits"]
    if not isinstance(child_work_waits, list):
        _raise_migration_error(work.id, "child_work_waits is not a list")
    if not child_work_waits:
        return None
    return state


async def _parent_calls(session: AsyncSession, work: SessionReplyWorkItem) -> dict[str, SubagentToolCall]:
    if work.id is None:
        raise RuntimeError(f"{MIGRATION_ID}: waiting work item has no id")
    result = await session.execute(select(SubagentToolCall).where(SubagentToolCall.parent_work_id == work.id).order_by(SubagentToolCall.id.asc()))
    calls: dict[str, SubagentToolCall] = {}
    for call in result.scalars().all():
        if not _positive_int(call.id) or not _non_empty_text(call.tool_call_id):
            _raise_migration_error(work.id, "subagent tool call has an invalid identity")
        if call.tool_call_id in calls:
            _raise_migration_error(work.id, f"duplicate subagent tool call {call.tool_call_id}")
        calls[call.tool_call_id] = call
    return calls


async def _validate_child_slot(
    session: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    slot: Any,
    calls: dict[str, SubagentToolCall],
    seen_slot_keys: set[tuple[int, str]],
) -> bool:
    if work.id is None:
        raise RuntimeError(f"{MIGRATION_ID}: waiting work item has no id")
    if not isinstance(slot, dict):
        _raise_migration_error(work.id, "child_work_waits contains a non-object slot")

    child_work_id = slot.get("child_work_id")
    subagent_id = slot.get("subagent_id")
    tool_call_id = slot.get("tool_call_id")
    tool_result_message_id = slot.get("tool_result_message_id")
    if not _positive_int(child_work_id) or not _non_empty_text(subagent_id) or not _non_empty_text(tool_call_id) or not _positive_int(tool_result_message_id):
        _raise_migration_error(work.id, "child_work_waits contains an invalid slot identity")
    if "tool_name" in slot and slot.get("tool_name") != AGENT_WORK_TOOL_NAME:
        _raise_migration_error(work.id, f"slot {tool_call_id} has an invalid tool name")

    slot_key = (child_work_id, tool_call_id)
    if slot_key in seen_slot_keys:
        _raise_migration_error(work.id, f"duplicate child wait slot {child_work_id}:{tool_call_id}")
    seen_slot_keys.add(slot_key)

    call = calls.get(tool_call_id)
    if call is None:
        _raise_migration_error(work.id, f"slot {tool_call_id} references a missing subagent tool call")
    if call.parent_work_id != work.id or call.child_work_id != child_work_id or call.subagent_id != subagent_id or call.tool_call_id != tool_call_id or call.tool_result_message_id != tool_result_message_id or call.delivered is not False:
        _raise_migration_error(work.id, f"slot {tool_call_id} does not match its subagent tool call")

    arguments = call.arguments
    operation = arguments.get("operation") if isinstance(arguments, dict) else None
    if operation not in _CHILD_CALL_OPERATIONS:
        _raise_migration_error(work.id, f"slot {tool_call_id} has an invalid subagent operation")
    if operation in {"send_message", "query", "wait"} and arguments.get("subagent_id") != subagent_id:
        _raise_migration_error(work.id, f"slot {tool_call_id} has an inconsistent subagent argument")
    if operation in {"query", "wait"} and "work_id" in arguments and arguments.get("work_id") != child_work_id:
        _raise_migration_error(work.id, f"slot {tool_call_id} has an inconsistent child work argument")

    subagent_result = await session.execute(
        select(Subagent).where(
            Subagent.id == subagent_id,
            Subagent.uid == work.uid,
            Subagent.session_id == work.session_id,
            Subagent.profile_id == work.profile_id,
        )
    )
    subagent = subagent_result.scalar_one_or_none()
    if subagent is None:
        _raise_migration_error(work.id, f"slot {tool_call_id} references an out-of-scope subagent")

    round_result = await session.execute(
        select(SubagentWork).where(
            SubagentWork.child_work_id == child_work_id,
            SubagentWork.subagent_id == subagent_id,
        )
    )
    round_row = round_result.scalar_one_or_none()
    if round_row is None or not _positive_int(round_row.current_work_id):
        _raise_migration_error(work.id, f"slot {tool_call_id} references an invalid child round")

    child_work = await session.get(SessionReplyWorkItem, round_row.current_work_id)
    child_state = child_work.execution_state if child_work is not None else None
    if (
        child_work is None
        or child_work.id != round_row.current_work_id
        or child_work.uid != work.uid
        or child_work.session_id != work.session_id
        or child_work.profile_id != work.profile_id
        or child_work.subagent_id != subagent_id
        or not isinstance(child_state, dict)
        or child_state.get("child_work_id", child_work.id) != child_work_id
    ):
        _raise_migration_error(work.id, f"slot {tool_call_id} does not match its child work")

    return operation == "wait"


async def _has_explicit_wait(session: AsyncSession, work: SessionReplyWorkItem, state: dict[str, Any]) -> bool:
    if work.id is None:
        raise RuntimeError(f"{MIGRATION_ID}: waiting work item has no id")
    calls = await _parent_calls(session, work)
    slots = state["child_work_waits"]
    seen_slot_keys: set[tuple[int, str]] = set()
    has_wait = False
    represented_call_ids: set[str] = set()
    for slot in slots:
        if isinstance(slot, dict) and isinstance(slot.get("tool_call_id"), str):
            represented_call_ids.add(slot["tool_call_id"])
        if await _validate_child_slot(
            session,
            work=work,
            slot=slot,
            calls=calls,
            seen_slot_keys=seen_slot_keys,
        ):
            has_wait = True

    for call in calls.values():
        if call.delivered is False and call.tool_call_id not in represented_call_ids:
            _raise_migration_error(work.id, f"undelivered subagent tool call {call.tool_call_id} has no child wait slot")
    return has_wait


def _checkpoint_for_release(work: SessionReplyWorkItem, checkpoint: Any) -> dict[str, Any]:
    if work.id is None:
        raise RuntimeError(f"{MIGRATION_ID}: waiting work item has no id")
    if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("messages"), list):
        _raise_migration_error(work.id, "checkpoint is missing a valid messages list")
    for key in ("messages", "turn_messages"):
        if key in checkpoint and (not isinstance(checkpoint[key], list) or any(not isinstance(message, dict) for message in checkpoint[key])):
            _raise_migration_error(work.id, f"checkpoint {key} is invalid")

    checkpoint["current_turn"] = 0
    checkpoint["execution_phase"] = GOAL_EXECUTION_PHASE_RUNNING
    return checkpoint


async def _release_work(session: AsyncSession, work: SessionReplyWorkItem, state: dict[str, Any]) -> None:
    if work.id is None:
        raise RuntimeError(f"{MIGRATION_ID}: waiting work item has no id")
    if await _has_explicit_wait(session, work, state):
        return

    checkpoint = _checkpoint_for_release(
        work,
        await session_reply_work_item_crud.get_checkpoint(session, work.id),
    )
    sequence_no = await _allocate_sequence_no(session, work.session_id)
    available_at = await get_database_timestamp(session)
    result = await session.execute(
        update(SessionReplyWorkItem)
        .where(
            SessionReplyWorkItem.id == work.id,
            SessionReplyWorkItem.uid == work.uid,
            SessionReplyWorkItem.session_id == work.session_id,
            SessionReplyWorkItem.profile_id == work.profile_id,
            SessionReplyWorkItem.dedupe_key == work.dedupe_key,
            SessionReplyWorkItem.subagent_id.is_(None),
            SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
        )
        .values(
            sequence_no=sequence_no,
            status=SessionReplyWorkStatus.READY_FOR_LLM,
            locked_by=None,
            lock_until=None,
            attempt_count=0,
            available_at=available_at,
            error=None,
            updated_at=get_local_time(),
        )
        .execution_options(synchronize_session=False)
    )
    if (result.rowcount or 0) != 1:
        return
    await session_reply_work_item_crud.save_checkpoint(session, work.id, checkpoint)


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)

    last_id = 0
    while True:
        result = await session.execute(
            select(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id > last_id,
                SessionReplyWorkItem.subagent_id.is_(None),
                SessionReplyWorkItem.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK,
            )
            .order_by(SessionReplyWorkItem.id.asc())
            .limit(_BATCH_SIZE)
            .execution_options(populate_existing=True)
        )
        works = list(result.scalars().all())
        if not works:
            return

        for work in works:
            state = _candidate_state(work)
            if state is not None:
                await _release_work(session, work, state)
        last_id = int(works[-1].id)
