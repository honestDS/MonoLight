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
from app.core.session_reply_queue.child_result import build_child_call_result
from app.core.utils.dispatcher.save_message import save_message
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkStatus

__all__ = ["resume_child_work_for_parent", "deliver_child_work_results"]


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

    if stored.role != MessageRole.TOOL or not isinstance(stored.tool_call_id, str) or not stored.tool_call_id.strip() or not isinstance(payload, dict) or payload.get("child_work_id") != round_row.child_work_id:
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
        if not isinstance(child_slots, list) or not child_slots:
            await db.rollback()
            return False

        validated_slots: list[tuple[dict[str, Any], Any, Any, Any]] = []
        seen_slot_keys: set[tuple[int, str]] = set()
        for slot in child_slots:
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
            validated_slots.append((slot, call, subagent, round_row))

        has_new_result = False
        for _slot, call, _subagent, round_row in validated_slots:
            if isinstance(round_row.result, dict) and call.delivered is False:
                has_new_result = True
                break
        if not has_new_result:
            await db.rollback()
            return False

        if work.status == SessionReplyWorkStatus.SUCCEEDED:
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
        delivered_child_work_ids: list[int] = []
        is_live_parent = not isinstance(state.get("frozen_context_message_ids"), list)
        checkpoint_message_ids = {message.id for message in messages if _is_positive_int(message.id)}
        message_work_id_loaded = False
        message_work_id: int | None = None
        turn_message_ids = {message.id for message in turn_messages if _is_positive_int(message.id)} if turn_messages is not None else set()
        for slot, call, _subagent, round_row in validated_slots:
            if not isinstance(round_row.result, dict):
                remaining_slots.append(slot)
                continue
            if call.delivered:
                continue
            delivered_child_work_ids.append(round_row.child_work_id)

            pending_message, stored = await _load_child_tool_result(
                db,
                parent=parent,
                call=call,
                round_row=round_row,
                message_id=slot["tool_result_message_id"],
            )
            try:
                child_result = build_child_call_result(call, round_row.result)
            except (KeyError, TypeError, ValueError) as exc:
                raise ParameterException(ERR_SUBAGENT_CONTEXT_INVALID) from exc
            execution_status = {
                "succeeded": "succeeded",
                "execution_unknown": "execution_unknown",
            }.get(round_row.result.get("status"), "failed")
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
            if replacement.role != MessageRole.TOOL or replacement.tool_call_id != stored.tool_call_id or not isinstance(replacement_payload, dict) or replacement_payload.get("child_work_id") != round_row.child_work_id:
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
                message_id=slot["tool_result_message_id"],
                call_ids=call_ids,
            )
            if turn_messages is not None:
                replaced = (
                    _replace_checkpoint_tool_result(
                        turn_messages,
                        replacement=replacement,
                        message_id=slot["tool_result_message_id"],
                        call_ids=call_ids,
                    )
                    or replaced
                )
            needs_resume_message = not has_context or not replaced
            if is_live_parent or needs_resume_message:
                resume_message = _build_resume_message(
                    call_id=stored.tool_call_id,
                    arguments=call.arguments,
                    result=replacement_payload,
                )
                if not message_work_id_loaded:
                    message_work_id = await reply_work_crud.get_message_work_id(db, parent)
                    message_work_id_loaded = True
                marker = await save_message(
                    db,
                    parent.session_id,
                    parent.uid,
                    MessageRole.USER,
                    MessageType.SUBAGENT_RESULT,
                    resume_message,
                    parent.profile_id,
                    is_processed=True,
                    dedupe_key=f"subagent-result:{call.id}",
                    commit=False,
                    work_id=message_work_id,
                )
                if not _is_positive_int(marker.id):
                    _invalid_context()
                resume_message.id = marker.id
                resume_message.created_at = marker.created_at
                if marker.id not in pending_resume_message_ids:
                    pending_resume_message_ids.append(marker.id)
                if marker.id not in checkpoint_message_ids:
                    messages.append(resume_message)
                    checkpoint_message_ids.add(marker.id)
                if needs_resume_message and turn_messages is not None and marker.id not in turn_message_ids:
                    turn_messages.append(resume_message.model_copy(deep=True))
                    turn_message_ids.add(marker.id)

            call.delivered = True
            db.add(call)

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

        if work.status == SessionReplyWorkStatus.READY_FOR_LLM:
            if not await external_work_crud.update_active_reply(
                db,
                work=work,
                values={"execution_state": state},
            ):
                await db.rollback()
                return False
            await reply_work_crud.save_checkpoint(db, parent_work_id, checkpoint)
            await db.commit()
            return False

        state["external_resume_pending"] = True
        if not await external_work_crud.wake_waiting(
            db,
            work=work,
            execution_state=state,
            commit=False,
        ):
            await db.rollback()
            return False
        checkpoint["current_turn"] = 0
        checkpoint["execution_phase"] = GOAL_EXECUTION_PHASE_RUNNING
        checkpoint.pop("reply_skipped", None)
        checkpoint["reload_history"] = not isinstance(state.get("frozen_context_message_ids"), list)
        await reply_work_crud.save_checkpoint(db, parent_work_id, checkpoint)
        latest_child_work_id = max(delivered_child_work_ids)
        await session_event_crud.publish(
            db,
            dedupe_key=f"subagent-work-ready:{parent_work_id}:{latest_child_work_id}",
            uid=parent.uid,
            session_id=parent.session_id,
            event={"type": "work_ready", "session_id": parent.session_id, "work_id": parent_work_id},
            commit=False,
        )
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
