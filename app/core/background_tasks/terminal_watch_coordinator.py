import hashlib
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.constants import TERMINAL_WATCH_TOOL_NAME
from app.core.crud.session.terminal_watch import get_terminal_watch_deliveries, terminal_watch_reply_crud
from app.core.crud.task.background import background_task_crud
from app.core.log import get_logger
from app.core.terminal.schemas import TERMINAL_SESSION_FINAL_STATUSES
from app.core.utils.background_task_result import build_background_task_success_result
from app.models.background_task import BackgroundTask, BackgroundTaskReplyStatus, BackgroundTaskStatus
from app.models.session_reply_work_item import SessionReplyWorkItem

__all__ = [
    "complete_terminal_watch_round",
    "create_terminal_auto_watch",
    "create_terminal_watch_subscription",
]


_CONTINUABLE_EVENTS = frozenset({"timeout", "output", "output_contains", "quiet_for"})


def _is_non_negative_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


async def create_terminal_auto_watch(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    terminal_session_id: str,
    after_offset: int,
    previous_task_id: int | None = None,
    origin_work_id: int | None = None,
    origin_work_identity: str | None = None,
    commit: bool = True,
) -> BackgroundTask | None:
    locked = await background_task_crud.lock_terminal_watch(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        terminal_session_id=terminal_session_id,
    )
    terminal_session = await background_task_crud.get_terminal_watch_session(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        terminal_session_id=terminal_session_id,
    )
    if not locked or terminal_session is None or (previous_task_id is None and terminal_session.status in TERMINAL_SESSION_FINAL_STATUSES):
        if commit:
            await db.commit()
        else:
            await db.flush()
        return None

    previous_task: BackgroundTask | None = None
    previous_content: dict[str, Any] | None = None
    previous_extra: dict[str, Any] = {}
    if previous_task_id is not None:
        previous_task = await background_task_crud.get(db, previous_task_id)
        if previous_task is not None:
            await db.refresh(previous_task)
        previous_extra = previous_task.extra if previous_task is not None and isinstance(previous_task.extra, dict) else {}
        previous_arguments = previous_task.arguments if previous_task is not None and isinstance(previous_task.arguments, dict) else {}
        previous_result_data = previous_task.result if previous_task is not None and isinstance(previous_task.result, dict) else {}
        candidate_content = previous_result_data.get("content")
        if (
            previous_task is None
            or previous_task.uid != uid
            or previous_task.session_id != session_id
            or previous_task.profile_id != profile_id
            or previous_task.tool_name != TERMINAL_WATCH_TOOL_NAME
            or previous_extra.get("terminal_session_id") != terminal_session_id
            or previous_arguments.get("terminal_session_id") != terminal_session_id
            or previous_extra.get("auto_terminal_watch") is not True
            or previous_task.status != BackgroundTaskStatus.SUCCEEDED
            or previous_task.auto_reply is not True
            or not isinstance(candidate_content, dict)
            or candidate_content.get("event") not in _CONTINUABLE_EVENTS
            or candidate_content.get("eof") is True
            or not _is_non_negative_int(candidate_content.get("next_offset"))
        ):
            if commit:
                await db.commit()
            else:
                await db.flush()
            return None
        previous_content = candidate_content

        next_auto_watch_task_id = previous_extra.get("next_auto_watch_task_id")
        if next_auto_watch_task_id is not None:
            next_task = await background_task_crud.get(db, next_auto_watch_task_id)
            if next_task is not None:
                await db.refresh(next_task)
            next_extra = next_task.extra if next_task is not None and isinstance(next_task.extra, dict) else {}
            next_arguments = next_task.arguments if next_task is not None and isinstance(next_task.arguments, dict) else {}
            if next_task is None or not (
                next_task.uid == uid
                and next_task.session_id == session_id
                and next_task.profile_id == profile_id
                and next_task.tool_name == TERMINAL_WATCH_TOOL_NAME
                and next_extra.get("auto_terminal_watch") is True
                and next_extra.get("terminal_session_id") == terminal_session_id
                and next_arguments.get("terminal_session_id") == terminal_session_id
            ):
                next_task = None
            if commit:
                await db.commit()
            else:
                await db.flush()
            return next_task

    if origin_work_id is None:
        origin_work_id = previous_extra.get("origin_work_id")
    if origin_work_identity is None:
        origin_work_identity = previous_extra.get("origin_work_identity")

    active_watch = await background_task_crud.get_active_terminal_watch(
        db,
        uid,
        session_id,
        terminal_session_id,
        automatic_only=True,
    )
    if active_watch is not None:
        if previous_task_id is None and isinstance(origin_work_id, int) and not isinstance(origin_work_id, bool) and origin_work_id > 0 and isinstance(origin_work_identity, str) and origin_work_identity.strip():
            active_extra = dict(active_watch.extra) if isinstance(active_watch.extra, dict) else {}
            active_extra.update(
                {
                    "origin_work_id": origin_work_id,
                    "origin_work_identity": origin_work_identity,
                }
            )
            active_watch.extra = active_extra
            db.add(active_watch)
            await db.flush()
        if commit:
            await db.commit()
        else:
            await db.flush()
        return active_watch

    cursor = after_offset if previous_content is None else previous_content["next_offset"]
    digest = hashlib.sha256(f"{terminal_session_id}:{cursor}:{previous_task_id}".encode()).hexdigest()[:32]
    extra: dict[str, Any] = {
        "source": "terminal_auto_watch",
        "auto_terminal_watch": True,
        "terminal_session_id": terminal_session_id,
    }
    if previous_content is not None and isinstance(previous_content.get("watch_states"), dict):
        extra["watch_states"] = previous_content["watch_states"]
    if isinstance(origin_work_id, int) and not isinstance(origin_work_id, bool) and origin_work_id > 0:
        extra["origin_work_id"] = origin_work_id
    if isinstance(origin_work_identity, str) and origin_work_identity.strip():
        extra["origin_work_identity"] = origin_work_identity
    task = await background_task_crud.create_task(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        tool_call_id=f"terminal-auto-watch:{digest}",
        tool_name=TERMINAL_WATCH_TOOL_NAME,
        arguments={
            "terminal_session_id": terminal_session_id,
            "after_offset": cursor,
            "condition": "process_exit",
        },
        auto_reply=True,
        extra=extra,
        commit=False,
    )
    if previous_task is not None:
        previous_task.extra = {
            **(previous_task.extra if isinstance(previous_task.extra, dict) else {}),
            "next_auto_watch_task_id": task.id,
        }
        db.add(previous_task)
        await db.flush()
    if commit:
        await db.commit()
    else:
        await db.flush()
    return task


