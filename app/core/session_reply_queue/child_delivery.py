import json
from copy import deepcopy
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation_results import replace_pending_tool_result
from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    ERR_SUBAGENT_CONTEXT_INVALID,
    GOAL_EXECUTION_PHASE_RUNNING,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.event import session_event_crud
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud as reply_work_crud
from app.core.crud.session.subagent import subagent_crud
from app.core.exceptions import ParameterException
from app.core.prompts import EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
from app.core.session_reply_queue.child_result import (
    build_subagent_work_result,
    get_child_snapshot,
    load_child_work,
)
from app.core.session_reply_queue.executor_common import _event_for_work
from app.core.session_reply_queue.manager import build_identified_work_response
from app.core.utils.dispatcher.save_message import save_message
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkCategory, SessionReplyWorkItem, SessionReplyWorkStatus
from app.models.subagent import SubagentBoardPost

__all__ = [
    "acknowledge_observed_child_results",
    "supersede_child_reports",
    "resume_child_work_for_parent",
    "deliver_child_work_results",
]


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _invalid_context() -> None:
    raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID)


def _parse_checkpoint_messages(value: Any) -> list[InternalMessage]:
    if not isinstance(value, list):
        _invalid_context()
    parsed: list[InternalMessage] = []
    for item in value:
        try:
            parsed.append(InternalMessage.model_validate(item))
        except (TypeError, ValueError):
            _invalid_context()
    return parsed


def _serialize_checkpoint_messages(messages: list[InternalMessage]) -> list[dict[str, Any]]:
    return [message.model_dump(mode="json", exclude_none=True) for message in messages]


def _read_subagent_notification(
    message: Message,
    *,
    work: SessionReplyWorkItem,
    child_work_id: int,
) -> tuple[bool, bool]:
    if message.uid != work.uid or message.session_id != work.session_id or message.profile_id != work.profile_id or message.role != MessageRole.USER or message.type != MessageType.SUBAGENT_RESULT:
        return False, False
    try:
        payload = json.loads(message.content or "")
    except (TypeError, ValueError):
        return False, False
    if not isinstance(payload, dict) or payload.get("source") != "subagent":
        return False, False

    if payload.get("type") in ("subagent_board_update", "subagent_result_ready"):
        return _is_positive_int(payload.get("work_id")) and payload.get("work_id") == child_work_id, True
    if payload.get("type") != "external_work_resume":
        return False, False
    results = payload.get("results")
    if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
        return False, True
    result = results[0]
    return _is_positive_int(result.get("work_id")) and result.get("work_id") == child_work_id, True


def _has_other_child_waits(state: dict[str, Any]) -> bool:
    return any(isinstance(state.get(key), list) and state[key] for key in ("child_work_waits", "pending_tools", "terminal_watch_waits", "terminal_auto_watch_task_ids"))


def _has_other_external_delivery(state: dict[str, Any], checkpoint: dict[str, Any] | None) -> bool:
    if state.get("terminal_auto_resume") is True:
        return True
    if any(isinstance(state.get(key), list) and state[key] for key in ("external_resume_context", "completed_task_ids", "external_results", "deferred_external_results")):
        return True
    return isinstance(checkpoint, dict) and (checkpoint.get("deferred_external_results") is True or bool(checkpoint.get("external_resume_messages")))


async def _wake_waiting_parent(
    db: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    state: dict[str, Any],
    checkpoint: dict[str, Any],
    event_dedupe_key: str,
) -> bool:
    state["external_resume_pending"] = True
    checkpoint["current_turn"] = 0
    checkpoint["execution_phase"] = GOAL_EXECUTION_PHASE_RUNNING
    checkpoint["turn_messages"] = []
    checkpoint["reload_history"] = True
    checkpoint.pop("reply_skipped", None)
    if not await external_work_crud.wake_waiting(
        db,
        work=work,
        execution_state=state,
        commit=False,
    ):
        return False
    await reply_work_crud.save_checkpoint(db, work.id, checkpoint)
    await session_event_crud.publish(
        db,
        dedupe_key=event_dedupe_key,
        uid=work.uid,
        session_id=work.session_id,
        event={"type": "work_ready", "session_id": work.session_id, "work_id": work.id},
        commit=False,
    )
    return True


def _has_tool_call_context(messages: list[InternalMessage], call_ids: set[str]) -> bool:
    return any(message.role == MessageRole.ASSISTANT and any(tool_call.id in call_ids for tool_call in (message.tool_calls or [])) for message in messages)


