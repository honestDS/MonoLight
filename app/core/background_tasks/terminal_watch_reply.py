import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.session.event import session_event_crud
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import (
    ensure_session_reply_work_claim,
    session_reply_work_item_crud,
)
from app.core.crud.session.session import session_crud
from app.core.crud.session.terminal_watch import get_terminal_watch_deliveries, terminal_watch_reply_crud
from app.core.crud.task.background import background_task_crud
from app.core.log import get_logger
from app.core.prompts import TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.message import Message, MessageRole, MessageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkStatus

__all__ = ["route_terminal_auto_watch_reply", "prepare_terminal_auto_watch_resume"]

logger = get_logger(__name__)


def _status_value(value: Any) -> Any:
    return getattr(value, "value", value)


def _positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _watch_arguments(task: BackgroundTask) -> tuple[str, int] | None:
    arguments = task.arguments if isinstance(task.arguments, dict) else {}
    terminal_session_id = arguments.get("terminal_session_id")
    after_offset = arguments.get("after_offset")
    if not isinstance(terminal_session_id, str) or not terminal_session_id.strip() or not isinstance(after_offset, int) or isinstance(after_offset, bool) or after_offset < 0:
        return None
    return terminal_session_id, after_offset


async def _consume_and_commit(
    db: AsyncSession,
    task: BackgroundTask,
    *,
    work_id: int,
    identity: str,
) -> None:
    if _status_value(task.reply_status) == BackgroundTaskReplyStatus.RUNNING.value:
        task.reply_status = BackgroundTaskReplyStatus.PENDING
        task.reply_locked_by = None
        task.reply_lock_until = None
        db.add(task)
        await db.flush()
    await terminal_watch_reply_crud.consume_result(db, task, work_id=work_id, identity=identity)
    await db.commit()


async def _restore_reply_inbox(db: AsyncSession, task: BackgroundTask) -> None:
    if _status_value(task.reply_status) != BackgroundTaskReplyStatus.RUNNING.value:
        return
    task.reply_status = BackgroundTaskReplyStatus.PENDING
    task.reply_locked_by = None
    task.reply_lock_until = None
    db.add(task)
    await db.flush()


def _mark_routed(task: BackgroundTask, *, work_id: int, identity: str) -> bool:
    extra = task.extra if isinstance(task.extra, dict) else {}
    deliveries = get_terminal_watch_deliveries(task)
    delivery = deliveries.get(str(work_id))
    if delivery is None or delivery["identity"] != identity:
        return False

    updated_deliveries = {key: dict(value) for key, value in deliveries.items()}
    updated_deliveries[str(work_id)] = {
        "identity": delivery["identity"],
        "consumed": delivery["consumed"],
        "routed": True,
    }
    updated_extra = dict(extra)
    updated_extra["terminal_watch_deliveries"] = updated_deliveries
    updated_extra["terminal_auto_watch_routed"] = all(candidate["routed"] or candidate["consumed"] for candidate in updated_deliveries.values())
    task.extra = updated_extra
    return True


