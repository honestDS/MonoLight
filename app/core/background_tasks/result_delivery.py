import copy
import json
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.background_tasks.external_work import _persist_external_work_markers, build_external_work_result
from app.core.background_tasks.reply_trigger import _build_background_tool_result_payload
from app.core.background_tasks.terminal_watch_reply import _result_payload
from app.core.constants import ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN, SESSION_REPLY_WORK_CLAIM_INFO_KEY
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import ensure_session_reply_work_claim, session_reply_work_item_crud
from app.core.crud.session.terminal_watch import get_terminal_watch_deliveries
from app.core.i18n import t
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.models.background_task import BackgroundTaskReplyStatus
from app.models.message import InternalMessage, Message, MessageType
from app.models.session_reply_work_item import SessionReplyWorkStatus


def _unknown() -> ValueError:
    return ValueError(t(ERR_BACKGROUND_TASK_EXECUTION_UNKNOWN))


def _status(value: Any) -> Any:
    return getattr(value, "value", value)


def _state(work: Any) -> dict[str, Any]:
    return dict(work.execution_state) if isinstance(work.execution_state, dict) else {}


def _ids(values: list[Any]) -> list[int]:
    result: list[int] = []
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value > 0 and value not in result:
            result.append(value)
    return result


def _payload_ids(payload: dict[str, Any]) -> list[int]:
    values = [payload.get("task_id"), *[item.get("task_id") for item in (payload.get("tasks") or []) if isinstance(item, dict)]]
    return _ids(values)


def _active(status: Any) -> bool:
    return _status(status) in {
        SessionReplyWorkStatus.READY_FOR_LLM.value,
        SessionReplyWorkStatus.RUNNING.value,
        SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value,
    }


def _same_scope(work: Any, current: Any) -> bool:
    return work.uid == current.uid and work.session_id == current.session_id and work.profile_id == current.profile_id and _state(work).get("actor_uid", work.uid) == _state(current).get("actor_uid", current.uid)


def _clean_slots(slots: Any, received: set[int], auto_sessions: set[str]) -> list[dict[str, Any]]:
    if not isinstance(slots, list):
        return []
    result = []
    for slot in slots:
        if not isinstance(slot, dict):
            continue
        is_auto = slot.get("terminal_auto_watch") is True
        remove = (is_auto and str(slot.get("terminal_session_id")) in auto_sessions) or (not is_auto and slot.get("task_id") in received)
        if not remove:
            result.append(slot)
    return result


def _clean_context(payloads: Any, received: set[int]) -> list[dict[str, Any]]:
    if not isinstance(payloads, list):
        return []
    result = []
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        tasks = payload.get("tasks")
        if isinstance(tasks, list):
            remaining = [item for item in tasks if not isinstance(item, dict) or item.get("task_id") not in received]
            if remaining:
                updated = {**payload, **remaining[0]} if payload.get("task_id") in received and isinstance(remaining[0], dict) else dict(payload)
                updated["tasks"] = remaining
                result.append(updated)
        elif payload.get("task_id") not in received:
            result.append(payload)
    return result


def _clean_state(state: dict[str, Any], received: set[int], auto_sessions: set[str]) -> dict[str, Any]:
    cleaned = copy.deepcopy(state)
    for key in ("pending_tools", "terminal_watch_waits"):
        if key in cleaned:
            slots = _clean_slots(cleaned[key], received, auto_sessions)
            if slots:
                cleaned[key] = slots
            else:
                cleaned.pop(key, None)
    if isinstance(cleaned.get("terminal_auto_watch_task_ids"), list):
        task_ids = [task_id for task_id in cleaned["terminal_auto_watch_task_ids"] if task_id not in received]
        if task_ids:
            cleaned["terminal_auto_watch_task_ids"] = task_ids
        else:
            cleaned.pop("terminal_auto_watch_task_ids", None)
    if "external_resume_context" in cleaned:
        payloads = _clean_context(cleaned["external_resume_context"], received)
        if payloads:
            cleaned["external_resume_context"] = payloads
        else:
            cleaned.pop("external_resume_context", None)
    if not cleaned.get("external_resume_context"):
        cleaned.pop("external_resume_pending", None)
        cleaned.pop("terminal_auto_resume", None)
        checkpoint = cleaned.get("dispatcher_checkpoint")
        if isinstance(checkpoint, dict):
            checkpoint = dict(checkpoint)
            checkpoint.pop("deferred_external_results", None)
            cleaned["dispatcher_checkpoint"] = checkpoint
    return cleaned