async def create_terminal_watch_subscription(
    db: AsyncSession,
    *,
    uid: str,
    session_id: str,
    profile_id: int,
    tool_call_id: str,
    arguments: dict[str, Any],
    extra: dict[str, Any],
    commit: bool = True,
) -> BackgroundTask | None:
    subscription_arguments = dict(arguments) if isinstance(arguments, dict) else {}
    terminal_session_id = subscription_arguments.get("terminal_session_id")
    after_offset = subscription_arguments.get("after_offset")
    if not isinstance(terminal_session_id, str) or not terminal_session_id.strip() or not _is_non_negative_int(after_offset):
        if commit:
            await db.commit()
        else:
            await db.flush()
        return None

    locked = await background_task_crud.lock_terminal_watch(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        terminal_session_id=terminal_session_id,
    )
    terminal_session = await background_task_crud.get_terminal_watch_session(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        terminal_session_id=terminal_session_id,
    )
    if not locked or terminal_session is None:
        if commit:
            await db.commit()
        else:
            await db.flush()
        return None

    existing = await background_task_crud.get_terminal_watch_by_call_id(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        tool_call_id=tool_call_id,
    )
    if existing is not None:
        if commit:
            await db.commit()
        else:
            await db.flush()
        return existing

    origin_work_id = extra.get("origin_work_id") if isinstance(extra, dict) else None
    origin_work_identity = extra.get("origin_work_identity") if isinstance(extra, dict) else None
    valid_origin = isinstance(origin_work_id, int) and not isinstance(origin_work_id, bool) and origin_work_id > 0 and isinstance(origin_work_identity, str) and bool(origin_work_identity.strip())
    origin_payload: dict[str, Any] = {}
    if valid_origin:
        origin_payload = {
            "origin_work_id": origin_work_id,
            "origin_work_identity": origin_work_identity,
        }

    cached_final_task: BackgroundTask | None = None
    if terminal_session.status not in TERMINAL_SESSION_FINAL_STATUSES:
        await create_terminal_auto_watch(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            terminal_session_id=terminal_session_id,
            after_offset=after_offset,
            origin_work_id=origin_payload.get("origin_work_id"),
            origin_work_identity=origin_payload.get("origin_work_identity"),
            commit=False,
        )
    else:
        active_watch = await background_task_crud.get_active_terminal_watch(
            db,
            uid,
            session_id,
            terminal_session_id,
            automatic_only=True,
        )
        if active_watch is not None:
            if valid_origin:
                active_extra = dict(active_watch.extra) if isinstance(active_watch.extra, dict) else {}
                active_extra.update(origin_payload)
                active_watch.extra = active_extra
                db.add(active_watch)
                await db.flush()
        else:
            cached_final_task = await background_task_crud.get_latest_terminal_watch_final_result(
                db,
                uid=uid,
                session_id=session_id,
                profile_id=profile_id,
                terminal_session_id=terminal_session_id,
            )
            if cached_final_task is None:
                digest = hashlib.sha256(f"{terminal_session_id}:{after_offset}:None".encode()).hexdigest()[:32]
                auto_extra: dict[str, Any] = {
                    "source": "terminal_auto_watch",
                    "auto_terminal_watch": True,
                    "terminal_session_id": terminal_session_id,
                }
                auto_extra.update(origin_payload)
                await background_task_crud.create_task(
                    db,
                    uid=uid,
                    session_id=session_id,
                    profile_id=profile_id,
                    tool_call_id=f"terminal-auto-watch:{digest}",
                    tool_name=TERMINAL_WATCH_TOOL_NAME,
                    arguments={
                        "terminal_session_id": terminal_session_id,
                        "after_offset": after_offset,
                        "condition": "process_exit",
                    },
                    auto_reply=True,
                    extra=auto_extra,
                    commit=False,
                )

    subscription_extra = {
        **(extra if isinstance(extra, dict) else {}),
        "terminal_watch_subscription": True,
        "terminal_session_id": terminal_session_id,
    }
    subscription = await background_task_crud.create_task(
        db,
        uid=uid,
        session_id=session_id,
        profile_id=profile_id,
        tool_call_id=tool_call_id,
        tool_name=TERMINAL_WATCH_TOOL_NAME,
        arguments=subscription_arguments,
        auto_reply=False,
        extra=subscription_extra,
        commit=False,
    )

    if cached_final_task is not None and subscription.id is not None:
        cached_result_data = cached_final_task.result if isinstance(cached_final_task.result, dict) else {}
        cached_content = cached_result_data.get("content") if isinstance(cached_result_data, dict) else None
        if isinstance(cached_content, dict):
            locked_cached_task = await terminal_watch_reply_crud.lock_result(db, cached_final_task)
            if locked_cached_task is not None:
                await db.refresh(locked_cached_task)
                cached_extra = dict(locked_cached_task.extra) if isinstance(locked_cached_task.extra, dict) else {}
                cached_ids = cached_extra.get("cached_completed_watch_task_ids")
                cached_ids = list(cached_ids) if isinstance(cached_ids, list) else []
                if subscription.id not in cached_ids:
                    cached_ids.append(subscription.id)
                cached_extra["cached_completed_watch_task_ids"] = cached_ids

                if valid_origin:
                    deliveries = get_terminal_watch_deliveries(locked_cached_task)
                    deliveries[str(origin_work_id)] = {
                        "identity": origin_work_identity,
                        "consumed": False,
                        "routed": False,
                    }
                    cached_extra.update(origin_payload)
                    cached_extra["terminal_watch_deliveries"] = deliveries
                    cached_extra["terminal_auto_watch_routed"] = False
                    locked_cached_task.auto_reply = True
                    locked_cached_task.reply_status = BackgroundTaskReplyStatus.PENDING
                    locked_cached_task.reply_locked_by = None
                    locked_cached_task.reply_lock_until = None

                locked_cached_task.extra = cached_extra
                db.add(locked_cached_task)
                await db.flush()

            cached_event = cached_content.get("event")
            child_condition = subscription_arguments.get("condition")
            child_content = {
                key: value
                for key, value in cached_content.items()
                if key
                not in {
                    "matched_watch_task_ids",
                    "watch_results",
                    "watch_states",
                    "completed_watch_task_ids",
                    "matched_text",
                    "match_context",
                }
            }
            child_content["condition"] = child_condition
            child_content["after_offset"] = after_offset
            child_matched = child_condition == "process_exit" and cached_event == "process_exit"
            child_content["matched"] = child_matched
            latest_offset = max(after_offset, terminal_session.next_output_offset)
            child_content["output"] = ""
            child_content["start_offset"] = after_offset
            child_content["next_offset"] = after_offset
            child_content["latest_offset"] = latest_offset
            child_content["output_truncated"] = latest_offset > after_offset
            child_content["eof"] = True

            child_result = build_background_task_success_result(subscription.tool_name, child_content)
            finished_subscription = await background_task_crud.finish_terminal_watch_subscription(
                db,
                task_id=subscription.id,
                result=child_result,
            )
            if finished_subscription is not None:
                subscription = finished_subscription

    if commit:
        await db.commit()
    else:
        await db.flush()
    return subscription