async def _route_terminal_auto_watch_delivery(
    db: AsyncSession,
    task: BackgroundTask,
    *,
    work_id: int,
    identity: str,
) -> bool:
    work = await terminal_watch_reply_crud.lock_origin(db, task, work_id=work_id, identity=identity)
    await terminal_watch_reply_crud.supersede_pending_timeouts(db, task)

    locked_task = await terminal_watch_reply_crud.lock_result(db, task)
    if locked_task is None:
        await db.commit()
        return True
    await db.refresh(locked_task)
    task = locked_task

    deliveries = get_terminal_watch_deliveries(task)
    delivery = deliveries.get(str(work_id))
    if delivery is None or delivery["identity"] != identity or delivery["consumed"]:
        await db.commit()
        return True

    task_status = _status_value(task.status)
    if task.auto_reply is not True or _status_value(task.reply_status) not in {BackgroundTaskReplyStatus.PENDING.value, BackgroundTaskReplyStatus.RUNNING.value} or task_status not in {BackgroundTaskStatus.SUCCEEDED.value, BackgroundTaskStatus.FAILED.value}:
        await _restore_reply_inbox(db, task)
        await db.commit()
        return True

    work_status = _status_value(work.status) if work is not None else None
    invalid_work = work_status not in {
        SessionReplyWorkStatus.SUCCEEDED.value,
        SessionReplyWorkStatus.READY_FOR_LLM.value,
        SessionReplyWorkStatus.RUNNING.value,
        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value,
        SessionReplyWorkStatus.MERGED.value,
    }
    if invalid_work:
        await _consume_and_commit(db, task, work_id=work_id, identity=identity)
        return True

    session = await session_crud.get_by_session_id(db, task.session_id)
    if session is None or session.uid != task.uid or session.profile_id != task.profile_id:
        await _consume_and_commit(db, task, work_id=work_id, identity=identity)
        return True

    if work_status == SessionReplyWorkStatus.MERGED.value:
        target = await session_reply_work_item_crud.resolve_merged_target(db, work_id)
        source_state = work.execution_state if isinstance(work.execution_state, dict) else {}
        target_state = target.execution_state if target is not None and isinstance(target.execution_state, dict) else {}
        source_actor_uid = source_state.get("actor_uid") or work.uid
        target_actor_uid = None
        if target is not None:
            target_actor_uid = target_state.get("actor_uid") or target.uid
        target_status = _status_value(target.status) if target is not None else None
        if (
            target is None
            or target.uid != work.uid
            or target.session_id != work.session_id
            or target.profile_id != work.profile_id
            or target_actor_uid != source_actor_uid
            or target_status
            not in {
                SessionReplyWorkStatus.SUCCEEDED.value,
                SessionReplyWorkStatus.READY_FOR_LLM.value,
                SessionReplyWorkStatus.RUNNING.value,
                SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value,
            }
        ):
            await _consume_and_commit(db, task, work_id=work_id, identity=identity)
            return True

        extra = task.extra if isinstance(task.extra, dict) else {}
        updated_extra = dict(extra)
        updated_deliveries = {key: dict(value) for key, value in get_terminal_watch_deliveries(task).items()}
        updated_deliveries[str(work_id)] = {"identity": identity, "consumed": True, "routed": True}
        target_key = str(target.id)
        existing_target_delivery = updated_deliveries.get(target_key)
        if not (isinstance(existing_target_delivery, dict) and existing_target_delivery.get("identity") == target.dedupe_key and existing_target_delivery.get("consumed") is True):
            updated_deliveries[target_key] = {
                "identity": target.dedupe_key,
                "consumed": False,
                "routed": False,
            }
        updated_extra["terminal_watch_deliveries"] = updated_deliveries
        updated_extra["terminal_auto_watch_routed"] = all(candidate["routed"] or candidate["consumed"] for candidate in updated_deliveries.values())
        task.extra = updated_extra
        task.reply_status = BackgroundTaskReplyStatus.PENDING
        task.reply_locked_by = None
        task.reply_lock_until = None
        db.add(task)
        await db.flush()
        await db.commit()
        return await _route_terminal_auto_watch_delivery(
            db,
            task,
            work_id=target.id,
            identity=target.dedupe_key,
        )

    watch = _watch_arguments(task)
    result = task.result if isinstance(task.result, dict) else {}
    result_content = result.get("content")
    result_next_offset = result_content.get("next_offset") if isinstance(result_content, dict) else None
    if (
        work_status == SessionReplyWorkStatus.SUCCEEDED.value
        and isinstance(result_content, dict)
        and result_content.get("event") == "process_exit"
        and result_content.get("eof") is True
        and watch is not None
        and isinstance(result_next_offset, int)
        and not isinstance(result_next_offset, bool)
        and result_next_offset >= 0
        and await terminal_watch_reply_crud.has_delivered_terminal_read(
            db,
            uid=task.uid,
            session_id=task.session_id,
            profile_id=task.profile_id,
            terminal_session_id=watch[0],
            start_offset=watch[1],
            next_offset=result_next_offset,
        )
    ):
        await _restore_reply_inbox(db, task)
        _mark_routed(task, work_id=work_id, identity=identity)
        db.add(task)
        await terminal_watch_reply_crud.consume_result(db, task, work_id=work_id, identity=identity)
        await db.commit()
        return True

    await _restore_reply_inbox(db, task)
    _mark_routed(task, work_id=work_id, identity=identity)
    db.add(task)
    if work_status == SessionReplyWorkStatus.SUCCEEDED.value:
        reopened = await terminal_watch_reply_crud.reopen_completed(db, work)
        if reopened:
            _event, created = await session_event_crud.publish(
                db,
                dedupe_key=f"terminal-auto-watch-ready:{work.id}:{task.id}",
                uid=work.uid,
                session_id=work.session_id,
                event={"type": "work_ready", "session_id": work.session_id, "work_id": work.id},
                commit=False,
            )
            if created:
                logger.bind(work_id=work.id, task_id=task.id, event=result_content.get("event") if isinstance(result_content, dict) else None).info("Terminal auto-watch work awakened")
    elif work_status == SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value:
        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        state.update({"terminal_auto_resume": True, "external_resume_pending": True})
        woke = await external_work_crud.wake_waiting(db, work=work, execution_state=state, commit=False)
        if woke:
            _event, created = await session_event_crud.publish(
                db,
                dedupe_key=f"terminal-auto-watch-ready:{work.id}:{task.id}",
                uid=work.uid,
                session_id=work.session_id,
                event={"type": "work_ready", "session_id": work.session_id, "work_id": work.id},
                commit=False,
            )
            if created:
                logger.bind(work_id=work.id, task_id=task.id, event=result_content.get("event") if isinstance(result_content, dict) else None).info("Terminal auto-watch work awakened")
    else:
        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        state["terminal_auto_resume"] = True
        work.execution_state = state
        db.add(work)
    await db.commit()
    return True


