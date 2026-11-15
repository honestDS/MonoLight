from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.session.tool_cancellation import complete_cancelled_tool_results_for_work
from app.models.audit import AuditRecord
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.session_reply_stream_event import SessionReplyStreamEvent
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)
from app.providers.database import ensure_sqlite_outer_transaction

MIGRATION_ID = "20261114_01_complete_cancelled_tool_results"

_BATCH_SIZE = 200


def _positive_id(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def _add_candidate_id(message_ids: set[int], value: Any) -> None:
    message_id = _positive_id(value)
    if message_id is not None:
        message_ids.add(message_id)


async def _get_confirmed_queued_result_ids(session: AsyncSession, *, work: SessionReplyWorkItem) -> list[int]:
    if work.work_type != SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION or work.source_type != SessionReplySourceType.AUDIT_RECORD:
        return []
    try:
        audit_record_id = int(work.source_id)
    except (TypeError, ValueError):
        return []
    if audit_record_id <= 0:
        return []

    result = await session.execute(
        select(Message.id)
        .where(
            Message.uid == work.uid,
            Message.session_id == work.session_id,
            Message.profile_id == work.profile_id,
            Message.role == MessageRole.TOOL,
            Message.type == MessageType.TOOL_RESULT,
            Message.audit_record_id == audit_record_id,
            Message.audit_tool_call_id.is_not(None),
        )
        .order_by(Message.id.asc())
    )
    return list(result.scalars().all())


async def migrate(session: AsyncSession) -> None:
    await ensure_sqlite_outer_transaction(session)

    last_id = 0
    while True:
        statement = (
            select(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id > last_id,
                SessionReplyWorkItem.status == SessionReplyWorkStatus.CANCELLED,
            )
            .order_by(SessionReplyWorkItem.id.asc())
            .limit(_BATCH_SIZE)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        rows = list((await session.execute(statement)).scalars().all())
        if not rows:
            break

        for work in rows:
            if work.id is None:
                continue

            state = work.execution_state if isinstance(work.execution_state, dict) else {}
            candidate_ids: set[int] = set()

            original_ids = state.get("tool_call_message_ids")
            if isinstance(original_ids, list):
                for value in original_ids:
                    _add_candidate_id(candidate_ids, value)

            checkpoint = await session_reply_work_item_crud.get_checkpoint(session, work.id)
            if isinstance(checkpoint, dict):
                turn_messages = checkpoint.get("turn_messages")
                if isinstance(turn_messages, list):
                    for turn_message in turn_messages:
                        if not isinstance(turn_message, dict):
                            continue
                        if turn_message.get("role") != MessageRole.ASSISTANT.value:
                            continue
                        tool_calls = turn_message.get("tool_calls")
                        if not isinstance(tool_calls, list) or not tool_calls:
                            continue
                        _add_candidate_id(candidate_ids, turn_message.get("id"))

            stream_result = await session.execute(
                select(SessionReplyStreamEvent.event).where(
                    SessionReplyStreamEvent.work_id == work.id,
                    SessionReplyStreamEvent.event["type"].as_string() == "turn_end",
                )
            )
            for event in stream_result.scalars().all():
                if isinstance(event, dict):
                    _add_candidate_id(candidate_ids, event.get("message_id"))

            message_result = await session.execute(
                select(Message.id).where(
                    Message.work_id == work.id,
                    Message.uid == work.uid,
                    Message.session_id == work.session_id,
                    Message.profile_id == work.profile_id,
                    Message.role == MessageRole.ASSISTANT,
                    Message.type == MessageType.TOOL_CALL,
                )
            )
            for message_id in message_result.scalars().all():
                _add_candidate_id(candidate_ids, message_id)

            audit_result = await session.execute(
                select(AuditRecord.source_assistant_message_id).where(
                    AuditRecord.uid == work.uid,
                    AuditRecord.session_id == work.session_id,
                    AuditRecord.source_work_id == work.id,
                )
            )
            for message_id in audit_result.scalars().all():
                _add_candidate_id(candidate_ids, message_id)

            message_work_id = await session_reply_work_item_crud.get_message_work_id(session, work)
            valid_ids: set[int] = set()
            for source_id in sorted(candidate_ids):
                source = await session.get(Message, source_id)
                if source is None:
                    continue
                if source.uid != work.uid:
                    continue
                if source.session_id != work.session_id:
                    continue
                if source.profile_id != work.profile_id:
                    continue
                if source.role != MessageRole.ASSISTANT:
                    continue
                if source.type != MessageType.TOOL_CALL:
                    continue
                if source.work_id != message_work_id:
                    continue
                try:
                    source_internal = InternalMessage.model_validate_json(source.content or "{}")
                except (TypeError, ValueError):
                    continue
                if source_internal.role != MessageRole.ASSISTANT or not any(call.id.strip() for call in source_internal.tool_calls or []):
                    continue
                valid_ids.add(source_id)

            if valid_ids:
                registered_ids = sorted(valid_ids)
                if state.get("tool_call_message_ids") != registered_ids:
                    state = dict(state)
                    state["tool_call_message_ids"] = registered_ids
                    work.execution_state = state
                    session.add(work)

            if not valid_ids and "tool_call_message_ids" in state:
                state = dict(state)
                state.pop("tool_call_message_ids", None)
                work.execution_state = state
                session.add(work)

            confirmed_queued_result_ids = await _get_confirmed_queued_result_ids(session, work=work)
            if valid_ids or confirmed_queued_result_ids:
                await complete_cancelled_tool_results_for_work(
                    session,
                    work=work,
                    confirmed_queued_result_ids=confirmed_queued_result_ids,
                )

        last_id = rows[-1].id

    await session.flush()
