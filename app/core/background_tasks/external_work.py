import json
from typing import Any

from app.core.audit.confirmation import update_confirmation_message_status
from app.core.audit.confirmation_results import replace_pending_tool_result
from app.core.constants import ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION, ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN, TERMINAL_WATCH_TOOL_NAME
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.event import session_event_crud
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.session import session_crud
from app.core.crud.task.background import background_task_crud
from app.core.crud.task.interrupted import interrupt_background_tasks
from app.core.i18n import t
from app.core.log import get_logger
from app.core.prompts import EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT, PROMPT_TOOL_INTERRUPTED, TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT
from app.core.terminal.schemas import TERMINAL_SESSION_FINAL_STATUSES, TerminalSessionStatus
from app.core.utils.background_task_result import build_background_task_failure_result
from app.core.utils.dispatcher.save_message import save_message
from app.core.utils.time import get_local_time
from app.models.background_task import BackgroundTask, BackgroundTaskStatus
from app.models.message import InternalMessage, MessageRole, MessageType
from app.providers.database import AsyncSessionLocal

__all__ = ["resume_external_work_for_task", "reconcile_external_work", "recover_interrupted_external_work"]

logger = get_logger(__name__)
_PENDING_STATUSES = {BackgroundTaskStatus.PENDING.value, BackgroundTaskStatus.RUNNING.value}


def _unknown_error() -> str:
    return t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN)


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _terminal_auto_watch_continuation(task: BackgroundTask) -> dict[str, Any] | None:
    if task.tool_name != TERMINAL_WATCH_TOOL_NAME or _status_value(task.status) != BackgroundTaskStatus.SUCCEEDED.value:
        return None
    extra = task.extra if isinstance(task.extra, dict) else {}
    if extra.get("auto_terminal_watch") is True:
        return None
    result = task.result if isinstance(task.result, dict) else None
    content = result.get("content") if result is not None else None
    if not isinstance(content, dict):
        return None
    terminal_session_id = content.get("terminal_session_id")
    if not isinstance(terminal_session_id, str) or not terminal_session_id.strip():
        return None
    try:
        session_status = TerminalSessionStatus(content.get("session_status"))
    except (TypeError, ValueError):
        return None
    if session_status in TERMINAL_SESSION_FINAL_STATUSES:
        return None
    after_offset = content.get("next_offset")
    if not _is_non_negative_int(after_offset):
        arguments = task.arguments if isinstance(task.arguments, dict) else {}
        after_offset = arguments.get("after_offset")
    if not _is_non_negative_int(after_offset):
        return None
    return {
        "uid": task.uid,
        "session_id": task.session_id,
        "profile_id": task.profile_id,
        "terminal_session_id": terminal_session_id,
        "after_offset": after_offset,
        "origin_work_id": extra.get("origin_work_id"),
        "origin_work_identity": extra.get("origin_work_identity"),
    }


def _slot(value: Any) -> tuple[int, str, str, int, str | None]:
    if not isinstance(value, dict):
        raise ValueError(_unknown_error())
    task_id, call_id, name, message_id, response_id = (value.get(key) for key in ("task_id", "tool_call_id", "tool_name", "tool_result_message_id", "response_id"))
    if not _is_positive_int(task_id) or not isinstance(call_id, str) or not call_id.strip() or not isinstance(name, str) or not name.strip() or not _is_positive_int(message_id) or (response_id is not None and not isinstance(response_id, str)):
        raise ValueError(_unknown_error())
    return task_id, call_id, name, message_id, response_id


def _status_value(status: Any) -> Any:
    return getattr(status, "value", status)


def _build_result(task: BackgroundTask) -> tuple[Any, str, str]:
    status = _status_value(task.status)
    extra = task.extra if isinstance(task.extra, dict) else {}
    if extra.get("audit_execution_unknown") or extra.get("execution_unknown"):
        payload, execution_status = {"status": "execution_unknown", "error": PROMPT_TOOL_INTERRUPTED}, "execution_unknown"
    elif status == BackgroundTaskStatus.CANCELLED.value:
        if task.started_at is not None and task.tool_name != TERMINAL_WATCH_TOOL_NAME:
            payload, execution_status = {"status": "execution_unknown", "error": PROMPT_TOOL_INTERRUPTED}, "execution_unknown"
        else:
            payload, execution_status = {"status": "cancelled", "error": t(ERR_BACKGROUND_TASK_CANCELLED_BEFORE_EXECUTION)}, "failed"
    elif status == BackgroundTaskStatus.SUCCEEDED.value:
        result = task.result
        payload, execution_status = (result.get("content") if isinstance(result, dict) and "content" in result else result), "succeeded"
    elif status == BackgroundTaskStatus.FAILED.value:
        error = task.error if isinstance(task.error, str) else str(task.error or "")
        result = task.result if isinstance(task.result, dict) else None
        payload, execution_status = (result.get("content") if result is not None and result.get("content") is not None else result or build_background_task_failure_result(task.tool_name, error)), "failed"
    else:
        raise ValueError(_unknown_error())
    return payload, payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False), execution_status


