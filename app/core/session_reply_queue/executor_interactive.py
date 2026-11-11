import asyncio
import json
from dataclasses import dataclass, field
from functools import partial
from types import SimpleNamespace
from typing import Any

from app.core.constants import (
    AGENT_WORK_TOOL_NAME,
    ERR_LLM_UNEXPECTED_ERROR,
    ERR_SESSION_REPLY_LEASE_LOST,
    ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT,
    SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY,
)
from app.core.crud.profile.profile import profile_crud
from app.core.crud.session.external_work import external_work_crud
from app.core.crud.session.reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session.reply_work_item import (
    ensure_session_reply_work_claim,
    session_reply_work_item_crud,
)
from app.core.crud.session.session import session_crud
from app.core.crud.session.subagent import subagent_crud
from app.core.dispatchers.dispatcher import ChatDispatcher
from app.core.i18n import t
from app.core.message_platforms.notifier import send_session_stream_event
from app.core.prompts import SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT, SUBAGENT_EXECUTION_GUIDANCE_PROMPT
from app.core.session_reply_queue.child_delivery import acknowledge_observed_child_results
from app.core.session_reply_queue.executor_common import (
    _metadata_with_work_order,
    _result_message_dedupe_key,
)
from app.core.session_reply_queue.executor_metadata import _persist_interactive_work_request_metadata
from app.core.session_reply_queue.manager import get_work_request_ids, session_reply_queue_manager
from app.core.session_reply_queue.manager_common import is_scheduled_task_work, resolve_work_tool_workflow
from app.core.utils.dispatcher.session_todo_snapshot import strip_session_todo_snapshot
from app.core.utils.dispatcher.terminal_watch_snapshot import strip_terminal_watch_snapshot
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.core.utils.llm_request_params import build_session_cache_key
from app.core.utils.message_parser import parse_db_messages_to_internal
from app.core.utils.request_token_baseline import build_session_cache_metrics
from app.models.message import InternalMessage, MessageRole, MessageType
from app.models.provider_usage import ProviderUsageType
from app.models.session_reply_work_item import SessionReplyWorkItem, SessionReplyWorkType
from app.providers.database import AsyncSessionLocal

__all__ = []


@dataclass
class _InteractiveWorkStreamEventState:
    work: SessionReplyWorkItem
    worker_id: str
    next_sequence: int
    dequeued_request_ids: set[str]
    turn_end_content_by_response_id: dict[str, str]
    tool_names_by_response_id: dict[str, list[str]]
    work_id: int | None = None
    uid: str | None = None
    session_id: str | None = None
    work_sequence_no: int | None = None
    update_session_context: bool = True
    session_token_totals: dict[str, Any] = field(default_factory=dict)
    is_scheduled_task_work: bool = False

    def __post_init__(self) -> None:
        if self.work_id is None or self.uid is None or self.session_id is None or self.work_sequence_no is None:
            work_id = self.work.id
            uid = self.work.uid
            session_id = self.work.session_id
            work_sequence_no = self.work.sequence_no
            if self.work_id is None:
                self.work_id = work_id
            if self.uid is None:
                self.uid = uid
            if self.session_id is None:
                self.session_id = session_id
            if self.work_sequence_no is None:
                self.work_sequence_no = work_sequence_no


async def _persist_interactive_work_stream_event(
    stream_state: _InteractiveWorkStreamEventState,
    event: dict[str, Any],
) -> dict[str, Any]:
    work_id = stream_state.work_id
    persisted_event = {
        **event,
        "session_id": stream_state.session_id,
        "work_id": work_id,
        "work_sequence_no": stream_state.work_sequence_no,
        "event_sequence_no": stream_state.next_sequence,
    }
    async with AsyncSessionLocal() as event_db:
        updated = await session_reply_work_item_crud.update_claimed(
            event_db,
            work_id=work_id,
            worker_id=stream_state.worker_id,
            values={},
            commit=False,
        )
        if not updated:
            await event_db.rollback()
            raise asyncio.CancelledError(t(ERR_SESSION_REPLY_LEASE_LOST))
        if persisted_event["type"] == "llm_request_metadata":
            persisted_event = _metadata_with_work_order(
                SimpleNamespace(
                    id=work_id,
                    sequence_no=stream_state.work_sequence_no,
                ),
                {
                    **persisted_event,
                    **stream_state.session_token_totals,
                },
                event_sequence_no=stream_state.next_sequence,
                update_session_context=stream_state.update_session_context,
            )
            if not stream_state.update_session_context:
                persisted_event["type"] = "llm_request_metadata"
                persisted_event["session_id"] = stream_state.session_id
            if stream_state.update_session_context:
                await session_crud.update_llm_request_metadata(
                    event_db,
                    session_id=stream_state.session_id,
                    uid=stream_state.uid,
                    metadata=persisted_event,
                    commit=False,
                )
        await session_reply_stream_event_crud.publish(
            event_db,
            work_id=work_id,
            sequence_no=stream_state.next_sequence,
            event=persisted_event,
            commit=False,
        )
        await event_db.commit()
    stream_state.next_sequence += 1
    return persisted_event