def _terminal_watch_origin(value: BackgroundTask) -> tuple[int, str] | None:
    extra = value.extra if isinstance(value.extra, dict) else {}
    work_id = extra.get("origin_work_id")
    identity = extra.get("origin_work_identity")
    if not isinstance(work_id, int) or isinstance(work_id, bool) or work_id <= 0 or not isinstance(identity, str) or not identity.strip():
        return None
    return work_id, identity


def _build_terminal_watch_deliveries(
    task: BackgroundTask,
    subscriptions: list[BackgroundTask],
    waiting_work: list[SessionReplyWorkItem],
    *,
    terminal_session_id: str,
) -> dict[str, dict[str, Any]]:
    subscription_ids = {subscription.id for subscription in subscriptions if isinstance(subscription.id, int) and not isinstance(subscription.id, bool) and subscription.id > 0}
    deliveries: dict[str, dict[str, Any]] = {}
    for source in (task, *subscriptions):
        origin = _terminal_watch_origin(source)
        if origin is not None:
            deliveries.setdefault(
                str(origin[0]),
                {
                    "identity": origin[1],
                    "consumed": False,
                    "routed": False,
                },
            )

    for work in waiting_work:
        if work.id is None or not isinstance(work.dedupe_key, str) or not work.dedupe_key.strip():
            continue
        state = work.execution_state if isinstance(work.execution_state, dict) else {}
        slots: list[dict[str, Any]] = []
        for state_key in ("terminal_watch_waits", "pending_tools"):
            raw_slots = state.get(state_key)
            if isinstance(raw_slots, list):
                slots.extend(slot for slot in raw_slots if isinstance(slot, dict))
        if not any(slot.get("task_id") in subscription_ids or (slot.get("terminal_auto_watch") is True and slot.get("terminal_session_id") == terminal_session_id) for slot in slots):
            continue
        deliveries.setdefault(
            str(work.id),
            {
                "identity": work.dedupe_key,
                "consumed": False,
                "routed": False,
            },
        )
    return deliveries


