from dataclasses import dataclass
from functools import partial
from typing import Any

from app.core.constants import (
    ERR_LLM_UNEXPECTED_ERROR,
    ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT,
    SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY,
)
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.session import session_crud
from app.core.dispatcher import ChatDispatcher
from app.core.i18n import t
from app.core.message_platforms.notifier import send_session_stream_event
from app.core.session_reply_queue.executor_common import (
    _metadata_with_work_order,
    _result_message_dedupe_key,
)
from app.core.session_reply_queue.executor_metadata import _persist_interactive_work_request_metadata
from app.core.session_reply_queue.manager import get_work_request_ids, session_reply_queue_manager
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.models.message import InternalMessage
from app.models.session_reply_work_item import SessionReplyWorkItem
from app.providers.database import AsyncSessionLocal

__all__ = []


@dataclass
class _InteractiveWorkStreamEventState:
    work: SessionReplyWorkItem
    next_sequence: int
    dequeued_request_ids: set[str]
    turn_end_content_by_response_id: dict[str, str]
    tool_names_by_response_id: dict[str, list[str]]


async def _persist_interactive_work_stream_event(
    stream_state: _InteractiveWorkStreamEventState,
    event: dict[str, Any],
) -> dict[str, Any]:
    work = stream_state.work
    persisted_event = {
        **event,
        "session_id": work.session_id,
        "work_id": work.id,
        "event_sequence_no": stream_state.next_sequence,
    }
    async with AsyncSessionLocal() as event_db:
        if persisted_event["type"] == "llm_request_metadata":
            persisted_event = _metadata_with_work_order(
                work,
                persisted_event,
                event_sequence_no=stream_state.next_sequence,
            )
            await session_crud.update_llm_request_metadata(
                event_db,
                session_id=work.session_id,
                uid=work.uid,
                metadata=persisted_event,
                commit=False,
            )
        await session_reply_stream_event_crud.publish(
            event_db,
            work_id=work.id,
            sequence_no=stream_state.next_sequence,
            event=persisted_event,
            commit=False,
        )
        await event_db.commit()
    stream_state.next_sequence += 1
    return persisted_event


async def _publish_newly_dequeued_request_ids(
    db,
    stream_state: _InteractiveWorkStreamEventState,
) -> None:
    work = stream_state.work
    await db.refresh(work)
    request_ids = [request_id for request_id in get_work_request_ids(work) if request_id not in stream_state.dequeued_request_ids]
    if not request_ids:
        return
    await _persist_interactive_work_stream_event(
        stream_state,
        {
            "type": "input_dequeued",
            "session_id": work.session_id,
            "work_id": work.id,
            "request_ids": request_ids,
        },
    )
    stream_state.dequeued_request_ids.update(request_ids)


async def _publish_interactive_work_stream_event(
    db,
    stream_state: _InteractiveWorkStreamEventState,
    event: dict[str, Any],
) -> None:
    work = stream_state.work
    if event.get("type") == "agent_loop_start":
        await _publish_newly_dequeued_request_ids(db, stream_state)
    persisted_event = await _persist_interactive_work_stream_event(stream_state, event)

    response_id = event.get("response_id")
    if event.get("type") == "turn_end":
        content = event.get("content")
        if isinstance(response_id, str) and response_id.strip() and isinstance(content, str) and content.strip():
            stream_state.turn_end_content_by_response_id[response_id] = content.strip()
        return

    if event.get("type") != "tool_start" or not isinstance(response_id, str) or not response_id.strip():
        return

    name = event.get("name")
    tool_names = stream_state.tool_names_by_response_id.setdefault(response_id, [])
    tool_names.append(name if isinstance(name, str) else "")
    tool_call_count = event.get("tool_call_count")
    if not isinstance(tool_call_count, int) or isinstance(tool_call_count, bool) or tool_call_count <= 0:
        tool_call_count = 1
    if len(tool_names) < tool_call_count:
        return

    stream_event = {
        **persisted_event,
        "tool_names": stream_state.tool_names_by_response_id.pop(response_id),
    }
    content = stream_state.turn_end_content_by_response_id.pop(response_id, None)
    if content is not None:
        stream_event["content"] = content
    await send_session_stream_event(work.uid, work.session_id, stream_event)


async def _fetch_additional_foreground_user_messages(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    stream_state: _InteractiveWorkStreamEventState,
) -> UserInputBatch | None:
    batch = await session_reply_queue_manager.absorb_contiguous_foreground_messages(
        db,
        work_id=work.id,
        worker_id=worker_id,
    )
    if batch is not None:
        await _publish_newly_dequeued_request_ids(db, stream_state)
    return batch