async def _publish_newly_dequeued_request_ids(
    stream_state: _InteractiveWorkStreamEventState,
) -> None:
    work_id = stream_state.work_id
    async with AsyncSessionLocal() as request_db:
        work_snapshot = await session_reply_work_item_crud.get(request_db, work_id)
        if work_snapshot is None:
            raise asyncio.CancelledError(t(ERR_SESSION_REPLY_LEASE_LOST))
        request_ids = [request_id for request_id in get_work_request_ids(work_snapshot) if request_id not in stream_state.dequeued_request_ids]
    if not request_ids:
        return
    await _persist_interactive_work_stream_event(
        stream_state,
        {
            "type": "input_dequeued",
            "session_id": stream_state.session_id,
            "work_id": work_id,
            "request_ids": request_ids,
        },
    )
    stream_state.dequeued_request_ids.update(request_ids)


async def _publish_interactive_work_stream_event(
    stream_state: _InteractiveWorkStreamEventState,
    event: dict[str, Any],
) -> None:
    if event.get("type") == "llm_request_metadata" and stream_state.is_scheduled_task_work:
        return
    if event.get("type") == "agent_loop_start":
        await _publish_newly_dequeued_request_ids(stream_state)
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
    await send_session_stream_event(stream_state.uid, stream_state.session_id, stream_event)


async def _fetch_additional_foreground_user_messages(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    stream_state: _InteractiveWorkStreamEventState,
) -> UserInputBatch | None:
    batch = await session_reply_queue_manager.absorb_contiguous_foreground_messages(
        db,
        work_id=stream_state.work_id,
        worker_id=worker_id,
    )
    if batch is not None:
        await _publish_newly_dequeued_request_ids(stream_state)
    return batch


