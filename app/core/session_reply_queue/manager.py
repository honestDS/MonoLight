import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.audit.confirmation import ConfirmationDecision, expire_confirmation_by_session, parse_confirmation_decision, update_confirmation_message_status, update_confirmation_tool_results_for_decision, update_confirmation_tool_results_for_invalid_input
from app.core.constants import (
    ERR_AUDIT_CONFIRMATION_INVALID_INPUT,
    ERR_AUDIT_CONFIRMATION_UNAVAILABLE,
    ERR_LLM_UNEXPECTED_ERROR,
    ERR_PERSISTED_USER_MESSAGE_MISMATCH,
    ERR_SESSION_REPLY_LEASE_LOST_FREEZING_INPUT,
    ERR_SESSION_REPLY_NO_FOREGROUND_INPUT,
    ERR_SESSION_REPLY_WORK_ENDED,
    ERR_SESSION_REPLY_WORK_NOT_FOUND,
)
from app.core.crud.audit import audit_crud
from app.core.crud.session import session_crud
from app.core.crud.session_reply_stream_event import session_reply_stream_event_crud
from app.core.crud.session_reply_work_item import session_reply_work_item_crud
from app.core.exceptions import BaseBusinessException
from app.core.i18n import get_current_locale, t
from app.core.log import get_logger
from app.core.utils.dispatcher.markdown_instruction import append_user_runtime_instructions
from app.models.audit import AuditRecordStatus
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkStatus,
    SessionReplyWorkType,
)

logger = get_logger(__name__)

WORK_RESULT_POLL_INTERVAL_SECONDS = 0.2


def get_work_request_ids(work: SessionReplyWorkItem) -> list[str]:
    state = getattr(work, "execution_state", None)
    request_ids = state.get("request_ids") if isinstance(state, dict) else None
    if not isinstance(request_ids, list):
        return []
    unique_request_ids: list[str] = []
    seen: set[str] = set()
    for request_id in request_ids:
        if isinstance(request_id, str) and request_id and request_id not in seen:
            seen.add(request_id)
            unique_request_ids.append(request_id)
    return unique_request_ids


def merge_work_request_ids(
    *works: SessionReplyWorkItem,
    request_id: str | None = None,
) -> list[str]:
    merged_request_ids: list[str] = []
    seen: set[str] = set()
    for work in works:
        for item_request_id in get_work_request_ids(work):
            if item_request_id not in seen:
                seen.add(item_request_id)
                merged_request_ids.append(item_request_id)
    if isinstance(request_id, str) and request_id and request_id not in seen:
        merged_request_ids.append(request_id)
    return merged_request_ids


def is_submission_queued(submission_status: str) -> bool:
    return submission_status == "queued" or submission_status.endswith("_and_queued")


def build_input_queued_event(
    session_id: str,
    request_id: str,
    work_id: int | None,
    submission_status: str,
) -> dict[str, Any]:
    return {
        "type": "input_queued",
        "session_id": session_id,
        "request_id": request_id,
        "work_id": work_id,
        "submission_status": submission_status,
    }


def _serialize_message_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    if hasattr(content, "model_dump"):
        content = content.model_dump(mode="json")
    return json.dumps(content, ensure_ascii=False)


def build_foreground_message_dedupe_key(session_id: str, message_id: int) -> str:
    session_digest = hashlib.sha256(session_id.encode("utf-8")).hexdigest()[:16]
    return f"foreground-message:{session_digest}:{message_id}"