async def route_terminal_auto_watch_reply(db: AsyncSession, task: BackgroundTask) -> bool:
    extra = task.extra if isinstance(task.extra, dict) else {}
    if extra.get("auto_terminal_watch") is not True:
        return False

    deliveries = get_terminal_watch_deliveries(task)
    if not deliveries:
        return False
    pending_deliveries = [(int(raw_work_id), delivery["identity"]) for raw_work_id, delivery in deliveries.items() if delivery["consumed"] is not True]
    for work_id, identity in pending_deliveries:
        await _route_terminal_auto_watch_delivery(db, task, work_id=work_id, identity=identity)
    return True


def _result_payload(task: BackgroundTask, *, original_tool_result_message_id: int | None = None) -> dict[str, Any]:
    extra = task.extra if isinstance(task.extra, dict) else {}
    result: Any = task.result
    if result is None:
        result = {"status": _status_value(task.status), "tool_name": task.tool_name, "error": task.error}
    task_result = result.get("content") if isinstance(result, dict) and result.get("content") is not None else result
    if isinstance(task_result, dict):
        task_result = {key: value for key, value in task_result.items() if key != "watch_states"}
    payload = {
        "type": "background_tool_result",
        "ui_only": True,
        "instruction": TERMINAL_AUTO_WATCH_RESUME_INSTRUCTION_PROMPT,
        "task": task_result,
        "task_id": task.id,
        "tool_name": task.tool_name,
        "execution_status": "succeeded" if _status_value(task.status) == BackgroundTaskStatus.SUCCEEDED.value else "failed",
        "tool_call": {
            "id": task.tool_call_id,
            "name": task.tool_name,
            "arguments": task.arguments or {},
        },
        "request_context": extra.get("request_context") if isinstance(extra.get("request_context"), list) else [],
        "purpose": extra.get("purpose"),
    }
    if _positive_int(original_tool_result_message_id):
        payload["original_tool_result_message_id"] = original_tool_result_message_id
    return payload


def _result_text(payload: dict[str, Any]) -> str:
    task_result = payload.get("task")
    return task_result if isinstance(task_result, str) else json.dumps(task_result, ensure_ascii=False)


async def _persist_result_marker(
    db: AsyncSession,
    *,
    work: SessionReplyWorkItem,
    task: BackgroundTask,
    payload: dict[str, Any],
) -> Message:
    dedupe_key = f"background-task:{task.id}:result"
    marker = await message_crud.get_by_dedupe_key(db, dedupe_key)
    if marker is None:
        marker = Message(
            session_id=work.session_id,
            uid=work.uid,
            role=MessageRole.USER,
            type=MessageType.BACKGROUND_TASK_RESULT,
            content=json.dumps(payload, ensure_ascii=False),
            profile_id=work.profile_id,
            is_processed=True,
            dedupe_key=dedupe_key,
        )
        await message_crud.persist(db, message=marker, commit=False)
    elif marker.uid != work.uid or marker.session_id != work.session_id or marker.profile_id != work.profile_id or marker.role not in {MessageRole.USER, MessageRole.ASSISTANT} or marker.type != MessageType.BACKGROUND_TASK_RESULT:
        raise RuntimeError("Terminal auto-watch result marker identity mismatch")
    if not _positive_int(marker.id):
        raise RuntimeError("Terminal auto-watch result marker was not persisted")
    return marker