async def _complete_terminal_watch_subscriptions(
    db: AsyncSession,
    *,
    subscriptions: list[BackgroundTask],
    content: dict[str, Any],
) -> list[BackgroundTask]:
    raw_matched_ids = content.get("matched_watch_task_ids")
    matched_ids = {matched_id for matched_id in raw_matched_ids if isinstance(matched_id, int) and not isinstance(matched_id, bool) and matched_id > 0} if isinstance(raw_matched_ids, (list, tuple, set)) else set()
    event = content.get("event")
    terminal_event = event in {"process_exit", "terminal_error", "observation_error"}
    if event == "timeout" or (not terminal_event and not matched_ids):
        return []

    watch_results = content.get("watch_results")
    base_content = {key: value for key, value in content.items() if key not in {"matched_watch_task_ids", "watch_results"}}
    completed: list[BackgroundTask] = []
    for subscription in subscriptions:
        subscription_id = subscription.id
        if not isinstance(subscription_id, int) or isinstance(subscription_id, bool) or subscription_id <= 0:
            continue
        if not terminal_event and subscription_id not in matched_ids:
            continue

        child_content: Any = None
        if isinstance(watch_results, dict):
            child_content = watch_results.get(str(subscription_id))
            if not isinstance(child_content, dict):
                child_content = watch_results.get(subscription_id)
        if not isinstance(child_content, dict):
            subscription_arguments = subscription.arguments if isinstance(subscription.arguments, dict) else {}
            child_content = dict(base_content)
            child_content["condition"] = subscription_arguments.get("condition")
            child_content["matched"] = subscription_id in matched_ids

        finished = await background_task_crud.finish_terminal_watch_subscription(
            db,
            task_id=subscription_id,
            result=build_background_task_success_result(subscription.tool_name, child_content),
        )
        if finished is not None:
            completed.append(finished)
    return completed