def _has_waits(state: dict[str, Any]) -> bool:
    return any(isinstance(state.get(key), list) and state[key] for key in ("pending_tools", "terminal_watch_waits", "external_resume_context", "terminal_auto_watch_task_ids"))


def _read_marker(marker: Message, current: Any, task_id: int | None = None) -> dict[str, Any]:
    if marker.uid != current.uid or marker.session_id != current.session_id or marker.profile_id != current.profile_id or _status(marker.type) != MessageType.BACKGROUND_TASK_RESULT.value:
        raise _unknown()
    try:
        payload = json.loads(marker.content or "")
    except (TypeError, json.JSONDecodeError) as exc:
        raise _unknown() from exc
    tasks = payload.get("tasks") if isinstance(payload, dict) else None
    valid_tasks = tasks is None or isinstance(tasks, list) and all(isinstance(item, dict) and isinstance(item.get("task_id"), int) and not isinstance(item.get("task_id"), bool) and item["task_id"] > 0 for item in tasks)
    if not isinstance(payload, dict) or payload.get("type") != "background_tool_result" or "task" not in payload or not isinstance(payload.get("task_id"), int) or isinstance(payload.get("task_id"), bool) or payload["task_id"] <= 0 or not valid_tasks or task_id is not None and task_id not in _payload_ids(payload):
        raise _unknown()
    return payload