async def _fetch_additional_subagent_messages(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> UserInputBatch | None:
    await ensure_session_reply_work_claim(db)
    pending_messages = await subagent_crud.list_pending_messages(db, work=work)
    parsed_messages = tuple(message for message in parse_db_messages_to_internal(pending_messages) if isinstance(message.id, int) and not isinstance(message.id, bool) and message.id > 0)
    if not parsed_messages:
        return None
    return UserInputBatch(
        messages=parsed_messages,
        source_message_ids=tuple(message.id for message in parsed_messages),
    )


async def _check_interactive_work_validity(
    *,
    work_id: int,
    session_id: str,
    uid: str,
    profile_id: int,
    worker_id: str,
    independent_context: bool,
) -> bool:
    async with AsyncSessionLocal() as validity_db:
        active_claims = await session_reply_work_item_crud.get_active_claims(
            validity_db,
            {work_id: worker_id},
        )
        session = await session_crud.get_by_session_id(
            validity_db,
            session_id,
        )
        profile = None
        if independent_context:
            profile = await profile_crud.get_with_relations(validity_db, profile_id)
    if (work_id, worker_id) not in active_claims or session is None or session.uid != uid:
        return False
    if independent_context:
        return profile is not None and profile.uid == uid
    return session.profile_id == profile_id


async def _save_interactive_work_execution_checkpoint(
    db,
    checkpoint: dict[str, Any],
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
) -> None:
    try:
        active_audit_execution_present = SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY in checkpoint
        active_audit_execution = checkpoint.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
        work_id = work.id
        if not isinstance(work_id, int) or isinstance(work_id, bool) or work_id <= 0:
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT))

        claimed = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={},
            commit=False,
        )
        if not claimed:
            raise asyncio.CancelledError(t(ERR_SESSION_REPLY_LEASE_LOST))

        await db.refresh(work)
        state = dict(work.execution_state or {})
        if active_audit_execution_present:
            if active_audit_execution is None:
                state.pop(SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY, None)
            else:
                state[SESSION_REPLY_ACTIVE_AUDIT_EXECUTION_KEY] = active_audit_execution
        is_subagent_work = work.subagent_id is not None
        if is_subagent_work:
            checkpoint_message_ids: list[int] = []
            seen_message_ids: set[int] = set()
            for message in checkpoint.get("messages", []):
                if not isinstance(message, dict) or message.get("role") != MessageRole.USER:
                    continue
                message_id = message.get("id")
                if not isinstance(message_id, int) or isinstance(message_id, bool) or message_id <= 0 or message_id in seen_message_ids:
                    continue
                seen_message_ids.add(message_id)
                checkpoint_message_ids.append(message_id)
            if checkpoint_message_ids:
                await subagent_crud.acknowledge_messages(
                    db,
                    work=work,
                    message_ids=checkpoint_message_ids,
                )
        else:
            tool_calls_by_id: dict[str, dict[str, Any]] = {}
            for message in checkpoint.get("messages", []):
                if not isinstance(message, dict) or message.get("role") != MessageRole.ASSISTANT:
                    continue
                tool_calls = message.get("tool_calls")
                if not isinstance(tool_calls, list):
                    continue
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict) or tool_call.get("name") != AGENT_WORK_TOOL_NAME:
                        continue
                    tool_call_id = tool_call.get("id")
                    if isinstance(tool_call_id, str) and tool_call_id.strip():
                        tool_calls_by_id[tool_call_id] = tool_call

            child_tool_slots: list[dict[str, Any]] = []
            registered_child_tool_keys: set[tuple[Any, Any, Any, Any]] = set()
            registered_response_ids: dict[tuple[Any, Any, Any], Any] = {}
            for slot_list_name in ("child_work_waits", "child_work_receipts"):
                previous_child_work_slots = state.get(slot_list_name)
                if isinstance(previous_child_work_slots, list):
                    for slot in previous_child_work_slots:
                        if not isinstance(slot, dict):
                            continue
                        child_tool_key = (slot.get("child_work_id"), slot.get("subagent_id"), slot.get("tool_call_id"))
                        registered_child_tool_keys.add((*child_tool_key, slot.get("tool_result_message_id")))
                        if slot.get("response_id") is not None:
                            registered_response_ids[child_tool_key] = slot.get("response_id")
            for message in checkpoint.get("messages", []):
                if not isinstance(message, dict) or message.get("role") != MessageRole.TOOL:
                    continue
                tool_call_id = message.get("tool_call_id")
                if not isinstance(tool_call_id, str) or not tool_call_id.strip():
                    continue
                tool_call = tool_calls_by_id.get(tool_call_id)
                if tool_call is None:
                    continue
                arguments = tool_call.get("arguments")
                operation = arguments.get("operation") if isinstance(arguments, dict) else None
                if not isinstance(operation, str) or operation not in {"create", "send_message"}:
                    continue
                tool_result_message_id = message.get("id")
                if not isinstance(tool_result_message_id, int) or isinstance(tool_result_message_id, bool) or tool_result_message_id <= 0:
                    continue
                content = strip_session_todo_snapshot(strip_terminal_watch_snapshot(message.get("content")))
                try:
                    payload = json.loads(content or "")
                except (TypeError, ValueError):
                    continue
                if not isinstance(payload, dict) or payload.get("status") != "queued" or payload.get("operation") != operation:
                    continue
                child_work_id = payload.get("child_work_id")
                subagent_id = payload.get("subagent_id")
                if not isinstance(child_work_id, int) or isinstance(child_work_id, bool) or child_work_id <= 0 or not isinstance(subagent_id, str) or not subagent_id.strip():
                    continue

                call = await subagent_crud.get_call(
                    db,
                    parent_work_id=work.id,
                    tool_call_id=tool_call_id,
                )
                if call is None or call.delivered is True:
                    continue
                child_tool_key = (child_work_id, subagent_id, tool_call_id)
                child_tool_slot_key = (*child_tool_key, tool_result_message_id)
                if child_tool_slot_key in registered_child_tool_keys:
                    continue
                child_tool_slots.append(
                    {
                        "child_work_id": child_work_id,
                        "subagent_id": subagent_id,
                        "tool_call_id": tool_call_id,
                        "tool_name": AGENT_WORK_TOOL_NAME,
                        "tool_result_message_id": tool_result_message_id,
                        "response_id": registered_response_ids.get(child_tool_key),
                    }
                )
                registered_child_tool_keys.add(child_tool_slot_key)
            if child_tool_slots:
                await external_work_crud.register_child_tools(
                    db,
                    work=work,
                    execution_state=state,
                    slots=child_tool_slots,
                )
            await acknowledge_observed_child_results(
                db,
                parent=work,
                checkpoint=checkpoint,
                execution_state=state,
            )

        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work_id,
            worker_id=worker_id,
            values={"execution_state": state},
            checkpoint=checkpoint,
            commit=False,
        )
        if not updated:
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_SAVING_CHECKPOINT))
        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    work.id = work_id
    work.execution_state = state