async def complete_terminal_watch_round(
    db: AsyncSession,
    *,
    task: BackgroundTask,
    worker_id: str,
    content: dict[str, Any],
) -> bool:
    arguments = task.arguments if isinstance(task.arguments, dict) else {}
    terminal_session_id = arguments.get("terminal_session_id")
    if not await background_task_crud.lock_terminal_watch(
        db,
        uid=task.uid,
        session_id=task.session_id,
        profile_id=task.profile_id,
        terminal_session_id=terminal_session_id,
    ):
        await db.rollback()
        return False

    await db.refresh(task)
    result = build_background_task_success_result(task.tool_name, content)
    marked = await background_task_crud.mark_succeeded(
        db,
        task_id=task.id,
        worker_id=worker_id,
        result=result,
        auto_reply=task.auto_reply,
        commit=False,
    )
    if not marked:
        await db.rollback()
        return False

    task.result = result
    subscriptions = await background_task_crud.list_terminal_watch_subscriptions(
        db,
        uid=task.uid,
        session_id=task.session_id,
        profile_id=task.profile_id,
        terminal_session_id=terminal_session_id,
    )
    completed = await _complete_terminal_watch_subscriptions(
        db,
        subscriptions=subscriptions,
        content=content,
    )
    completed_ids = [subscription.id for subscription in completed if subscription.id is not None]
    if completed_ids:
        completed_content = {**content, "completed_watch_task_ids": completed_ids}
        task.result = {**result, "content": completed_content}

    waiting_work = await terminal_watch_reply_crud.list_terminal_watch_waiting_work(
        db,
        uid=task.uid,
        session_id=task.session_id,
        profile_id=task.profile_id,
    )
    deliveries = _build_terminal_watch_deliveries(
        task,
        subscriptions,
        waiting_work,
        terminal_session_id=terminal_session_id,
    )
    if deliveries:
        task_extra = dict(task.extra) if isinstance(task.extra, dict) else {}
        task_extra["terminal_watch_deliveries"] = deliveries
        task.extra = task_extra

    if completed_ids or deliveries:
        db.add(task)
        await db.flush()

    next_task = None
    next_offset = content.get("next_offset")
    event = content.get("event")
    if _is_non_negative_int(next_offset) and isinstance(event, str) and event in _CONTINUABLE_EVENTS and content.get("eof") is not True:
        next_task = await create_terminal_auto_watch(
            db,
            uid=task.uid,
            session_id=task.session_id,
            profile_id=task.profile_id,
            terminal_session_id=terminal_session_id,
            after_offset=next_offset,
            previous_task_id=task.id,
            commit=False,
        )

    replaced_timeouts = await terminal_watch_reply_crud.supersede_pending_timeouts(db, task)
    await db.commit()
    get_logger(__name__).bind(
        uid=task.uid,
        session_id=task.session_id,
        terminal_session_id=terminal_session_id,
        task_id=task.id,
        event=event,
        next_task_id=getattr(next_task, "id", None),
        replaced_timeouts=replaced_timeouts,
    ).info("Terminal watch observation completed")
    return True