async def receive_pending_external_results(db: AsyncSession, *, uid: str, session_id: str, profile_id: int) -> list[InternalMessage]:
    claim = getattr(db, "info", {}).get(SESSION_REPLY_WORK_CLAIM_INFO_KEY)
    if claim is None:
        return []
    if not isinstance(claim, tuple) or len(claim) != 2 or not isinstance(claim[0], int) or isinstance(claim[0], bool) or claim[0] <= 0 or not isinstance(claim[1], str) or not claim[1].strip():
        raise _unknown()
    work_id, worker_id = claim
    await ensure_session_reply_work_claim(db)
    current = await session_reply_work_item_crud.get(db, work_id)
    if current is None or _status(current.status) != SessionReplyWorkStatus.RUNNING.value or current.locked_by != worker_id or current.uid != uid or current.session_id != session_id or current.profile_id != profile_id:
        raise _unknown()

    sources = await external_work_crud.list_result_source_works(db, work=current)
    if not any(source.id == current.id for source in sources):
        sources.append(current)
    contexts: list[dict[str, Any]] = []
    by_work_id: dict[int, dict[str, Any]] = {}
    slot_sources: dict[int, tuple[dict[str, Any], dict[str, Any]]] = {}
    payload_sources: dict[int, tuple[dict[str, Any], dict[str, Any], int]] = {}
    marker_sources: dict[int, tuple[dict[str, Any], Message, dict[str, Any], int]] = {}
    source_context_by_task: dict[int, dict[str, Any]] = {}
    candidate_ids: list[int] = []

    for source in sources:
        if not _same_scope(source, current) or (source.id != current.id and _status(source.status) == SessionReplyWorkStatus.RUNNING.value):
            continue
        state = _state(source)
        context = {"work": source, "state": state, "received": set(), "auto_sessions": set()}
        contexts.append(context)
        if source.id is not None:
            by_work_id[source.id] = context
        for key in ("pending_tools", "terminal_watch_waits"):
            for slot in state.get(key, []) if isinstance(state.get(key), list) else []:
                if isinstance(slot, dict) and isinstance(slot.get("task_id"), int) and not isinstance(slot["task_id"], bool):
                    slot_sources.setdefault(slot["task_id"], (context, slot))
                    candidate_ids.append(slot["task_id"])
        for payload in state.get("external_resume_context", []) if isinstance(state.get("external_resume_context"), list) else []:
            if isinstance(payload, dict):
                ids = _payload_ids(payload)
                if ids:
                    for task_id in ids:
                        payload_sources.setdefault(task_id, (context, payload, ids[0]))
                        candidate_ids.append(task_id)
        if isinstance(state.get("terminal_auto_watch_task_ids"), list):
            for task_id in _ids(state["terminal_auto_watch_task_ids"]):
                source_context_by_task.setdefault(task_id, context)
                candidate_ids.append(task_id)
        if _status(source.work_type) == "background_tool_summary":
            try:
                task_id = int(source.source_id)
            except (TypeError, ValueError):
                raise _unknown()
            source_context_by_task.setdefault(task_id, context)
            candidate_ids.append(task_id)
        checkpoint = state.get("dispatcher_checkpoint")
        for marker_id in _ids(checkpoint.get("pending_resume_message_ids", []) if isinstance(checkpoint, dict) else []):
            marker = await message_crud.get(db, marker_id)
            if marker is None:
                raise _unknown()
            payload = _read_marker(marker, current)
            ids = _payload_ids(payload)
            for task_id in ids:
                marker_sources.setdefault(task_id, (context, marker, payload, ids[0]))
                candidate_ids.append(task_id)

    tasks = await external_work_crud.list_completed_reply_tasks(db, work=current, task_ids=_ids(candidate_ids))
    work_cache = {source.id: source for source in sources if source.id is not None}
    work_cache[current.id] = current

    async def get_work(origin_id: Any) -> Any:
        if not isinstance(origin_id, int) or isinstance(origin_id, bool) or origin_id <= 0:
            return None
        if origin_id not in work_cache:
            work_cache[origin_id] = await session_reply_work_item_crud.get(db, origin_id)
        return work_cache[origin_id]

    async def valid_origin(origin_id: Any, identity: Any, allow_succeeded: bool = False) -> Any:
        origin = await get_work(origin_id)
        if origin is None or not _same_scope(origin, current) or not isinstance(identity, str) or origin.dedupe_key != identity:
            return None
        if origin.id != current.id and _status(origin.status) == SessionReplyWorkStatus.RUNNING.value:
            return None
        if _active(origin.status) or allow_succeeded and _status(origin.status) == SessionReplyWorkStatus.SUCCEEDED.value:
            return origin
        return None

    target_state = _state(current)
    owner_markers: dict[int, tuple[Message, dict[str, Any]]] = {}
    persisted_by_anchor: dict[int, list[int]] = {}
    marker_order: list[int] = []
    received_ids: list[int] = []

    for task in tasks:
        if task.id is None:
            continue
        extra = task.extra if isinstance(task.extra, dict) else {}
        owner = extra.get("reply_owner") if isinstance(extra.get("reply_owner"), dict) else {}
        if owner.get("status") == "handled":
            continue
        owner_work = await get_work(owner.get("work_id")) if owner.get("work_id") is not None else None
        if owner_work is not None and not _same_scope(owner_work, current):
            continue
        if owner.get("status") == "received" and owner_work is not None and _same_scope(owner_work, current) and _active(owner_work.status) and owner_work.dedupe_key == owner.get("identity"):
            if owner_work.id == current.id and owner.get("sequence_no") != current.sequence_no:
                updated_extra = dict(extra)
                updated_owner = dict(owner)
                updated_owner["sequence_no"] = current.sequence_no
                updated_extra["reply_owner"] = updated_owner
                task.extra = updated_extra
                db.add(task)
            continue

        slot_ref, payload_ref, marker_ref = slot_sources.get(task.id), payload_sources.get(task.id), marker_sources.get(task.id)
        context = slot_ref[0] if slot_ref else payload_ref[0] if payload_ref else marker_ref[0] if marker_ref else source_context_by_task.get(task.id)
        source = context["work"] if context else None
        slot = slot_ref[1] if slot_ref else None
        payload = payload_ref[1] if payload_ref else marker_ref[2] if marker_ref else None
        anchor_id = payload_ref[2] if payload_ref else marker_ref[3] if marker_ref else task.id
        marker = marker_ref[1] if marker_ref else None
        marker_id = marker.id if marker is not None else None
        owner_marker_id = owner.get("marker_id")
        if marker is None and isinstance(owner_marker_id, int) and owner_marker_id > 0:
            if owner_work is None or not (_status(owner_work.status) in {SessionReplyWorkStatus.FAILED.value, SessionReplyWorkStatus.CANCELLED.value} or owner_work.dedupe_key != owner.get("identity")):
                continue
            if owner_marker_id not in owner_markers:
                owner_marker = await message_crud.get(db, owner_marker_id)
                if owner_marker is None:
                    raise _unknown()
                owner_markers[owner_marker_id] = (owner_marker, _read_marker(owner_marker, current, task.id))
            marker, payload = owner_markers[owner_marker_id]
            marker_id = owner_marker_id

        auto_terminal = extra.get("auto_terminal_watch") is True
        deliveries = get_terminal_watch_deliveries(task)
        valid_delivery_ids: list[str] = []
        delivery_contexts: set[int] = set()
        terminal_session_id: str | None = None
        if auto_terminal:
            for raw_work_id, delivery in deliveries.items():
                if not isinstance(delivery, dict):
                    continue
                origin = await valid_origin(int(raw_work_id), delivery.get("identity"), True)
                if origin is not None:
                    if origin.id not in by_work_id:
                        origin_context = {
                            "work": origin,
                            "state": _state(origin),
                            "received": set(),
                            "auto_sessions": set(),
                        }
                        contexts.append(origin_context)
                        by_work_id[origin.id] = origin_context
                    delivery_contexts.add(origin.id)
                    if delivery.get("consumed") is not True:
                        valid_delivery_ids.append(str(raw_work_id))
                    if source is None:
                        source, context = origin, by_work_id.get(origin.id)
            if not valid_delivery_ids and marker_id is None:
                continue
        elif source is None and extra.get("origin_work_id") is not None:
            source = await valid_origin(extra.get("origin_work_id"), extra.get("origin_work_identity"))
            context = by_work_id.get(source.id) if source is not None else None

        if source is None and extra.get("origin_work_id") is not None:
            origin = await valid_origin(extra.get("origin_work_id"), extra.get("origin_work_identity"))
            if origin is None:
                if marker_id is None:
                    continue
            else:
                source = origin
                context = by_work_id.get(origin.id)
        if source is None and task.auto_reply is not True and marker_id is None:
            continue

        if marker_id is not None and (not isinstance(payload, dict) or task.id not in _payload_ids(payload)):
            raise _unknown()
        if marker_id is None:
            if payload is None:
                if auto_terminal:
                    payload = _result_payload(task)
                elif slot is not None:
                    payload, _result_text, _call_id = await build_external_work_result(db, work=source, slot=slot, task=task)
                else:
                    payload = _build_background_tool_result_payload(task)
            if not isinstance(payload, dict) or task.id not in _payload_ids(payload):
                raise _unknown()
            marker_ids = persisted_by_anchor.get(anchor_id)
            if marker_ids is None:
                marker_ids = await _persist_external_work_markers(db, work=current, state=target_state, payloads=[payload])
                if not marker_ids:
                    raise _unknown()
                persisted_by_anchor[anchor_id] = marker_ids
        else:
            marker_ids = [marker_id]
        marker_ids = _ids(marker_ids)
        if not marker_ids:
            raise _unknown()
        marker_order.extend(marker_ids)
        received_ids.append(task.id)
        updated_extra = dict(extra)
        updated_extra["reply_owner"] = {"work_id": current.id, "identity": current.dedupe_key, "sequence_no": current.sequence_no, "status": "received", "marker_id": marker_ids[0]}
        if auto_terminal:
            updated_deliveries = {key: dict(value) for key, value in deliveries.items()}
            for delivery_id in valid_delivery_ids:
                updated_deliveries[delivery_id]["consumed"] = True
                updated_deliveries[delivery_id]["routed"] = True
            updated_extra["terminal_watch_deliveries"] = updated_deliveries
            updated_extra["terminal_auto_watch_routed"] = all(value.get("consumed") is True or value.get("routed") is True for value in updated_deliveries.values())
            arguments = task.arguments if isinstance(task.arguments, dict) else {}
            terminal_session_id = arguments.get("terminal_session_id") or extra.get("terminal_session_id") or (slot or {}).get("terminal_session_id")
            if context is not None and isinstance(terminal_session_id, str):
                context["auto_sessions"].add(terminal_session_id)
        task.extra = updated_extra
        if task.auto_reply is True:
            task.reply_status = BackgroundTaskReplyStatus.RUNNING
            task.reply_locked_by = None
            task.reply_lock_until = None
        db.add(task)
        if context is not None:
            context["received"].add(task.id)
        if auto_terminal and isinstance(terminal_session_id, str):
            for delivery_work_id in delivery_contexts:
                delivery_context = by_work_id[delivery_work_id]
                delivery_context["received"].add(task.id)
                delivery_context["auto_sessions"].add(terminal_session_id)

    marker_order, received_ids = _ids(marker_order), _ids(received_ids)
    if not marker_order:
        return []
    target_state["received_external_task_ids"] = _ids([*(target_state.get("received_external_task_ids", []) if isinstance(target_state.get("received_external_task_ids"), list) else []), *received_ids])
    target_state["external_tool_result_message_ids"] = _ids([*(target_state.get("external_tool_result_message_ids", []) if isinstance(target_state.get("external_tool_result_message_ids"), list) else []), *marker_order])

    for context in contexts:
        if not context["received"]:
            continue
        source, cleaned = context["work"], _clean_state(context["state"], context["received"], context["auto_sessions"])
        if source.id == current.id:
            target_state = _clean_state(target_state, context["received"], context["auto_sessions"])
            continue
        if _status(source.status) not in {SessionReplyWorkStatus.READY_FOR_LLM.value, SessionReplyWorkStatus.WAITING_EXTERNAL_WORK.value}:
            continue
        if _has_waits(cleaned):
            values = {"execution_state": cleaned, "status": SessionReplyWorkStatus.WAITING_EXTERNAL_WORK, "locked_by": None, "lock_until": None, "merged_into_id": None}
        else:
            request_ids = cleaned.get("request_ids")
            if isinstance(request_ids, list):
                current_requests = target_state.get("request_ids") if isinstance(target_state.get("request_ids"), list) else []
                target_state["request_ids"] = list(dict.fromkeys([*current_requests, *[item for item in request_ids if isinstance(item, str)]]))
            values = {"execution_state": cleaned, "status": SessionReplyWorkStatus.MERGED, "locked_by": None, "lock_until": None, "merged_into_id": current.id}
        if not await external_work_crud.update_active_reply(db, work=source, values=values):
            raise _unknown()

    if not await session_reply_work_item_crud.update_claimed(db, work_id=current.id, worker_id=worker_id, values={"execution_state": target_state}, commit=False):
        raise _unknown()
    await db.flush()
    messages: list[Message] = []
    for marker_id in marker_order:
        marker = await message_crud.get(db, marker_id)
        if marker is None:
            raise _unknown()
        messages.append(marker)
    return parse_db_messages_to_internal(messages)