def build_session_reply_work_identity(work: SessionReplyWorkItem) -> str:
    created_at = work.created_at.replace(tzinfo=None).isoformat(timespec="microseconds") if isinstance(work.created_at, datetime) else str(work.created_at)
    payload = json.dumps(
        {
            "dedupe_key": work.dedupe_key,
            "created_at": created_at,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def build_session_reply_work_event_id(work: SessionReplyWorkItem, *, error: bool = False) -> str:
    return f"session-reply-work:{build_session_reply_work_identity(work)}:{'error' if error else 'event'}"


async def _get_work_failure_content(db: AsyncSession, work: SessionReplyWorkItem) -> str:
    message = await db.get(Message, work.result_message_id) if work.result_message_id else None
    return message.content if message and message.content else t(ERR_LLM_UNEXPECTED_ERROR)


async def _raise_work_failure(db: AsyncSession, work: SessionReplyWorkItem) -> None:
    error_content = await _get_work_failure_content(db, work)
    raise BaseBusinessException(
        message=error_content,
        default_message=error_content,
        data={
            "work_id": work.id,
            "event_id": build_session_reply_work_event_id(work, error=True),
        },
    )


class SessionReplyQueueManager:
    async def submit_user_message(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile: Profile,
        message: str | list[dict[str, Any]],
        attachments: list[str] | None,
        source: str,
        stream_requested: bool | None = None,
        context_summary_events_requested: bool | None = None,
        has_quote: bool = False,
        request_id: str | None = None,
    ) -> tuple[InternalMessage, SessionReplyWorkItem, str]:
        await expire_confirmation_by_session(db, uid=uid, session_id=session_id)
        current_confirmation = await audit_crud.get_current_confirmation(db, uid=uid, session_id=session_id)
        if current_confirmation is None:
            initial_message, work = await self._enqueue_foreground_message(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                message=message,
                attachments=attachments,
                source=source,
                stream_requested=stream_requested,
                context_summary_events_requested=context_summary_events_requested,
                request_id=request_id,
            )
            submission_status = await self._resolve_submission_status(
                db,
                work,
                immediate_status="accepted",
            )
            return initial_message, work, submission_status

        decision = parse_confirmation_decision(
            message,
            attachments=attachments,
            has_quote=has_quote,
        )
        if decision == ConfirmationDecision.REJECT:
            profile_id = profile.id if profile and profile.id else -1
            decision_raw_message = message if isinstance(message, str) else _serialize_message_content(message)
            message_row = Message(
                session_id=session_id,
                uid=uid,
                role=MessageRole.USER,
                type=MessageType.AUDIT_DECISION,
                content=decision_raw_message,
                attachments=None,
                profile_id=profile_id,
                is_processed=False,
            )
            db.add(message_row)
            await db.flush()
            await audit_crud.close_pending(
                db,
                audit_record_id=current_confirmation.id,
                uid=uid,
                session_id=session_id,
                status=AuditRecordStatus.REJECTED,
                decision_message_id=message_row.id,
                decision_raw_message=decision_raw_message,
                decided_by=current_confirmation.operator_username,
            )
            await update_confirmation_tool_results_for_decision(
                db,
                audit_record_id=current_confirmation.id,
                before_message_id=message_row.id,
                decision=decision,
                raw_message=decision_raw_message,
            )
            await update_confirmation_message_status(db, audit_record_id=current_confirmation.id)
            initial_message, work = await self._enqueue_foreground_message(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                message=message,
                attachments=None,
                source=source,
                stream_requested=stream_requested,
                context_summary_events_requested=context_summary_events_requested,
                persisted_message_row=message_row,
                audit_decision_response=True,
                request_id=request_id,
            )
            submission_status = await self._resolve_submission_status(
                db,
                work,
                immediate_status="rejected",
            )
            return initial_message, work, submission_status

        if decision is None:
            profile_id = profile.id if profile and profile.id else -1
            message_row = Message(
                session_id=session_id,
                uid=uid,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content=_serialize_message_content(message),
                attachments=attachments,
                profile_id=profile_id,
                is_processed=False,
            )
            db.add(message_row)
            await db.flush()
            invalid_input_feedback = t(ERR_AUDIT_CONFIRMATION_INVALID_INPUT, locale=current_confirmation.language)
            cancelled_count = await audit_crud.cancel_confirmation_by_session(
                db,
                uid=uid,
                session_id=session_id,
                error_reason=invalid_input_feedback,
            )
            if cancelled_count:
                await update_confirmation_tool_results_for_invalid_input(
                    db,
                    audit_record_id=current_confirmation.id,
                    before_message_id=message_row.id,
                    feedback=invalid_input_feedback,
                )
            await update_confirmation_message_status(db, audit_record_id=current_confirmation.id)
            initial_message, work = await self._enqueue_foreground_message(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                message=message,
                attachments=attachments,
                source=source,
                stream_requested=stream_requested,
                context_summary_events_requested=context_summary_events_requested,
                persisted_message_row=message_row,
                request_id=request_id,
            )
            submission_status = await self._resolve_submission_status(
                db,
                work,
                immediate_status="cancelled",
            )
            return initial_message, work, submission_status

        profile_id = profile.id if profile and profile.id else -1
        decision_raw_message = message if isinstance(message, str) else _serialize_message_content(message)
        message_row = Message(
            session_id=session_id,
            uid=uid,
            role=MessageRole.USER,
            type=MessageType.AUDIT_DECISION,
            content=decision_raw_message,
            attachments=None,
            profile_id=profile_id,
            is_processed=True,
        )
        db.add(message_row)
        await db.flush()
        claimed_record, claim_token = await audit_crud.claim_pending_for_execution(
            db,
            audit_record_id=current_confirmation.id,
            uid=uid,
            session_id=session_id,
            decision_message_id=message_row.id,
            decision_raw_message=decision_raw_message,
            decided_by=current_confirmation.operator_username,
        )
        if claimed_record is None or claim_token is None:
            raise RuntimeError(t(ERR_AUDIT_CONFIRMATION_UNAVAILABLE))
        await update_confirmation_tool_results_for_decision(
            db,
            audit_record_id=claimed_record.id,
            before_message_id=message_row.id,
            decision=decision,
            raw_message=decision_raw_message,
        )
        await update_confirmation_message_status(db, audit_record_id=claimed_record.id)
        work, _created = await session_reply_work_item_crud.enqueue(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            work_type=SessionReplyWorkType.CONFIRMED_TOOL_EXECUTION,
            source_type=SessionReplySourceType.AUDIT_RECORD,
            source_id=claimed_record.id,
            dedupe_key=f"confirmed-audit:{claimed_record.id}",
            commit=False,
        )
        work.execution_state = {
            **(work.execution_state or {}),
            "audit_claim_token": claim_token,
            "decision_message_id": message_row.id,
            "stream_requested": source == "ws" if stream_requested is None else stream_requested,
            "context_summary_events_requested": source == "ws" if context_summary_events_requested is None else context_summary_events_requested,
            "expose_tool_call_content": source != "weixin-openclaw",
            "language": get_current_locale(),
            "message_source": source,
            "request_ids": merge_work_request_ids(work, request_id=request_id),
        }
        db.add(work)
        await db.commit()
        await db.refresh(message_row)
        await db.refresh(work)
        return (
            InternalMessage(
                id=message_row.id,
                role=MessageRole.USER,
                content=message_row.content,
                created_at=message_row.created_at.timestamp(),
            ),
            work,
            await self._resolve_submission_status(
                db,
                work,
                immediate_status="approved",
            ),
        )

    async def _resolve_submission_status(
        self,
        db: AsyncSession,
        work: SessionReplyWorkItem,
        *,
        immediate_status: str,
    ) -> str:
        if not await session_reply_work_item_crud.has_nonterminal_predecessor(db, work):
            return immediate_status
        if immediate_status == "accepted":
            return "queued"
        return f"{immediate_status}_and_queued"

    async def enqueue_foreground_message(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile: Profile,
        message: str | list[dict[str, Any]],
        attachments: list[str] | None,
        source: str,
        stream_requested: bool | None = None,
        context_summary_events_requested: bool | None = None,
        has_quote: bool = False,
        request_id: str | None = None,
    ) -> tuple[InternalMessage, SessionReplyWorkItem]:
        if not hasattr(db, "execute"):
            return await self._enqueue_foreground_message(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                message=message,
                attachments=attachments,
                source=source,
                stream_requested=stream_requested,
                context_summary_events_requested=context_summary_events_requested,
                request_id=request_id,
            )
        initial_message, work, _status = await self.submit_user_message(
            db,
            uid=uid,
            session_id=session_id,
            profile=profile,
            message=message,
            attachments=attachments,
            source=source,
            stream_requested=stream_requested,
            context_summary_events_requested=context_summary_events_requested,
            has_quote=has_quote,
            request_id=request_id,
        )
        return initial_message, work

    async def _enqueue_foreground_message(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile: Profile,
        message: str | list[dict[str, Any]],
        attachments: list[str] | None,
        source: str,
        stream_requested: bool | None = None,
        context_summary_events_requested: bool | None = None,
        persisted_message_row: Message | None = None,
        audit_decision_response: bool = False,
        request_id: str | None = None,
    ) -> tuple[InternalMessage, SessionReplyWorkItem]:
        profile_id = profile.id if profile and profile.id else -1
        if profile_id > 0:
            await session_crud.upsert_profile(
                db,
                session_id=session_id,
                uid=uid,
                profile_id=profile_id,
                source=source,
            )

        message_row = persisted_message_row
        if message_row is None:
            message_row = Message(
                session_id=session_id,
                uid=uid,
                role=MessageRole.USER,
                type=MessageType.TEXT,
                content=_serialize_message_content(message),
                attachments=attachments,
                profile_id=profile_id,
                is_processed=False,
            )
            db.add(message_row)
            await db.flush()
        elif message_row.uid != uid or message_row.session_id != session_id or message_row.profile_id != profile_id:
            raise ValueError(t(ERR_PERSISTED_USER_MESSAGE_MISMATCH))
        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            work_type=SessionReplyWorkType.FOREGROUND_REPLY,
            source_type=SessionReplySourceType.USER_MESSAGE,
            source_id=message_row.id,
            dedupe_key=build_foreground_message_dedupe_key(session_id, message_row.id),
            commit=False,
        )
        if created:
            work.execution_state = {
                **(work.execution_state or {}),
                "stream_requested": source == "ws" if stream_requested is None else stream_requested,
                "context_summary_events_requested": source == "ws" if context_summary_events_requested is None else context_summary_events_requested,
                # 微信 OpenClaw 对发送频率有限制，工具调用阶段的正文只保留在
                # 数据库和日志中，不作为额外的用户可见消息发送到微信。
                "expose_tool_call_content": source != "weixin-openclaw",
                "language": get_current_locale(),
                "message_source": source,
                "audit_decision_response": audit_decision_response,
            }
        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        state["request_ids"] = merge_work_request_ids(work, request_id=request_id)
        work.execution_state = state
        db.add(work)
        await db.commit()
        await db.refresh(message_row)
        await db.refresh(work)
        return (
            InternalMessage(
                id=message_row.id,
                role=MessageRole.USER,
                content=message_row.content,
                attachments=message_row.attachments,
                created_at=message_row.created_at.timestamp(),
            ),
            work,
        )

    async def enqueue_background_summary(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        background_task_id: int,
        commit: bool = True,
    ) -> tuple[SessionReplyWorkItem, bool]:
        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            work_type=SessionReplyWorkType.BACKGROUND_TOOL_SUMMARY,
            source_type=SessionReplySourceType.BACKGROUND_TASK,
            source_id=background_task_id,
            dedupe_key=f"background-task-summary:{background_task_id}",
            commit=False,
        )
        if created:
            work.execution_state = {
                **(work.execution_state or {}),
                "language": get_current_locale(),
            }
            db.add(work)
        if commit:
            await db.commit()
            await db.refresh(work)
        else:
            await db.flush()
        return work, created

    async def enqueue_scheduled_summary(
        self,
        db: AsyncSession,
        *,
        uid: str,
        session_id: str,
        profile_id: int,
        scheduled_task_id: int,
        trigger_message_id: int,
        commit: bool = True,
    ) -> tuple[SessionReplyWorkItem, bool]:
        work, created = await session_reply_work_item_crud.enqueue(
            db,
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            work_type=SessionReplyWorkType.SCHEDULED_TASK_SUMMARY,
            source_type=SessionReplySourceType.SCHEDULED_TASK_RUN,
            source_id=trigger_message_id,
            dedupe_key=f"scheduled-task-summary:{scheduled_task_id}:{trigger_message_id}",
            commit=False,
        )
        if created:
            work.execution_state = {
                **(work.execution_state or {}),
                "language": get_current_locale(),
            }
            db.add(work)
        if commit:
            await db.commit()
            await db.refresh(work)
        else:
            await db.flush()
        return work, created

    async def freeze_foreground_input(
        self,
        db: AsyncSession,
        *,
        work: SessionReplyWorkItem,
        worker_id: str,
    ) -> tuple[str, list[str], list[int]]:
        if work.input_message_ids:
            return await self._load_frozen_input(db, work.input_message_ids)

        contiguous = await session_reply_work_item_crud.list_contiguous_foreground(db, work=work)
        bounded_contiguous: list[SessionReplyWorkItem] = []
        for item in contiguous:
            is_audit_decision = bool((item.execution_state or {}).get("audit_decision_response"))
            if is_audit_decision and item.id != work.id:
                break
            bounded_contiguous.append(item)
            if is_audit_decision:
                break
        contiguous = bounded_contiguous
        source_message_ids = [int(item.source_id) for item in contiguous]
        message_types = [MessageType.AUDIT_DECISION] if bool((work.execution_state or {}).get("audit_decision_response")) else [MessageType.TEXT]
        message_result = await db.execute(
            select(Message)
            .where(
                Message.id.in_(source_message_ids),
                Message.uid == work.uid,
                Message.session_id == work.session_id,
                Message.role == MessageRole.USER,
                Message.type.in_(message_types),
                Message.is_processed == False,  # noqa: E712
            )
            .order_by(Message.id)
        )
        messages = list(message_result.scalars().all())
        message_ids = [message.id for message in messages if message.id is not None]
        if not message_ids:
            if work.input_message_ids:
                return await self._load_frozen_input(db, work.input_message_ids)
            raise RuntimeError(t(ERR_SESSION_REPLY_NO_FOREGROUND_INPUT))

        await db.execute(update(Message).where(Message.id.in_(message_ids)).values(is_processed=True))
        merged_ids = [item.id for item in contiguous[1:] if item.id is not None]
        if merged_ids:
            await db.execute(
                update(SessionReplyWorkItem)
                .where(
                    SessionReplyWorkItem.id.in_(merged_ids),
                    SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
                )
                .values(
                    status=SessionReplyWorkStatus.MERGED,
                    merged_into_id=work.id,
                    locked_by=None,
                    lock_until=None,
                )
            )
        stream_requested = any(bool((item.execution_state or {}).get("stream_requested")) for item in contiguous)
        context_summary_events_requested = any(bool((item.execution_state or {}).get("context_summary_events_requested")) for item in contiguous)
        expose_tool_call_content = all(bool((item.execution_state or {}).get("expose_tool_call_content", True)) for item in contiguous)
        execution_state = {
            **(work.execution_state or {}),
            "stream_requested": stream_requested,
            "context_summary_events_requested": context_summary_events_requested,
            "expose_tool_call_content": expose_tool_call_content,
            "request_ids": merge_work_request_ids(*contiguous),
        }
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={
                "input_message_ids": message_ids,
                "execution_state": execution_state,
            },
            commit=False,
        )
        if not updated:
            await db.rollback()
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_FREEZING_INPUT))
        await db.commit()
        work.input_message_ids = message_ids
        work.execution_state = execution_state
        return self._merge_messages(messages)

    async def absorb_contiguous_foreground_messages(
        self,
        db: AsyncSession,
        *,
        work_id: int,
        worker_id: str,
    ) -> list[InternalMessage]:
        work = await session_reply_work_item_crud.get(db, work_id)
        if work is None or work.status != SessionReplyWorkStatus.RUNNING or work.locked_by != worker_id or work.work_type != SessionReplyWorkType.FOREGROUND_REPLY:
            return []
        if bool((work.execution_state or {}).get("audit_decision_response")):
            return []

        contiguous = await session_reply_work_item_crud.list_contiguous_foreground(db, work=work)
        additional_work: list[SessionReplyWorkItem] = []
        for item in contiguous:
            if item.id == work.id:
                continue
            if bool((item.execution_state or {}).get("audit_decision_response")):
                break
            if item.status != SessionReplyWorkStatus.READY_FOR_LLM or not item.source_id:
                continue
            additional_work.append(item)
        if not additional_work:
            return []

        source_message_ids = [int(item.source_id) for item in additional_work]
        message_result = await db.execute(
            select(Message)
            .where(
                Message.id.in_(source_message_ids),
                Message.uid == work.uid,
                Message.session_id == work.session_id,
                Message.role == MessageRole.USER,
                Message.type == MessageType.TEXT,
                Message.is_processed == False,  # noqa: E712
            )
            .order_by(Message.id)
        )
        messages = list(message_result.scalars().all())
        message_ids = [message.id for message in messages if message.id is not None]
        if not message_ids:
            return []

        await db.execute(update(Message).where(Message.id.in_(message_ids)).values(is_processed=True))
        merged_work_ids = [item.id for item in additional_work if item.id is not None]
        await db.execute(
            update(SessionReplyWorkItem)
            .where(
                SessionReplyWorkItem.id.in_(merged_work_ids),
                SessionReplyWorkItem.status == SessionReplyWorkStatus.READY_FOR_LLM,
            )
            .values(
                status=SessionReplyWorkStatus.MERGED,
                merged_into_id=work.id,
                locked_by=None,
                lock_until=None,
            )
        )
        frozen_message_ids = list(work.input_message_ids or [])
        execution_state = {
            **(work.execution_state or {}),
            "request_ids": merge_work_request_ids(work, *additional_work),
        }
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={
                "input_message_ids": [*frozen_message_ids, *message_ids],
                "execution_state": execution_state,
            },
            commit=False,
        )
        if not updated:
            await db.rollback()
            return []

        await db.commit()
        work.input_message_ids = [*frozen_message_ids, *message_ids]
        work.execution_state = execution_state
        content, attachments, _ids = self._merge_messages(messages)
        combined_message = InternalMessage(
            id=message_ids[-1],
            role=MessageRole.USER,
            content=content or None,
            attachments=attachments or None,
        )
        logger.bind(
            uid=work.uid,
            session_id=work.session_id,
            work_id=work.id,
            message_ids=message_ids,
        ).info(
            t(
                "LOG_DISPATCHER_NON_STREAM_ADDITIONAL_MESSAGES",
                message=content,
                attachments=str(attachments),
            )
        )
        await append_user_runtime_instructions(db, work.session_id, combined_message)
        return [combined_message]

    async def _load_frozen_input(self, db: AsyncSession, message_ids: list[int]) -> tuple[str, list[str], list[int]]:
        result = await db.execute(select(Message).where(Message.id.in_(message_ids)).order_by(Message.id))
        messages = list(result.scalars().all())
        content, attachments, _ids = self._merge_messages(messages)
        return content, attachments, message_ids

    @staticmethod
    def _merge_messages(messages: list[Message]) -> tuple[str, list[str], list[int]]:
        contents = [message.content or "" for message in messages]
        attachments: list[str] = []
        seen: set[str] = set()
        for message in messages:
            for attachment in message.attachments or []:
                if attachment not in seen:
                    seen.add(attachment)
                    attachments.append(attachment)
        return "\n".join(contents), attachments, [message.id for message in messages if message.id is not None]

    async def wait_for_result(self, work_id: int) -> dict[str, Any]:
        from app.providers.database import AsyncSessionLocal

        while True:
            async with AsyncSessionLocal() as db:
                work = await session_reply_work_item_crud.resolve_merged_target(db, work_id)
                if work is None:
                    raise RuntimeError(t(ERR_SESSION_REPLY_WORK_NOT_FOUND))
                if work.status == SessionReplyWorkStatus.SUCCEEDED:
                    response = (work.execution_state or {}).get("response")
                    if isinstance(response, dict):
                        return {**response, "work_id": work.id}
                    if work.result_message_id:
                        message = await db.get(Message, work.result_message_id)
                        return {"content": message.content if message else "", "work_id": work.id}
                    return {"content": "", "work_id": work.id}
                if work.status == SessionReplyWorkStatus.FAILED:
                    await _raise_work_failure(db, work)
                if work.status == SessionReplyWorkStatus.CANCELLED:
                    raise RuntimeError(work.error or t(ERR_SESSION_REPLY_WORK_ENDED, status=work.status))
            await asyncio.sleep(WORK_RESULT_POLL_INTERVAL_SECONDS)

    async def wait_for_stream(self, work_id: int):
        from app.providers.database import AsyncSessionLocal

        target_work_id = work_id
        after_sequence_no = 0
        while True:
            async with AsyncSessionLocal() as db:
                work = await session_reply_work_item_crud.resolve_merged_target(db, target_work_id)
                if work is None:
                    raise RuntimeError(t(ERR_SESSION_REPLY_WORK_NOT_FOUND))
                if work.id != target_work_id:
                    target_work_id = work.id
                    after_sequence_no = 0

                events = await session_reply_stream_event_crud.list_after_sequence(
                    db,
                    work_id=target_work_id,
                    after_sequence_no=after_sequence_no,
                )
                for item in events:
                    after_sequence_no = item.sequence_no
                    yield item.event

                if work.status == SessionReplyWorkStatus.SUCCEEDED:
                    response = (work.execution_state or {}).get("response")
                    if not isinstance(response, dict):
                        response = await self.wait_for_result(target_work_id)
                    yield {
                        "type": "done",
                        "session_id": work.session_id,
                        "work_id": target_work_id,
                        "response_id": f"session-reply-work:{target_work_id}",
                        "history": response.get("history", []),
                        "files": response.get("files"),
                        "response": response,
                        "request_ids": get_work_request_ids(work),
                    }
                    return
                if work.status == SessionReplyWorkStatus.FAILED:
                    error_content = await _get_work_failure_content(db, work)
                    yield {
                        "event_id": build_session_reply_work_event_id(work, error=True),
                        "type": "error",
                        "message": error_content,
                        "session_id": work.session_id,
                        "work_id": target_work_id,
                        "request_ids": get_work_request_ids(work),
                    }
                    return
                if work.status == SessionReplyWorkStatus.CANCELLED:
                    raise RuntimeError(work.error or t(ERR_SESSION_REPLY_WORK_ENDED, status=work.status))
            await asyncio.sleep(WORK_RESULT_POLL_INTERVAL_SECONDS)


session_reply_queue_manager = SessionReplyQueueManager()