async def _resume_waiting(work_id: int, uid: str, session_id: str, identity: str) -> bool:
    async with AsyncSessionLocal() as db:
        try:
            work = await external_work_crud.lock_waiting(db, work_id=work_id, uid=uid, session_id=session_id, identity=identity)
            if work is None:
                await db.rollback()
                return False
            session = await session_crud.get_by_session_id(db, session_id)
            if session is None or session.uid != uid or session.profile_id != work.profile_id:
                raise ValueError(_unknown_error())
            state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
            pending_tools = state.get("pending_tools")
            if not isinstance(pending_tools, list) or not pending_tools:
                raise ValueError(_unknown_error())

            slots = [_slot(item) for item in pending_tools]
            seen_task_ids: set[int] = set()
            seen_call_ids: set[str] = set()
            task_entries: list[tuple[tuple[int, str, str, int, str | None], BackgroundTask]] = []
            for slot in slots:
                task_id, call_id, name, _message_id, _response_id = slot
                if task_id in seen_task_ids or call_id in seen_call_ids:
                    raise ValueError(_unknown_error())
                seen_task_ids.add(task_id)
                seen_call_ids.add(call_id)
                task = await background_task_crud.get(db, task_id)
                extra = task.extra if task is not None and isinstance(task.extra, dict) else {}
                if task is None or task.uid != uid or task.session_id != session_id or task.profile_id != work.profile_id or extra.get("origin_work_id") != work.id or extra.get("origin_work_identity") != work.dedupe_key or task.tool_call_id != call_id or task.tool_name != name:
                    raise ValueError(_unknown_error())
                task_entries.append((slot, task))
            if any(_status_value(task.status) in _PENDING_STATUSES for _slot_data, task in task_entries):
                await db.rollback()
                return False
            interrupted = any(isinstance(task.extra, dict) and task.extra.get("restart_interrupted") is True for _slot_data, task in task_entries)

            marker_payloads: list[dict[str, Any]] = []
            message_snapshots: list[dict[str, Any]] = []
            result_entries: list[tuple[tuple[int, str, str, int, str | None], str, Any, str, int]] = []
            for slot, task in task_entries:
                message_id = slot[3]
                pending_message = await message_crud.get(db, message_id)
                if pending_message is None or pending_message.uid != uid or pending_message.session_id != session_id or pending_message.profile_id != work.profile_id or pending_message.role != MessageRole.TOOL or pending_message.type != MessageType.TOOL_RESULT:
                    raise ValueError(_unknown_error())
                try:
                    stored = InternalMessage.model_validate_json(pending_message.content or "{}")
                except (TypeError, ValueError) as exc:
                    raise ValueError(_unknown_error()) from exc
                try:
                    stored_payload = json.loads(stored.content or "{}")
                except (TypeError, ValueError) as exc:
                    raise ValueError(_unknown_error()) from exc
                stored_task_id = stored_payload.get("task_id") if isinstance(stored_payload, dict) else None
                if not isinstance(stored_payload, dict) or stored_payload.get("status") != "queued" or not _is_positive_int(stored_task_id) or stored_task_id != task.id:
                    raise ValueError(_unknown_error())
                if stored.role != MessageRole.TOOL or not isinstance(stored.tool_call_id, str) or not stored.tool_call_id or (pending_message.audit_tool_call_id is not None and pending_message.audit_tool_call_id != stored.tool_call_id):
                    raise ValueError(_unknown_error())
                if pending_message.audit_record_id is None:
                    if stored.tool_call_id != slot[1]:
                        raise ValueError(_unknown_error())
                else:
                    audit_record_id = pending_message.audit_record_id
                    if audit_record_id != task.audit_record_id or not _is_positive_int(task.audit_execution_record_id):
                        raise ValueError(_unknown_error())
                    execution = await audit_crud.get_execution_record(db, task.audit_execution_record_id)
                    if execution is None or execution.audit_record_id != audit_record_id or execution.new_tool_call_id != slot[1]:
                        raise ValueError(_unknown_error())
                    details = await audit_crud.list_tool_details(db, audit_record_id)
                    detail = next((item for item in details if item.id == execution.audit_tool_detail_id), None)
                    if detail is None or detail.original_tool_call_id != stored.tool_call_id or detail.tool_name != task.tool_name:
                        raise ValueError(_unknown_error())
                payload, result_text, execution_status = _build_result(task)
                if pending_message.audit_record_id is not None:
                    replacement = await replace_pending_tool_result(
                        db,
                        pending_message=pending_message,
                        original_tool_call_id=stored.tool_call_id,
                        content=result_text,
                        tool_execution_status=execution_status,
                        audit_record_id=pending_message.audit_record_id,
                    )
                    result_text = replacement if isinstance(replacement, str) else result_text
                extra = task.extra if isinstance(task.extra, dict) else {}
                instruction = TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT if extra.get("auto_terminal_watch") is True else EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
                marker_payload = {
                    "type": "background_tool_result",
                    "ui_only": True,
                    "original_tool_result_message_id": pending_message.id,
                    "task_id": task.id,
                    "tool_name": task.tool_name,
                    "execution_status": execution_status,
                    "instruction": instruction,
                    "task": payload,
                    "tool_call": {"id": stored.tool_call_id, "name": task.tool_name, "arguments": dict(task.arguments or {})},
                    "request_context": extra.get("request_context") if isinstance(extra.get("request_context"), list) else [],
                    "purpose": extra.get("purpose"),
                }
                marker = await save_message(db, session_id, uid, MessageRole.USER, MessageType.BACKGROUND_TASK_RESULT, json.dumps(marker_payload, ensure_ascii=False), work.profile_id, is_processed=True, dedupe_key=f"background-task:{task.id}:result", commit=False)
                if not _is_positive_int(marker.id):
                    raise ValueError(_unknown_error())
                marker_content = marker.content if isinstance(marker.content, str) else json.dumps(marker_payload, ensure_ascii=False)
                message_snapshots.append(
                    {
                        "id": marker.id,
                        "db_id": marker.id,
                        "role": "assistant",
                        "type": MessageType.BACKGROUND_TASK_RESULT.value,
                        "content": marker_content,
                        "created_at": marker.created_at if marker.created_at is not None else None,
                    }
                )
                marker_payloads.append(marker_payload)
                result_entries.append((slot, stored.tool_call_id, payload, result_text, marker.id))

            pending_message_ids = {slot[3] for slot, _task in task_entries}
            existing_message_ids = state.get("external_tool_result_message_ids")
            external_tool_result_message_ids: list[int] = []
            seen_message_ids: set[int] = set()
            if isinstance(existing_message_ids, list):
                for message_id in existing_message_ids:
                    if _is_positive_int(message_id) and message_id not in pending_message_ids and message_id not in seen_message_ids:
                        seen_message_ids.add(message_id)
                        external_tool_result_message_ids.append(message_id)
            for _slot_data, _call_id, _payload, _result_text, marker_id in result_entries:
                if marker_id not in seen_message_ids:
                    seen_message_ids.add(marker_id)
                    external_tool_result_message_ids.append(marker_id)
            state["external_tool_result_message_ids"] = external_tool_result_message_ids
            latest_marker_id = max(marker_id for _slot_data, _call_id, _payload, _result, marker_id in result_entries)
            if bool(state.get("show_tool_calls")):
                sequence_no = await session_reply_stream_event_crud.get_latest_sequence(db, work_id=work.id)
                for slot, original_call_id, _payload, result_text, _marker_id in result_entries:
                    sequence_no += 1
                    await session_reply_stream_event_crud.publish(
                        db,
                        work_id=work.id,
                        sequence_no=sequence_no,
                        event={"type": "tool_end", "work_id": work.id, "session_id": session_id, "event_sequence_no": sequence_no, "tool_call_id": original_call_id, "name": slot[2], "result": result_text, "response_id": slot[4]},
                        commit=False,
                    )

            if bool(state.get("show_tool_calls", True)) and session.show_tool_calls:
                results_event_id = f"external-work-results:{work.id}:{latest_marker_id}"
                await session_event_crud.publish(
                    db,
                    dedupe_key=results_event_id,
                    uid=uid,
                    session_id=session_id,
                    event={
                        "type": "tool_results_update",
                        "event_id": results_event_id,
                        "session_id": session_id,
                        "work_id": work.id,
                        "messages": message_snapshots,
                    },
                    commit=False,
                )

            if interrupted:
                state.pop("pending_tools", None)
                state.pop("dispatcher_checkpoint", None)
                state.pop("active_audit_execution", None)
                state.pop("external_resume_context", None)
                state.pop("external_resume_pending", None)
                if not await external_work_crud.interrupt_waiting(db, work=work, execution_state=state, error=_unknown_error(), commit=False):
                    await db.rollback()
                    return False
                await session_event_crud.publish(db, dedupe_key=f"external-work-interrupted:{work.id}:{latest_marker_id}", uid=uid, session_id=session_id, event={"type": "work_ready", "session_id": session_id, "work_id": work.id}, commit=False)
                await db.commit()
                return True

            checkpoint_raw = state.get("dispatcher_checkpoint")
            checkpoint = dict(checkpoint_raw) if isinstance(checkpoint_raw, dict) else {}
            from app.core.utils.assistant_files import merge_assistant_files
            from app.core.utils.dispatcher.helpers import extract_files_to_user

            for _slot_data, original_call_id, _payload, result_text, _marker_id in result_entries:
                files = extract_files_to_user([InternalMessage(role=MessageRole.TOOL, tool_call_id=original_call_id, content=result_text)])
                if files:
                    checkpoint["files_to_user"] = merge_assistant_files(checkpoint.get("files_to_user", []), files)
            resume_instruction = TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT if marker_payloads and all(marker["instruction"] == TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT for marker in marker_payloads) else EXTERNAL_WORK_RESUME_INSTRUCTION_PROMPT
            checkpoint.update(
                {
                    "messages": [],
                    "turn_messages": [],
                    "reload_history": True,
                    "external_resume_messages": [InternalMessage(role=MessageRole.USER, id=None, content=json.dumps({"type": "external_work_resume", "instruction": resume_instruction, "tasks": marker_payloads}, ensure_ascii=False)).model_dump(mode="json")],
                    "context_summary_trigger_mode": "user_message",
                    "context_summary_fixed_upper_message_id": latest_marker_id,
                }
            )
            state["dispatcher_checkpoint"] = checkpoint
            state.pop("pending_tools", None)
            state.pop("active_audit_execution", None)
            state["external_resume_pending"] = True
            state["external_resume_context"] = marker_payloads

            if not await external_work_crud.wake_waiting(db, work=work, execution_state=state, commit=False):
                await db.rollback()
                return False
            await session_event_crud.publish(db, dedupe_key=f"external-work-ready:{work.id}:{latest_marker_id}", uid=uid, session_id=session_id, event={"type": "work_ready", "session_id": session_id, "work_id": work.id}, commit=False)
            await db.commit()
            return True
        except Exception:
            await db.rollback()
            raise