async def _check_interactive_work_validity(
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> bool:
    async with AsyncSessionLocal() as validity_db:
        active_claims = await session_reply_work_item_crud.get_active_claims(
            validity_db,
            {work.id: worker_id},
        )
        session = await session_crud.get_by_session_id(
            validity_db,
            work.session_id,
        )
    return (work.id, worker_id) in active_claims and session is not None and session.uid == work.uid and session.profile_id == work.profile_id


async def _save_interactive_work_execution_checkpoint(
    db,
    checkpoint: dict[str, Any],
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> None:
    active_audit_execution_present = SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint
    active_audit_execution = checkpoint.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
    state = {
        **(work.execution_state or {}),
        "dispatcher_checkpoint": checkpoint,
    }
    if active_audit_execution_present:
        if active_audit_execution is None:
            state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
        else:
            state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = active_audit_execution
    updated = await session_reply_work_item_crud.update_claimed(
        db,
        work_id=work.id,
        worker_id=worker_id,
        values={"execution_state": state},
    )
    if not updated:
        raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT))
    work.execution_state = state


async def _dispatch_interactive_work(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    message: str,
    initial_message: InternalMessage,
    history_before_id: int,
    frozen_user_message_ids: list[int],
    attachments: list[str] | None,
    allow_additional_user_messages: bool,
    execution_resume_state: dict[str, Any] | None,
) -> dict[str, Any]:
    execution_state = work.execution_state or {}
    stream_requested = bool((work.execution_state or {}).get("stream_requested"))
    context_summary_events_requested = bool((work.execution_state or {}).get("context_summary_events_requested"))
    async with AsyncSessionLocal() as event_db:
        next_stream_sequence = await session_reply_stream_event_crud.get_latest_sequence(event_db, work_id=work.id) + 1
    stream_state = _InteractiveWorkStreamEventState(
        work=work,
        next_sequence=next_stream_sequence,
        dequeued_request_ids=set(),
        turn_end_content_by_response_id={},
        tool_names_by_response_id={},
    )

    expose_tool_call_content = bool(execution_state.get("expose_tool_call_content", True))
    show_tool_calls = bool(execution_state.get("show_tool_calls", True))

    dispatch_kwargs = {
        "db": db,
        "message": message,
        "uid": work.uid,
        "session_id": work.session_id,
        "attachments": attachments or None,
        "session_source": str(execution_state.get("message_source") or "queue"),
        "persisted_initial_message": initial_message,
        "history_before_id": history_before_id,
        "frozen_user_message_ids": frozen_user_message_ids,
        "final_message_dedupe_key": _result_message_dedupe_key(work),
        "persisted_profile_id": work.profile_id,
        "additional_user_messages_fetcher": partial(
            _fetch_additional_foreground_user_messages,
            db,
            work=work,
            worker_id=worker_id,
            stream_state=stream_state,
        )
        if allow_additional_user_messages
        else None,
        "execution_resume_state": execution_resume_state,
        "execution_checkpoint_callback": partial(
            _save_interactive_work_execution_checkpoint,
            db,
            work=work,
            worker_id=worker_id,
        ),
        "context_summary_work_validity_checker": partial(
            _check_interactive_work_validity,
            work=work,
            worker_id=worker_id,
        ),
        "expose_tool_call_content": expose_tool_call_content,
        "show_tool_calls": show_tool_calls,
    }
    additional_system_prompt = execution_state.get("additional_system_prompt")
    if isinstance(additional_system_prompt, str) and additional_system_prompt.strip():
        dispatch_kwargs["additional_system_prompt"] = additional_system_prompt.strip()
    if not stream_requested:
        response = await ChatDispatcher.dispatch(
            **dispatch_kwargs,
            request_metadata_callback=partial(_persist_interactive_work_request_metadata, work=work),
            context_summary_lifecycle_callback=partial(
                _publish_interactive_work_stream_event,
                db,
                stream_state,
            )
            if context_summary_events_requested
            else None,
        )
        if isinstance(response.get("llm_request_metadata"), dict):
            response["llm_request_metadata"] = _metadata_with_work_order(work, response["llm_request_metadata"])
            await session_crud.update_llm_request_metadata(
                db,
                session_id=work.session_id,
                uid=work.uid,
                metadata=response["llm_request_metadata"],
                commit=False,
            )
        return response

    response = None
    async for event in ChatDispatcher.dispatch_stream(
        **dispatch_kwargs,
        context_summary_events_requested=context_summary_events_requested,
        raise_errors=True,
    ):
        event_type = event.get("type")
        if event_type == "done":
            response = event.get("response")
            response_id = event.get("response_id")
            if isinstance(response, dict) and isinstance(response_id, str) and response_id:
                response = {**response, "response_id": response_id}
        elif event_type == "error":
            error_message = str(event.get("message") or t(ERR_LLM_UNEXPECTED_ERROR))
            raise RuntimeError(error_message)
        else:
            await _publish_interactive_work_stream_event(db, stream_state, event)
    if not isinstance(response, dict):
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
    return response