def _replace_checkpoint_tool_result(
    messages: list[InternalMessage],
    *,
    replacement: InternalMessage,
    message_id: int,
    call_ids: set[str],
) -> bool:
    index = next(
        (index for index, message in enumerate(messages) if message.role == MessageRole.TOOL and message.id == message_id),
        None,
    )
    if index is None:
        index = next(
            (index for index, message in enumerate(messages) if message.role == MessageRole.TOOL and message.tool_call_id in call_ids),
            None,
        )
    if index is None:
        return False
    messages[index] = replacement.model_copy(deep=True)
    return True


def _build_resume_message(
    *,
    call_id: str,
    arguments: dict[str, Any],
    result: dict[str, Any],
) -> InternalMessage:
    return InternalMessage(
        role=MessageRole.USER,
        type=MessageType.SUBAGENT_RESULT,
        content=json.dumps(
            {
                "type": "external_work_resume",
                "source": "subagent",
                "instruction": EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT,
                "tool_call": {
                    "id": call_id,
                    "name": AGENT_WORK_TOOL_NAME,
                    "arguments": deepcopy(arguments),
                },
                "results": [deepcopy(result)],
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


def _build_result_ready_message(*, subagent_id: str, work_id: int) -> InternalMessage:
    return InternalMessage(
        role=MessageRole.USER,
        type=MessageType.SUBAGENT_RESULT,
        content=json.dumps(
            {
                "type": "subagent_result_ready",
                "source": "subagent",
                "subagent_id": subagent_id,
                "work_id": work_id,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ),
    )


async def _load_child_tool_result(
    db: AsyncSession,
    *,
    parent,
    call,
    round_row,
    message_id: int,
) -> tuple[Message, InternalMessage]:
    message = await message_crud.get(db, message_id)
    if message is None or message.id != message_id or message.uid != parent.uid or message.session_id != parent.session_id or message.profile_id != parent.profile_id or message.role != MessageRole.TOOL or message.type != MessageType.TOOL_RESULT:
        _invalid_context()

    try:
        stored = InternalMessage.model_validate_json(message.content or "{}")
        payload = json.loads(stored.content or "{}")
    except (TypeError, ValueError):
        _invalid_context()

    if stored.role != MessageRole.TOOL or not isinstance(stored.tool_call_id, str) or not stored.tool_call_id.strip() or not isinstance(payload, dict) or not _is_positive_int(payload.get("work_id")) or payload.get("work_id") != round_row.child_work_id or payload.get("subagent_id") != call.subagent_id:
        _invalid_context()
    if message.audit_tool_call_id is not None and message.audit_tool_call_id != stored.tool_call_id:
        _invalid_context()

    audit_record_id = message.audit_record_id
    if audit_record_id is None:
        if stored.tool_call_id != call.tool_call_id:
            _invalid_context()
    else:
        if not _is_positive_int(audit_record_id):
            _invalid_context()
        binding = await audit_crud.get_execution_binding_for_tool_call(
            db,
            new_tool_call_id=call.tool_call_id,
        )
        if binding is None:
            _invalid_context()
        record, execution = binding
        if record is None or execution is None or record.id != audit_record_id or record.uid != parent.uid or record.session_id != parent.session_id or execution.audit_record_id != record.id or execution.new_tool_call_id != call.tool_call_id:
            _invalid_context()
        details = await audit_crud.list_tool_details(db, record.id)
        detail = next((item for item in details if item.id == execution.audit_tool_detail_id), None)
        if detail is None or detail.original_tool_call_id != stored.tool_call_id or detail.tool_name != AGENT_WORK_TOOL_NAME:
            _invalid_context()

    return message, stored


async def _apply_child_call_result(
    db: AsyncSession,
    *,
    parent,
    call,
    round_row,
    snapshot: dict[str, Any],
    messages: list[InternalMessage],
    turn_messages: list[InternalMessage] | None,
) -> tuple[str, dict[str, Any], bool]:
    pending_message, stored = await _load_child_tool_result(
        db,
        parent=parent,
        call=call,
        round_row=round_row,
        message_id=call.tool_result_message_id,
    )
    try:
        child_result = build_subagent_work_result(call.subagent_id, snapshot)
    except (KeyError, TypeError, ValueError) as exc:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID) from exc
    execution_status = {
        "succeeded": "succeeded",
        "execution_unknown": "execution_unknown",
    }.get(snapshot.get("status"), "failed")
    await replace_pending_tool_result(
        db,
        pending_message=pending_message,
        original_tool_call_id=stored.tool_call_id,
        content=json.dumps(child_result, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        tool_execution_status=execution_status,
        audit_record_id=pending_message.audit_record_id,
    )
    try:
        replacement = InternalMessage.model_validate_json(pending_message.content or "{}")
        replacement_payload = json.loads(replacement.content or "{}")
    except (TypeError, ValueError) as exc:
        raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID) from exc
    if (
        replacement.role != MessageRole.TOOL
        or replacement.tool_call_id != stored.tool_call_id
        or not isinstance(replacement_payload, dict)
        or not _is_positive_int(replacement_payload.get("work_id"))
        or replacement_payload.get("work_id") != round_row.child_work_id
        or replacement_payload.get("subagent_id") != call.subagent_id
    ):
        _invalid_context()
    replacement.id = pending_message.id
    replacement.type = pending_message.type
    if pending_message.created_at is not None:
        replacement.created_at = pending_message.created_at.timestamp()

    call_ids = {call.tool_call_id, stored.tool_call_id}
    has_context = _has_tool_call_context([*messages, *(turn_messages or [])], call_ids)
    replaced = _replace_checkpoint_tool_result(
        messages,
        replacement=replacement,
        message_id=call.tool_result_message_id,
        call_ids=call_ids,
    )
    if turn_messages is not None:
        replaced = (
            _replace_checkpoint_tool_result(
                turn_messages,
                replacement=replacement,
                message_id=call.tool_result_message_id,
                call_ids=call_ids,
            )
            or replaced
        )
    needs_resume_message = not has_context or not replaced
    call.delivered = True
    db.add(call)
    return stored.tool_call_id, replacement_payload, needs_resume_message


async def acknowledge_observed_child_results(
    db: AsyncSession,
    *,
    parent,
    checkpoint: dict[str, Any],
    execution_state: dict[str, Any],
) -> None:
    if parent.subagent_id is not None:
        return

    child_work_waits = execution_state.get("child_work_waits")
    if not isinstance(child_work_waits, list) or not child_work_waits:
        return

    messages = _parse_checkpoint_messages(checkpoint.get("messages"))
    if "turn_messages" not in checkpoint:
        return
    turn_messages = _parse_checkpoint_messages(checkpoint["turn_messages"])
    turn_result_ids = {message.id for message in turn_messages if message.role == MessageRole.TOOL and _is_positive_int(message.id)}
    if not turn_result_ids:
        return

    observed_calls: dict[str, Any] = {}
    observed_results: dict[int, tuple[InternalMessage, Any]] = {}
    for message in messages:
        if message.role == MessageRole.ASSISTANT:
            observed_calls = {}
            for tool_call in message.tool_calls or []:
                if tool_call.name != AGENT_WORK_TOOL_NAME or not isinstance(tool_call.arguments, dict) or tool_call.arguments.get("operation") not in ("query_result", "cancel"):
                    continue
                subagent_id = tool_call.arguments.get("subagent_id")
                if not isinstance(subagent_id, str) or not subagent_id.strip():
                    continue
                observed_calls[tool_call.id] = tool_call
            continue
        if message.role != MessageRole.TOOL:
            observed_calls = {}
            continue

        observed_call = observed_calls.get(message.tool_call_id) if isinstance(message.tool_call_id, str) else None
        if observed_call is None or not _is_positive_int(message.id) or message.id not in turn_result_ids:
            continue
        observed_results.setdefault(message.id, (message, observed_call))

    if not observed_results:
        return

    calls = await subagent_crud.list_calls(
        db,
        parent_work_id=parent.id,
        undelivered_only=True,
    )
    if not calls:
        return

    message_work_id = await reply_work_crud.get_message_work_id(db, parent)
    candidates: dict[tuple[str, int], list[tuple[int, dict[str, Any]]]] = {}
    for observed_message, observed_call in observed_results.values():
        persisted = await message_crud.get(db, observed_message.id)
        if persisted is None:
            continue
        if (
            persisted.id != observed_message.id
            or persisted.uid != parent.uid
            or persisted.session_id != parent.session_id
            or persisted.profile_id != parent.profile_id
            or persisted.role != MessageRole.TOOL
            or persisted.type != MessageType.TOOL_RESULT
            or persisted.work_id != message_work_id
            or persisted.audit_record_id is not None
        ):
            _invalid_context()
        try:
            stored = InternalMessage.model_validate_json(persisted.content or "{}")
        except (TypeError, ValueError):
            continue
        if stored.role != MessageRole.TOOL or stored.tool_call_id != observed_message.tool_call_id:
            continue

        if stored.content != observed_message.content:
            continue

        try:
            payload = json.loads(stored.content or "{}")
        except (TypeError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue

        arguments = observed_call.arguments
        subagent_id = arguments["subagent_id"]
        if payload.get("subagent_id") != subagent_id:
            continue
        child_work_id = payload.get("work_id")
        if not _is_positive_int(child_work_id):
            continue
        if "work_id" in arguments and arguments.get("work_id") != child_work_id:
            continue

        candidates.setdefault((subagent_id, child_work_id), []).append((observed_message.id, payload))

    if not candidates:
        return

    accepted_tool_call_ids: set[str] = set()
    for call in calls:
        call_candidates = candidates.get((call.subagent_id, call.child_work_id))
        if not call_candidates:
            continue

        matching_wait = False
        has_tool_binding = False
        for slot in child_work_waits:
            if not isinstance(slot, dict):
                continue
            if slot.get("tool_call_id") == call.tool_call_id:
                has_tool_binding = True
            if slot.get("child_work_id") == call.child_work_id and slot.get("subagent_id") == call.subagent_id and slot.get("tool_call_id") == call.tool_call_id and slot.get("tool_result_message_id") == call.tool_result_message_id:
                matching_wait = True
                break
        if not matching_wait:
            if has_tool_binding:
                _invalid_context()
            continue

        if (
            call.parent_work_id != parent.id
            or call.delivered is not False
            or not isinstance(call.tool_call_id, str)
            or not call.tool_call_id.strip()
            or not isinstance(call.arguments, dict)
            or not isinstance(call.arguments.get("operation"), str)
            or not call.arguments["operation"].strip()
            or not _is_positive_int(call.tool_result_message_id)
        ):
            _invalid_context()

        subagent = await subagent_crud.get_scoped(
            db,
            subagent_id=call.subagent_id,
            uid=parent.uid,
            session_id=parent.session_id,
            profile_id=parent.profile_id,
            lock=False,
            touch=False,
        )
        if subagent is None:
            _invalid_context()
        round_row = await subagent_crud.get_round(
            db,
            child_work_id=call.child_work_id,
            subagent_id=call.subagent_id,
        )
        if round_row is None:
            _invalid_context()
        await load_child_work(db, subagent=subagent, round_row=round_row)
        snapshot = await get_child_snapshot(db, subagent=subagent, round_row=round_row)
        if not isinstance(snapshot, dict) or snapshot.get("completed") is not True:
            continue

        for observed_message_id, payload in call_candidates:
            if payload != build_subagent_work_result(call.subagent_id, snapshot):
                continue
            if observed_message_id <= call.tool_result_message_id:
                continue

            await _apply_child_call_result(
                db,
                parent=parent,
                call=call,
                round_row=round_row,
                snapshot=snapshot,
                messages=messages,
                turn_messages=turn_messages,
            )
            accepted_tool_call_ids.add(call.tool_call_id)
            break

    if not accepted_tool_call_ids:
        return

    remaining_waits = [slot for slot in child_work_waits if not isinstance(slot, dict) or not isinstance(slot.get("tool_call_id"), str) or slot.get("tool_call_id") not in accepted_tool_call_ids]
    if remaining_waits:
        execution_state["child_work_waits"] = remaining_waits
    else:
        execution_state.pop("child_work_waits", None)
    checkpoint["messages"] = _serialize_checkpoint_messages(messages)
    if turn_messages is not None:
        checkpoint["turn_messages"] = _serialize_checkpoint_messages(turn_messages)


async def supersede_child_reports(
    db: AsyncSession,
    *,
    parent: SessionReplyWorkItem,
    child_work_id: int,
) -> None:
    if not _is_positive_int(child_work_id):
        return

    parent_work_ids = await subagent_crud.list_parent_ids_for_child(
        db,
        child_work_id=child_work_id,
        undelivered_only=False,
    )
    for parent_work_id in parent_work_ids:
        if parent_work_id == parent.id:
            continue

        candidate = await reply_work_crud.get(db, parent_work_id)
        if candidate is None:
            continue
        work = await subagent_crud.lock_parent_for_delivery(
            db,
            work_id=parent_work_id,
            uid=parent.uid,
            session_id=parent.session_id,
            profile_id=parent.profile_id,
            identity=candidate.dedupe_key,
        )
        if work is None or work.work_category != SessionReplyWorkCategory.FOREGROUND:
            continue

        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        raw_child_slots = state.get("child_work_waits")
        child_slots = raw_child_slots if isinstance(raw_child_slots, list) else []
        moved_slots = [slot for slot in child_slots if isinstance(slot, dict) and _is_positive_int(slot.get("child_work_id")) and slot.get("child_work_id") == child_work_id]

        checkpoint = await reply_work_crud.get_checkpoint(db, work.id)
        raw_pending_ids = checkpoint.get("pending_resume_message_ids") if isinstance(checkpoint, dict) else None
        pending_ids = list(raw_pending_ids) if isinstance(raw_pending_ids, list) else []
        target_message_ids: set[int] = set()
        has_other_subagent_notification = False
        if pending_ids:
            for pending_id in pending_ids:
                if not _is_positive_int(pending_id):
                    continue
                marker = await message_crud.get(db, pending_id)
                if marker is None:
                    continue
                is_target, is_subagent_notification = _read_subagent_notification(
                    marker,
                    work=work,
                    child_work_id=child_work_id,
                )
                if is_target:
                    target_message_ids.add(pending_id)
                elif is_subagent_notification:
                    has_other_subagent_notification = True

        if not moved_slots and not target_message_ids:
            continue

        if moved_slots:
            remaining_child_slots = [slot for slot in child_slots if slot not in moved_slots]
            if remaining_child_slots:
                state["child_work_waits"] = remaining_child_slots
            else:
                state.pop("child_work_waits", None)
            raw_receipt_slots = state.get("child_work_receipts")
            if raw_receipt_slots is None:
                receipt_slots: list[Any] = []
            elif isinstance(raw_receipt_slots, list):
                receipt_slots = raw_receipt_slots
            else:
                _invalid_context()
            state["child_work_receipts"] = [*receipt_slots, *moved_slots]

        if isinstance(state.get("subagent_board_cursors"), dict):
            board_cursors = dict(state["subagent_board_cursors"])
            board_cursors.pop(str(child_work_id), None)
            board_cursors.pop(child_work_id, None)
            if board_cursors:
                state["subagent_board_cursors"] = board_cursors
            else:
                state.pop("subagent_board_cursors", None)

        remaining_pending_ids = pending_ids
        if target_message_ids:
            if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("messages"), list):
                _invalid_context()
            messages = _parse_checkpoint_messages(checkpoint["messages"])
            turn_messages = None
            if "turn_messages" in checkpoint:
                turn_messages = _parse_checkpoint_messages(checkpoint["turn_messages"])
            remaining_pending_ids = [message_id for message_id in pending_ids if message_id not in target_message_ids]
            messages = [message for message in messages if message.id not in target_message_ids]
            checkpoint["messages"] = _serialize_checkpoint_messages(messages)
            if turn_messages is not None:
                checkpoint["turn_messages"] = _serialize_checkpoint_messages([message for message in turn_messages if message.id not in target_message_ids])
            if remaining_pending_ids:
                checkpoint["pending_resume_message_ids"] = remaining_pending_ids
            else:
                checkpoint.pop("pending_resume_message_ids", None)

        was_board_review_pending = state.get("subagent_board_review_pending") is True
        unique_child_review = was_board_review_pending and bool(target_message_ids) and not has_other_subagent_notification
        has_other_ready = bool(remaining_pending_ids) or _has_other_external_delivery(state, checkpoint if isinstance(checkpoint, dict) else None)
        lost_ready = work.status == SessionReplyWorkStatus.READY_FOR_LLM and unique_child_review and not has_other_ready
        must_wait_or_finish = lost_ready or (work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK and not has_other_ready)
        if unique_child_review or must_wait_or_finish:
            state.pop("subagent_board_review_pending", None)
        if must_wait_or_finish:
            if _has_other_child_waits(state):
                state.pop("external_resume_pending", None)
                if lost_ready:
                    work.status = SessionReplyWorkStatus.WAITING_EXTERNAL_WORK
                    work.locked_by = None
                    work.lock_until = None
            elif isinstance(checkpoint, dict) and checkpoint.get("files_to_user"):
                if work.status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK:
                    if not await _wake_waiting_parent(
                        db,
                        work=work,
                        state=state,
                        checkpoint=checkpoint,
                        event_dedupe_key=f"subagent-transfer-file-ready:{work.id}:{child_work_id}",
                    ):
                        _invalid_context()
                    continue
            else:
                state.pop("external_resume_pending", None)
                work.result_message_id = None
                work.error = None
                response = build_identified_work_response(
                    work,
                    {
                        "skip_reply": True,
                        "history": [],
                    },
                )
                state["response"] = response
                work.status = SessionReplyWorkStatus.SUCCEEDED
                work.locked_by = None
                work.lock_until = None
                await external_work_crud.finish_received_results(
                    db,
                    work=work,
                    allow_prior_sequence=True,
                )
                message_work_id = await reply_work_crud.get_message_work_id(db, work)
                work.event_sent = False
                if message_work_id is None:
                    event = _event_for_work(work, response)
                    event.update({"type": "done", "skip_reply": True, "response": response})
                    await session_event_crud.publish(
                        db,
                        dedupe_key=event["event_id"],
                        uid=work.uid,
                        session_id=work.session_id,
                        event=event,
                        commit=False,
                    )
                    work.event_sent = True

        work.execution_state = state
        db.add(work)
        if target_message_ids:
            await reply_work_crud.save_checkpoint(db, work.id, checkpoint)


async def resume_child_work_for_parent(
    db: AsyncSession,
    *,
    parent_work_id: int,
) -> bool:
    parent = await reply_work_crud.get(db, parent_work_id)
    if parent is None:
        return False
    try:
        await db.refresh(parent)
        work = await subagent_crud.lock_parent_for_delivery(
            db,
            work_id=parent_work_id,
            uid=parent.uid,
            session_id=parent.session_id,
            profile_id=parent.profile_id,
            identity=parent.dedupe_key,
        )
        if work is None:
            await db.rollback()
            return False
        parent = work

        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        child_slots = state.get("child_work_waits")
        receipt_slots = state.get("child_work_receipts")
        if not isinstance(child_slots, list):
            child_slots = []
        if not isinstance(receipt_slots, list):
            receipt_slots = []
        if not child_slots and not receipt_slots:
            await db.rollback()
            return False

        validated_slots: list[tuple[dict[str, Any], Any, Any, bool]] = []
        seen_slot_keys: set[tuple[int, str]] = set()
        for index, slot in enumerate([*child_slots, *receipt_slots]):
            wakeable = index < len(child_slots)
            if not isinstance(slot, dict):
                _invalid_context()
            child_work_id = slot.get("child_work_id")
            subagent_id = slot.get("subagent_id")
            tool_call_id = slot.get("tool_call_id")
            message_id = slot.get("tool_result_message_id")
            if not _is_positive_int(child_work_id) or not isinstance(subagent_id, str) or not subagent_id.strip() or not isinstance(tool_call_id, str) or not tool_call_id.strip() or not _is_positive_int(message_id) or ("tool_name" in slot and slot.get("tool_name") != AGENT_WORK_TOOL_NAME):
                _invalid_context()
            slot_key = (child_work_id, tool_call_id)
            if slot_key in seen_slot_keys:
                _invalid_context()
            seen_slot_keys.add(slot_key)

            call = await subagent_crud.get_call(
                db,
                parent_work_id=parent_work_id,
                tool_call_id=tool_call_id,
            )
            if call is None or call.parent_work_id != parent_work_id or call.tool_call_id != tool_call_id or call.child_work_id != child_work_id or call.subagent_id != subagent_id or call.tool_result_message_id != message_id:
                _invalid_context()

            subagent = await subagent_crud.get_scoped(
                db,
                subagent_id=subagent_id,
                uid=parent.uid,
                session_id=parent.session_id,
                profile_id=parent.profile_id,
                lock=False,
                touch=False,
            )
            if subagent is None:
                _invalid_context()

            round_row = await subagent_crud.get_round(
                db,
                child_work_id=child_work_id,
                subagent_id=subagent_id,
            )
            if round_row is None or not _is_positive_int(round_row.current_work_id):
                _invalid_context()
            child_work = await reply_work_crud.get(db, round_row.current_work_id)
            child_state = child_work.execution_state if child_work is not None and isinstance(child_work.execution_state, dict) else {}
            if child_work is None or child_work.id != round_row.current_work_id or child_work.uid != parent.uid or child_work.session_id != parent.session_id or child_work.profile_id != parent.profile_id or child_work.subagent_id != subagent_id or child_state.get("child_work_id", child_work.id) != child_work_id:
                _invalid_context()
            validated_slots.append((slot, call, round_row, wakeable))

        existing_board_cursors = state.get("subagent_board_cursors")
        if existing_board_cursors is None:
            board_cursors: dict[str, Any] = {}
        elif not isinstance(existing_board_cursors, dict):
            _invalid_context()
        else:
            board_cursors = dict(existing_board_cursors)

        has_new_result = False
        has_receipt_result = False
        new_board_posts: list[SubagentBoardPost] = []
        checked_board_work_ids: set[int] = set()
        for _slot, call, round_row, wakeable in validated_slots:
            if not wakeable:
                if call.delivered or isinstance(round_row.result, dict):
                    has_receipt_result = True
                continue
            if isinstance(round_row.result, dict) and call.delivered is False:
                has_new_result = True
            if isinstance(round_row.result, dict) or call.delivered is not False or round_row.child_work_id in checked_board_work_ids:
                continue
            checked_board_work_ids.add(round_row.child_work_id)
            latest_post = await subagent_crud.get_latest_board_post(
                db,
                uid=parent.uid,
                session_id=parent.session_id,
                profile_id=parent.profile_id,
                subagent_id=call.subagent_id,
                child_work_id=round_row.child_work_id,
            )
            if latest_post is None:
                continue
            if not _is_positive_int(latest_post.id):
                _invalid_context()
            child_work_key = str(round_row.child_work_id)
            cursor = board_cursors.get(child_work_key, 0)
            if not isinstance(cursor, int) or isinstance(cursor, bool) or cursor < 0:
                _invalid_context()
            if latest_post.id > cursor:
                board_cursors[child_work_key] = latest_post.id
                new_board_posts.append(latest_post)

        if not has_new_result and not new_board_posts and not has_receipt_result:
            await db.rollback()
            return False

        has_active_progress = has_new_result or bool(new_board_posts)
        if has_active_progress and work.status == SessionReplyWorkStatus.SUCCEEDED:
            if not await external_work_crud.reopen_completed_waiting(db, work=work):
                await db.rollback()
                return False
            await db.refresh(work)
            state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
            child_slots = state.get("child_work_waits")
            if not isinstance(child_slots, list) or not child_slots:
                _invalid_context()
        checkpoint = await reply_work_crud.get_checkpoint(db, parent_work_id)
        if not isinstance(checkpoint, dict) or not isinstance(checkpoint.get("messages"), list):
            _invalid_context()
        messages = _parse_checkpoint_messages(checkpoint["messages"])
        turn_messages = None
        if "turn_messages" in checkpoint:
            turn_messages = _parse_checkpoint_messages(checkpoint["turn_messages"])
        pending_resume_message_ids: list[Any] = []
        existing_pending_resume_message_ids = checkpoint.get("pending_resume_message_ids")
        if isinstance(existing_pending_resume_message_ids, list):
            for message_id in existing_pending_resume_message_ids:
                if message_id not in pending_resume_message_ids:
                    pending_resume_message_ids.append(message_id)

        remaining_slots: list[dict[str, Any]] = []
        remaining_receipts: list[dict[str, Any]] = []
        delivered_child_work_ids: list[int] = []
        is_live_parent = not isinstance(state.get("frozen_context_message_ids"), list)
        checkpoint_message_ids = {message.id for message in messages if _is_positive_int(message.id)}
        turn_message_ids = {message.id for message in turn_messages if _is_positive_int(message.id)} if turn_messages is not None else set()
        pending_notifications: list[tuple[InternalMessage, str, bool]] = []
        for slot, call, round_row, wakeable in validated_slots:
            snapshot = round_row.result
            if not isinstance(snapshot, dict):
                if wakeable:
                    remaining_slots.append(slot)
                elif not call.delivered:
                    remaining_receipts.append(slot)
                continue
            if call.delivered:
                continue

            if wakeable:
                delivered_child_work_ids.append(round_row.child_work_id)
            call_id, replacement_payload, needs_resume_message = await _apply_child_call_result(
                db,
                parent=parent,
                call=call,
                round_row=round_row,
                snapshot=snapshot,
                messages=messages,
                turn_messages=turn_messages,
            )
            if wakeable:
                if is_live_parent or needs_resume_message:
                    notification = _build_resume_message(
                        call_id=call_id,
                        arguments=call.arguments,
                        result=replacement_payload,
                    )
                    include_in_turn = needs_resume_message
                else:
                    notification = _build_result_ready_message(
                        subagent_id=call.subagent_id,
                        work_id=round_row.child_work_id,
                    )
                    include_in_turn = False
                pending_notifications.append(
                    (
                        notification,
                        f"subagent-result:{call.id}",
                        include_in_turn,
                    )
                )

        for post in new_board_posts:
            board_message = InternalMessage(
                role=MessageRole.USER,
                type=MessageType.SUBAGENT_RESULT,
                content=json.dumps(
                    {
                        "type": "subagent_board_update",
                        "source": "subagent",
                        "subagent_id": post.subagent_id,
                        "work_id": post.child_work_id,
                        "post_id": post.id,
                        "stage": post.stage,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            pending_notifications.append(
                (
                    board_message,
                    f"subagent-board:{parent_work_id}:{post.id}",
                    False,
                )
            )

        if pending_notifications:
            message_work_id = await reply_work_crud.get_message_work_id(db, parent)
            for notification, dedupe_key, include_in_turn in pending_notifications:
                marker = await save_message(
                    db,
                    parent.session_id,
                    parent.uid,
                    MessageRole.USER,
                    MessageType.SUBAGENT_RESULT,
                    notification,
                    parent.profile_id,
                    is_processed=True,
                    dedupe_key=dedupe_key,
                    commit=False,
                    work_id=message_work_id,
                )
                if not _is_positive_int(marker.id):
                    _invalid_context()
                notification.id = marker.id
                notification.created_at = marker.created_at
                if marker.id not in pending_resume_message_ids:
                    pending_resume_message_ids.append(marker.id)
                if marker.id not in checkpoint_message_ids:
                    messages.append(notification)
                    checkpoint_message_ids.add(marker.id)
                if include_in_turn and turn_messages is not None and marker.id not in turn_message_ids:
                    turn_messages.append(notification.model_copy(deep=True))
                    turn_message_ids.add(marker.id)

        checkpoint["messages"] = _serialize_checkpoint_messages(messages)
        if turn_messages is not None:
            checkpoint["turn_messages"] = _serialize_checkpoint_messages(turn_messages)
        if pending_resume_message_ids:
            checkpoint["pending_resume_message_ids"] = pending_resume_message_ids
        else:
            checkpoint.pop("pending_resume_message_ids", None)

        state.pop("child_work_waits", None)
        if remaining_slots:
            state["child_work_waits"] = remaining_slots
        state.pop("child_work_receipts", None)
        if remaining_receipts:
            state["child_work_receipts"] = remaining_receipts
        remaining_child_work_keys: set[str] = set()
        for slot in remaining_slots:
            if isinstance(slot, dict) and _is_positive_int(slot.get("child_work_id")):
                remaining_child_work_keys.add(str(slot["child_work_id"]))
        for child_work_key in list(board_cursors):
            if child_work_key not in remaining_child_work_keys:
                board_cursors.pop(child_work_key, None)
        if board_cursors:
            state["subagent_board_cursors"] = board_cursors
        else:
            state.pop("subagent_board_cursors", None)

        if not has_active_progress:
            work.execution_state = state
            db.add(work)
            await reply_work_crud.save_checkpoint(db, parent_work_id, checkpoint)
            await db.commit()
            return False

        was_board_review_pending = state.get("subagent_board_review_pending") is True
        state["subagent_board_review_pending"] = True
        checkpoint.pop("reply_skipped", None)

        if work.status == SessionReplyWorkStatus.READY_FOR_LLM:
            if not await external_work_crud.update_active_reply(
                db,
                work=work,
                values={"execution_state": state},
            ):
                await db.rollback()
                return False
            if not was_board_review_pending and work.work_category == SessionReplyWorkCategory.FOREGROUND:
                await reply_work_crud.defer_ready_board_reviews(db, uid=parent.uid, session_id=parent.session_id)
            await reply_work_crud.save_checkpoint(db, parent_work_id, checkpoint)
            await db.commit()
            return False

        if new_board_posts:
            event_dedupe_key = f"subagent-board-ready:{parent_work_id}:{max(post.id for post in new_board_posts)}"
        else:
            event_dedupe_key = f"subagent-work-ready:{parent_work_id}:{max(delivered_child_work_ids)}"
        if not await _wake_waiting_parent(
            db,
            work=work,
            state=state,
            checkpoint=checkpoint,
            event_dedupe_key=event_dedupe_key,
        ):
            await db.rollback()
            return False
        await db.commit()
        return True
    except Exception:
        await db.rollback()
        raise


async def deliver_child_work_results(
    db: AsyncSession,
    *,
    child_work_id: int,
) -> int:
    parent_work_ids = await subagent_crud.list_parent_ids_for_child(
        db,
        child_work_id=child_work_id,
    )
    delivered_count = 0
    for parent_work_id in parent_work_ids:
        if await resume_child_work_for_parent(db, parent_work_id=parent_work_id):
            delivered_count += 1
    return delivered_count