async def resume_external_work_for_task(task_id: int) -> bool:
    async with AsyncSessionLocal() as db:
        task = await background_task_crud.get(db, task_id)
        if task is None:
            return False
        extra = task.extra if isinstance(task.extra, dict) else {}
        work_id, identity = extra.get("origin_work_id"), extra.get("origin_work_identity")
        if not _is_positive_int(work_id) or not isinstance(identity, str) or not identity.strip():
            return False
        uid, session_id = task.uid, task.session_id
        continuation = _terminal_auto_watch_continuation(task)
        if continuation is not None:
            await background_task_crud.create_terminal_auto_watch(db, **continuation, commit=False)
            await db.commit()
    return await _resume_waiting(work_id, uid, session_id, identity)


async def recover_interrupted_external_work() -> int:
    cutoff = get_local_time()
    async with AsyncSessionLocal() as db:
        interrupted = await interrupt_background_tasks(db, before=cutoff)
        audit_record_ids: list[int] = []
        seen_audit_record_ids: set[int] = set()
        for _task_id, audit_record_id, execution_id in interrupted:
            if audit_record_id is None or execution_id is None:
                continue
            execution = await audit_crud.get_execution_record(db, execution_id)
            if execution is None:
                continue
            await audit_crud.mark_execution_unknown(
                db,
                audit_record_id=audit_record_id,
                claim_token=execution.claim_token,
                execution_record_id=execution_id,
                error_reason=_unknown_error(),
                commit=False,
            )
            if audit_record_id not in seen_audit_record_ids:
                seen_audit_record_ids.add(audit_record_id)
                audit_record_ids.append(audit_record_id)
        await db.commit()
        for audit_record_id in audit_record_ids:
            await update_confirmation_message_status(db, audit_record_id=audit_record_id)
    await reconcile_external_work()
    return len(interrupted)


async def reconcile_external_work() -> int:
    resumed_count, after_id = 0, 0
    while True:
        async with AsyncSessionLocal() as db:
            works = await external_work_crud.list_waiting(db, limit=100, after_id=after_id)
        if not works:
            break
        for work in works:
            if work.id is None:
                continue
            after_id = max(after_id, work.id)
            try:
                resumed_count += int(await _resume_waiting(work.id, work.uid, work.session_id, work.dedupe_key))
            except Exception:
                logger.bind(work_id=work.id, uid=work.uid, session_id=work.session_id).exception("External work reconciliation failed")
        if len(works) < 100:
            break
    return resumed_count
