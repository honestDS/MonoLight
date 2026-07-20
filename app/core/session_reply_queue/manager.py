import asyncio
import hashlib
import json
from datetime import datetime
from typing import Any

from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from app.core.audit.confirmation import ConfirmationDecision, expire_confirmation_by_session, parse_confirmation_decision, update_confirmation_message_status, update_confirmation_tool_results_for_invalid_input
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
            )
            return initial_message, work, "queued"

        decision = parse_confirmation_decision(
            message,
            attachments=attachments,
            has_quote=has_quote,
        )
        if decision != ConfirmationDecision.APPROVE:
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
            if decision == ConfirmationDecision.REJECT:
                await audit_crud.close_pending(
                    db,
                    audit_record_id=current_confirmation.id,
                    uid=uid,
                    session_id=session_id,
                    status=AuditRecordStatus.REJECTED,
                    decision_message_id=message_row.id,
                    decision_raw_message=message,
                    decided_by=current_confirmation.operator_username,
                )
                await update_confirmation_message_status(db, audit_record_id=current_confirmation.id)
                submission_status = "rejected"
            else:
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
                submission_status = "cancelled_and_queued"
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
            )
            return initial_message, work, submission_status

        profile_id = profile.id if profile and profile.id else -1
        message_row = Message(
            session_id=session_id,
            uid=uid,
            role=MessageRole.USER,
            type=MessageType.TEXT,
            content=_serialize_message_content(message),
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
            decision_raw_message=message,
            decided_by=current_confirmation.operator_username,
        )
        if claimed_record is None or claim_token is None:
            raise RuntimeError(t(ERR_AUDIT_CONFIRMATION_UNAVAILABLE))
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
            "approved",
        )

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
        source_message_ids = [int(item.source_id) for item in contiguous]
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
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={
                "input_message_ids": message_ids,
                "execution_state": {
                    **(work.execution_state or {}),
                    "stream_requested": stream_requested,
                    "context_summary_events_requested": context_summary_events_requested,
                    "expose_tool_call_content": expose_tool_call_content,
                },
            },
            commit=False,
        )
        if not updated:
            await db.rollback()
            raise RuntimeError(t(ERR_SESSION_REPLY_LEASE_LOST_FREEZING_INPUT))
        await db.commit()
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

        contiguous = await session_reply_work_item_crud.list_contiguous_foreground(db, work=work)
        additional_work = [item for item in contiguous if item.id != work.id and item.status == SessionReplyWorkStatus.READY_FOR_LLM and item.source_id]
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
        updated = await session_reply_work_item_crud.update_claimed(
            db,
            work_id=work.id,
            worker_id=worker_id,
            values={"input_message_ids": [*frozen_message_ids, *message_ids]},
            commit=False,
        )
        if not updated:
            await db.rollback()
            return []

        await db.commit()
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
                    }
                    return
                if work.status == SessionReplyWorkStatus.CANCELLED:
                    raise RuntimeError(work.error or t(ERR_SESSION_REPLY_WORK_ENDED, status=work.status))
            await asyncio.sleep(WORK_RESULT_POLL_INTERVAL_SECONDS)


session_reply_queue_manager = SessionReplyQueueManager()