async def prepare_terminal_auto_watch_resume(
    db: AsyncSession,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> bool:
    await ensure_session_reply_work_claim(db)
    await db.refresh(work)

    state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
    raw_terminal_watch_waits = state.get("terminal_watch_waits")
    terminal_watch_waits = list(raw_terminal_watch_waits) if isinstance(raw_terminal_watch_waits, list) else []
    payloads: list[dict[str, Any]] = []
    marker_ids: list[int] = []
    delivered_subscription_ids: set[int] = set()
    delivered_auto_task_ids: set[int] = set()
    delivered_terminal_session_ids: set[str] = set()
    subscription_entries: list[tuple[dict[str, Any], BackgroundTask, dict[str, Any], Message]] = []
    subscription_marker_ids: list[int] = []
    subscription_original_message_ids: set[int] = set()

    for task in await terminal_watch_reply_crud.list_pending_results(db, work):
        if not await terminal_watch_reply_crud.consume_result(db, task, work_id=work.id, identity=work.dedupe_key):
            continue
        result = task.result if isinstance(task.result, dict) else {}
        content = result.get("content") if isinstance(result.get("content"), dict) else {}
        task_extra = task.extra if isinstance(task.extra, dict) else {}
        task_arguments = task.arguments if isinstance(task.arguments, dict) else {}
        terminal_session_id = content.get("terminal_session_id")
        if not isinstance(terminal_session_id, str) or not terminal_session_id.strip():
            terminal_session_id = task_extra.get("terminal_session_id")
        if not isinstance(terminal_session_id, str) or not terminal_session_id.strip():
            terminal_session_id = task_arguments.get("terminal_session_id")

        completed_subscription_ids: list[int] = []
        for completed_ids in (content.get("completed_watch_task_ids"), task_extra.get("cached_completed_watch_task_ids")):
            if isinstance(completed_ids, list):
                for completed_id in completed_ids:
                    if _positive_int(completed_id) and completed_id not in completed_subscription_ids:
                        completed_subscription_ids.append(completed_id)

        delivered_subscription_for_task = False
        for subscription_id in completed_subscription_ids:
            subscription = await background_task_crud.get(db, subscription_id)
            subscription_extra = subscription.extra if subscription is not None and isinstance(subscription.extra, dict) else {}
            subscription_arguments = subscription.arguments if subscription is not None and isinstance(subscription.arguments, dict) else {}
            if (
                subscription is None
                or subscription.uid != work.uid
                or subscription.session_id != work.session_id
                or subscription.profile_id != work.profile_id
                or subscription.tool_name != task.tool_name
                or subscription.auto_reply is not False
                or _status_value(subscription.status) != BackgroundTaskStatus.SUCCEEDED.value
                or subscription_extra.get("terminal_watch_subscription") is not True
                or subscription_extra.get("origin_work_id") != work.id
                or subscription_extra.get("origin_work_identity") != work.dedupe_key
                or (isinstance(terminal_session_id, str) and subscription_arguments.get("terminal_session_id") != terminal_session_id)
            ):
                continue

            slot = next(
                (candidate for candidate in terminal_watch_waits if isinstance(candidate, dict) and candidate.get("task_id") == subscription_id),
                None,
            )
            original_message_id = slot.get("tool_result_message_id") if isinstance(slot, dict) else None
            if slot is None or slot.get("terminal_watch") is not True or subscription.tool_call_id != slot.get("tool_call_id") or not _positive_int(original_message_id):
                continue

            payload = _result_payload(subscription, original_tool_result_message_id=original_message_id)
            existing_marker = await message_crud.get_by_dedupe_key(db, f"background-task:{subscription.id}:result")
            marker = await _persist_result_marker(db, work=work, task=subscription, payload=payload)
            payloads.append(payload)
            marker_ids.append(marker.id)
            subscription_marker_ids.append(marker.id)
            subscription_original_message_ids.add(original_message_id)
            delivered_subscription_ids.add(subscription_id)
            delivered_subscription_for_task = True
            if isinstance(terminal_session_id, str) and terminal_session_id.strip():
                delivered_terminal_session_ids.add(terminal_session_id)
            if existing_marker is None:
                subscription_entries.append((slot, subscription, payload, marker))

        if not delivered_subscription_for_task:
            payload = _result_payload(task)
            marker = await _persist_result_marker(db, work=work, task=task, payload=payload)
            payloads.append(payload)
            marker_ids.append(marker.id)
        if _positive_int(task.id):
            delivered_auto_task_ids.add(task.id)
        if isinstance(terminal_session_id, str) and terminal_session_id.strip():
            delivered_terminal_session_ids.add(terminal_session_id)

    if delivered_subscription_ids or delivered_auto_task_ids:
        state["terminal_watch_waits"] = [
            slot for slot in terminal_watch_waits if not (isinstance(slot, dict) and (slot.get("task_id") in delivered_subscription_ids or (slot.get("terminal_auto_watch") is True and (slot.get("task_id") in delivered_auto_task_ids or slot.get("terminal_session_id") in delivered_terminal_session_ids))))
        ]

    if subscription_entries and _positive_int(work.id):
        session = await session_crud.get_by_session_id(db, work.session_id)
        if session is not None and session.uid == work.uid and session.profile_id == work.profile_id:
            if bool(state.get("show_tool_calls")):
                sequence_no = await session_reply_stream_event_crud.get_latest_sequence(db, work_id=work.id)
                for slot, _subscription, payload, _marker in subscription_entries:
                    sequence_no += 1
                    await session_reply_stream_event_crud.publish(
                        db,
                        work_id=work.id,
                        sequence_no=sequence_no,
                        event={
                            "type": "tool_end",
                            "work_id": work.id,
                            "session_id": work.session_id,
                            "event_sequence_no": sequence_no,
                            "tool_call_id": slot.get("tool_call_id"),
                            "name": slot.get("tool_name"),
                            "result": _result_text(payload),
                            "response_id": slot.get("response_id"),
                        },
                        commit=False,
                    )
            if bool(state.get("show_tool_calls", True)) and session.show_tool_calls:
                latest_marker_id = max(marker.id for _slot, _subscription, _payload, marker in subscription_entries)
                event_id = f"terminal-auto-watch-subscriptions:{work.id}:{latest_marker_id}"
                await session_event_crud.publish(
                    db,
                    dedupe_key=event_id,
                    uid=work.uid,
                    session_id=work.session_id,
                    event={
                        "type": "tool_results_update",
                        "event_id": event_id,
                        "session_id": work.session_id,
                        "work_id": work.id,
                        "messages": [
                            {
                                "id": marker.id,
                                "db_id": marker.id,
                                "role": "assistant",
                                "type": MessageType.BACKGROUND_TASK_RESULT.value,
                                "content": marker.content,
                                "created_at": marker.created_at.timestamp() if marker.created_at is not None else None,
                            }
                            for _slot, _subscription, _payload, marker in subscription_entries
                        ],
                    },
                    commit=False,
                )

    if payloads:
        if subscription_marker_ids:
            existing_message_ids = state.get("external_tool_result_message_ids")
            external_tool_result_message_ids: list[int] = []
            seen_message_ids: set[int] = set()
            if isinstance(existing_message_ids, list):
                for message_id in existing_message_ids:
                    if _positive_int(message_id) and message_id not in subscription_original_message_ids and message_id not in seen_message_ids:
                        seen_message_ids.add(message_id)
                        external_tool_result_message_ids.append(message_id)
            for message_id in subscription_marker_ids:
                if message_id not in seen_message_ids:
                    seen_message_ids.add(message_id)
                    external_tool_result_message_ids.append(message_id)
            state["external_tool_result_message_ids"] = external_tool_result_message_ids
        raw_checkpoint = state.get("dispatcher_checkpoint")
        checkpoint = dict(raw_checkpoint) if isinstance(raw_checkpoint, dict) else {}
        checkpoint.pop("external_resume_messages", None)
        checkpoint["pending_resume_message_ids"] = list(
            dict.fromkeys(
                [
                    *checkpoint.get("pending_resume_message_ids", []),
                    *marker_ids,
                ]
            )
        )
        checkpoint.update({"messages": [], "turn_messages": [], "reload_history": True})
        checkpoint["context_summary_trigger_mode"] = "user_message"
        old_upper = checkpoint.get("context_summary_fixed_upper_message_id")
        upper_bounds = [*marker_ids, old_upper] if _positive_int(old_upper) else marker_ids
        checkpoint["context_summary_fixed_upper_message_id"] = max(upper_bounds)
        state["dispatcher_checkpoint"] = checkpoint
        if work.input_message_ids:
            state["external_resume_pending"] = True
        else:
            state.pop("external_resume_pending", None)
        state.pop("terminal_auto_resume", None)
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={"execution_state": state},
            commit=False,
        )
        if not updated:
            raise RuntimeError("Terminal auto-watch resume lost its work claim")
        work.execution_state = state
        await db.commit()
        return True

    if state.get("terminal_auto_resume") is True:
        state.pop("terminal_auto_resume", None)
        state.pop("external_resume_pending", None)
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={"execution_state": state},
            commit=False,
        )
        if not updated:
            raise RuntimeError("Terminal auto-watch resume lost its work claim")
        work.execution_state = state
        marked = await session_reply_work_item_crud.mark_terminal(
            db,
            work_id=work.id,
            worker_id=worker_id,
            status=SessionReplyWorkStatus.SUCCEEDED,
            event_sent=True,
            commit=False,
        )
        if not marked:
            raise RuntimeError("Terminal auto-watch work could not be completed")
        await db.commit()
        return False

    await db.commit()
    return True