async def _dispatch_interactive_work(
    db,
    *,
    work: SessionReplyWorkItem,
    worker_id: str,
    message: str,
    initial_message: InternalMessage,
    history_before_id: int | None,
    frozen_user_message_ids: list[int],
    attachments: list[str] | None,
    allow_additional_user_messages: bool,
    execution_resume_state: dict[str, Any] | None,
    execution_scope: str | None = None,
    frozen_context_messages: list[InternalMessage] | None = None,
) -> dict[str, Any]:
    work_id = work.id
    uid = work.uid
    session_id = work.session_id
    work_sequence_no = work.sequence_no
    if not isinstance(work_id, int) or isinstance(work_id, bool) or work_id <= 0 or not isinstance(uid, str) or not uid or not isinstance(session_id, str) or not session_id or not isinstance(work_sequence_no, int) or isinstance(work_sequence_no, bool) or work_sequence_no <= 0:
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
    await ensure_session_reply_work_claim(db, commit=True)
    execution_state = work.execution_state or {}
    is_subagent_board_review = execution_state.get("subagent_board_review_pending") is True
    scheduled_task_work = await is_scheduled_task_work(db, work)
    if scheduled_task_work:
        request_purpose = ProviderUsageType.SCHEDULED_TASK
    else:
        request_purpose = ProviderUsageType.MAIN_DIALOGUE
    is_independent_work = (await session_reply_work_item_crud.get_message_work_id(db, work)) is not None
    update_session_context = not scheduled_task_work and not is_independent_work and frozen_context_messages is None
    stream_requested = False if is_independent_work else bool((work.execution_state or {}).get("stream_requested"))
    context_summary_events_requested = False if is_independent_work else bool((work.execution_state or {}).get("context_summary_events_requested"))
    async with AsyncSessionLocal() as event_db:
        next_stream_sequence = await session_reply_stream_event_crud.get_latest_sequence(event_db, work_id=work_id) + 1
    stream_state = _InteractiveWorkStreamEventState(
        work=work,
        work_id=work_id,
        uid=uid,
        session_id=session_id,
        work_sequence_no=work_sequence_no,
        worker_id=worker_id,
        next_sequence=next_stream_sequence,
        dequeued_request_ids=set(),
        turn_end_content_by_response_id={},
        tool_names_by_response_id={},
        update_session_context=update_session_context,
        is_scheduled_task_work=scheduled_task_work,
    )
    metadata_work = SimpleNamespace(
        id=work_id,
        uid=uid,
        session_id=session_id,
        sequence_no=work_sequence_no,
    )

    async def persist_request_metadata(metadata: dict[str, Any]) -> None:
        result = await _persist_interactive_work_request_metadata(
            metadata,
            work=work,
            request_purpose=request_purpose,
            update_session_context=update_session_context,
        )
        if result is None:
            return
        stream_state.session_token_totals.update(
            build_session_cache_metrics(
                result.total_input_tokens,
                result.total_cached_tokens,
            )
        )
        stream_state.session_token_totals["total_output_tokens"] = result.total_output_tokens

    expose_tool_call_content = False if is_independent_work else bool(execution_state.get("expose_tool_call_content", True))
    show_tool_calls = False if is_independent_work else bool(execution_state.get("show_tool_calls", True))

    if work.subagent_id is not None:
        additional_user_messages_fetcher = partial(
            _fetch_additional_subagent_messages,
            db,
            work=work,
            worker_id=worker_id,
        )
    elif allow_additional_user_messages and not is_subagent_board_review:
        additional_user_messages_fetcher = partial(
            _fetch_additional_foreground_user_messages,
            db,
            work=work,
            worker_id=worker_id,
            stream_state=stream_state,
        )
    else:
        additional_user_messages_fetcher = None

    dispatch_kwargs = {
        "db": db,
        "message": message,
        "uid": uid,
        "cache_key": build_session_cache_key(uid=uid, session_id=session_id, subagent_id=work.subagent_id),
        "actor_uid": execution_state.get("actor_uid") or uid,
        "actor_username": execution_state.get("actor_username"),
        "session_id": session_id,
        "attachments": attachments or None,
        "session_source": str(execution_state.get("message_source") or "queue"),
        "persisted_initial_message": initial_message,
        "initial_message_type": {
            SessionReplyWorkType.BACKGROUND_TASK_RESUME: MessageType.BACKGROUND_TASK_RESULT,
            SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION: MessageType.AUDIT_DECISION,
        }.get(work.work_type, MessageType.TEXT),
        "execution_scope": execution_scope,
        "frozen_context_messages": frozen_context_messages,
        "history_before_id": history_before_id,
        "frozen_user_message_ids": frozen_user_message_ids,
        "final_message_dedupe_key": _result_message_dedupe_key(work),
        "persisted_profile_id": work.profile_id,
        "additional_user_messages_fetcher": additional_user_messages_fetcher,
        "execution_resume_state": execution_resume_state,
        "execution_checkpoint_callback": partial(
            _save_interactive_work_execution_checkpoint,
            db,
            work=work,
            worker_id=worker_id,
        ),
        "request_metadata_callback": persist_request_metadata,
        "context_summary_work_validity_checker": partial(
            _check_interactive_work_validity,
            work_id=work_id,
            session_id=session_id,
            uid=uid,
            profile_id=work.profile_id,
            worker_id=worker_id,
            independent_context=is_independent_work,
        ),
        "expose_tool_call_content": expose_tool_call_content,
        "show_tool_calls": show_tool_calls,
        "tool_workflow": await resolve_work_tool_workflow(db, work),
    }
    additional_system_prompt = execution_state.get("additional_system_prompt")
    if work.subagent_id is not None:
        if isinstance(additional_system_prompt, str) and additional_system_prompt.strip():
            additional_system_prompt = f"{additional_system_prompt.strip()}\n\n{SUBAGENT_EXECUTION_GUIDANCE_PROMPT}"
        else:
            additional_system_prompt = SUBAGENT_EXECUTION_GUIDANCE_PROMPT
        dispatch_kwargs["additional_system_prompt"] = additional_system_prompt
    else:
        if is_subagent_board_review:
            if isinstance(additional_system_prompt, str) and additional_system_prompt.strip():
                additional_system_prompt = f"{additional_system_prompt.strip()}\n\n{SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT}"
            else:
                additional_system_prompt = SUBAGENT_BOARD_REVIEW_INSTRUCTION_PROMPT
        if isinstance(additional_system_prompt, str) and additional_system_prompt.strip():
            dispatch_kwargs["additional_system_prompt"] = additional_system_prompt.strip()
    if not stream_requested:
        response = await ChatDispatcher.dispatch(
            **dispatch_kwargs,
            context_summary_lifecycle_callback=partial(
                _publish_interactive_work_stream_event,
                stream_state,
            )
            if update_session_context
            else None,
        )
        if scheduled_task_work:
            response.pop("llm_request_metadata", None)
        elif isinstance(response.get("llm_request_metadata"), dict):
            response["llm_request_metadata"] = _metadata_with_work_order(
                metadata_work,
                {
                    **response["llm_request_metadata"],
                    **stream_state.session_token_totals,
                },
                update_session_context=update_session_context,
            )
            if update_session_context:
                await session_crud.update_llm_request_metadata(
                    db,
                    session_id=session_id,
                    uid=uid,
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
        if event_type in {"done", "external_wait"}:
            response = event.get("response")
            response_id = event.get("response_id")
            if event_type == "done" and isinstance(response, dict) and isinstance(response_id, str) and response_id:
                response = {**response, "response_id": response_id}
        elif event_type == "error":
            error_message = str(event.get("message") or t(ERR_LLM_UNEXPECTED_ERROR))
            raise RuntimeError(error_message)
        else:
            await _publish_interactive_work_stream_event(stream_state, event)
    if not isinstance(response, dict):
        raise RuntimeError(t(ERR_LLM_UNEXPECTED_ERROR))
    if scheduled_task_work:
        response.pop("llm_request_metadata", None)
    elif isinstance(response.get("llm_request_metadata"), dict):
        response["llm_request_metadata"] = _metadata_with_work_order(
            metadata_work,
            {
                **response["llm_request_metadata"],
                **stream_state.session_token_totals,
            },
            update_session_context=update_session_context,
        )
    return response
