import hashlib
import json
from typing import Any

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation_common import ConfirmationDecision
from app.core.audit.confirmation_decision import parse_confirmation_decision
from app.core.audit.confirmation_events import _broadcast_confirmation_status_update, broadcast_pending_confirmation_cancellation
from app.core.audit.confirmation_lifecycle import expire_confirmation_by_session
from app.core.audit.confirmation_projection import (
    _sync_confirmation_message_status_projection,
    update_confirmation_message_status,
)
from app.core.audit.confirmation_results import (
    cancel_persisted_pending_confirmation_bundle,
    update_confirmation_tool_results_for_decision,
)
from app.core.constants import (
    ERR_AUDIT_CONFIRMATION_INVALID_INPUT,
    ERR_AUDIT_HIGH_RISK_CONFIRMATION_INVALID_INPUT,
    ERR_CHAT_REQUEST_ID_CONFLICT,
    ERR_CHAT_REQUEST_WORK_UNAVAILABLE,
)
from app.core.crud.audit.audit import audit_crud
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.session import session_crud
from app.core.exceptions import ParameterException
from app.core.i18n import get_current_locale, t
from app.models.audit import AuditRecordStatus
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkType,
)

from .manager_common import (
    _serialize_message_content,
    get_tool_call_visibility,
    merge_work_request_ids,
)

__all__ = [
    "SessionReplySubmission",
]


class SessionReplySubmission:
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
        additional_system_prompt: str | None = None,
        idempotent_http_request: bool = False,
    ) -> tuple[InternalMessage, SessionReplyWorkItem, str, list[dict[str, Any]]]:
        profile_id = profile.id if profile and profile.id else -1
        serialized_message = _serialize_message_content(message)
        request_digest = hashlib.sha256(json.dumps([uid, session_id, request_id], separators=(",", ":")).encode("utf-8")).hexdigest()
        idempotent_dedupe_key = f"http:{request_digest[:58]}"

        def validate_idempotent_message(message_row: Message) -> None:
            if message_row.uid != uid or message_row.session_id != session_id or message_row.content != serialized_message or (message_row.attachments or []) != (attachments or []):
                raise ParameterException(ERR_CHAT_REQUEST_ID_CONFLICT)

        async def existing_idempotent_submission(
            message_row: Message,
        ) -> tuple[InternalMessage, SessionReplyWorkItem, str, list[dict[str, Any]]]:
            validate_idempotent_message(message_row)
            if message_row.id is None:
                raise ParameterException(ERR_CHAT_REQUEST_WORK_UNAVAILABLE)
            existing_work = await session_reply_work_item_crud.get_by_input_message(
                db,
                uid=uid,
                session_id=session_id,
                message_id=message_row.id,
            )
            if existing_work is None:
                raise ParameterException(ERR_CHAT_REQUEST_WORK_UNAVAILABLE)
            return (
                InternalMessage(
                    id=message_row.id,
                    role=MessageRole.USER,
                    content=message_row.content,
                    attachments=message_row.attachments,
                    guidance_prompt=message_row.guidance_prompt,
                    created_at=message_row.created_at.timestamp(),
                ),
                existing_work,
                await self._resolve_submission_status(
                    db,
                    existing_work,
                    immediate_status="accepted",
                ),
                [],
            )

        async def reserve_idempotent_message() -> tuple[Message, bool]:
            existing_message_row = await message_crud.get_by_dedupe_key(db, idempotent_dedupe_key)
            if existing_message_row is not None:
                message_row = existing_message_row
                message_was_existing = True
            else:
                # Keep the creator flag across the unique-key race instead of
                # inferring it from the row returned after an insert conflict.
                message_row = Message(
                    session_id=session_id,
                    uid=uid,
                    role=MessageRole.USER,
                    type=MessageType.TEXT,
                    content=serialized_message,
                    attachments=attachments,
                    profile_id=profile_id,
                    is_processed=False,
                    dedupe_key=idempotent_dedupe_key,
                )
                try:
                    async with db.begin_nested():
                        db.add(message_row)
                        await db.flush()
                except IntegrityError:
                    message_row = await message_crud.get_by_dedupe_key(db, idempotent_dedupe_key)
                    if message_row is None:
                        raise
                    message_was_existing = True
                else:
                    await db.refresh(message_row)
                    message_was_existing = False
            validate_idempotent_message(message_row)
            return message_row, message_was_existing

        idempotent_message_row: Message | None = None
        idempotent_message_was_existing = False
        if idempotent_http_request:
            existing_message_row = await message_crud.get_by_dedupe_key(db, idempotent_dedupe_key)
            if existing_message_row is not None:
                return await existing_idempotent_submission(existing_message_row)

        await expire_confirmation_by_session(db, uid=uid, session_id=session_id)

        if idempotent_http_request:
            idempotent_message_row, idempotent_message_was_existing = await reserve_idempotent_message()
            if idempotent_message_was_existing:
                return await existing_idempotent_submission(idempotent_message_row)

        cleaned_additional_system_prompt = additional_system_prompt.strip() if isinstance(additional_system_prompt, str) else ""
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
                additional_system_prompt=cleaned_additional_system_prompt or None,
                **({"persisted_message_row": idempotent_message_row} if idempotent_message_row is not None else {}),
            )
            submission_status = await self._resolve_submission_status(
                db,
                work,
                immediate_status="accepted",
            )
            return initial_message, work, submission_status, []

        current_confirmation_id = current_confirmation.id
        requires_high_risk_override = await audit_crud.requires_high_risk_override(
            db,
            current_confirmation_id,
        )
        decision = parse_confirmation_decision(
            message,
            attachments=attachments,
            has_quote=has_quote,
            requires_high_risk_override=requires_high_risk_override,
        )
        if decision == ConfirmationDecision.REJECT:
            profile_id = profile.id if profile and profile.id else -1
            decision_raw_message = message if isinstance(message, str) else _serialize_message_content(message)
            message_row = idempotent_message_row
            if message_row is None:
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
            else:
                message_row.type = MessageType.AUDIT_DECISION
                message_row.content = decision_raw_message
                message_row.attachments = None
                message_row.profile_id = profile_id
                message_row.is_processed = False
            db.add(message_row)
            await db.flush()
            await audit_crud.close_pending(
                db,
                audit_record_id=current_confirmation_id,
                uid=uid,
                session_id=session_id,
                status=AuditRecordStatus.REJECTED,
                decision_message_id=message_row.id,
                decision_raw_message=decision_raw_message,
                decided_by=current_confirmation.operator_username,
                **({"commit": False} if idempotent_http_request else {}),
            )
            await update_confirmation_tool_results_for_decision(
                db,
                audit_record_id=current_confirmation_id,
                before_message_id=message_row.id,
                decision=decision,
                raw_message=decision_raw_message,
            )
            await update_confirmation_message_status(
                db,
                audit_record_id=current_confirmation_id,
                **({"commit": False} if idempotent_http_request else {}),
            )
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
                additional_system_prompt=cleaned_additional_system_prompt or None,
            )
            submission_status = await self._resolve_submission_status(
                db,
                work,
                immediate_status="rejected",
            )
            confirmation_update_events = await self._build_direct_confirmation_update_events(
                db,
                source=source,
                audit_record_id=current_confirmation_id,
                work=work,
            )
            return initial_message, work, submission_status, confirmation_update_events

        if decision is None:
            profile_id = profile.id if profile and profile.id else -1
            message_row = idempotent_message_row
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
            else:
                message_row.type = MessageType.TEXT
                message_row.content = _serialize_message_content(message)
                message_row.attachments = attachments
                message_row.profile_id = profile_id
                message_row.is_processed = False
            db.add(message_row)
            try:
                await db.flush()
                invalid_input_feedback = t(
                    ERR_AUDIT_HIGH_RISK_CONFIRMATION_INVALID_INPUT if requires_high_risk_override else ERR_AUDIT_CONFIRMATION_INVALID_INPUT,
                    locale=current_confirmation.language,
                )
                cancellation = await cancel_persisted_pending_confirmation_bundle(
                    db,
                    audit_record_id=current_confirmation_id,
                    uid=uid,
                    session_id=session_id,
                    feedback=invalid_input_feedback,
                    confirmation_status="invalid_input",
                    commit=False,
                )
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
                    commit=False,
                    additional_system_prompt=cleaned_additional_system_prompt or None,
                )
                await db.commit()
            except Exception:
                await db.rollback()
                raise
            await db.refresh(message_row)
            await db.refresh(work)
            await broadcast_pending_confirmation_cancellation(db, cancellation=cancellation)
            submission_status = await self._resolve_submission_status(
                db,
                work,
                immediate_status="cancelled",
            )
            confirmation_update_events = await self._build_direct_confirmation_update_events(
                db,
                source=source,
                audit_record_id=current_confirmation_id,
                work=work,
            )
            return initial_message, work, submission_status, confirmation_update_events

        profile_id = profile.id if profile and profile.id else -1
        decision_raw_message = message if isinstance(message, str) else _serialize_message_content(message)
        message_row = idempotent_message_row
        if message_row is None:
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
        else:
            message_row.type = MessageType.AUDIT_DECISION
            message_row.content = decision_raw_message
            message_row.attachments = None
            message_row.profile_id = profile_id
            message_row.is_processed = True
        db.add(message_row)
        await db.flush()
        claimed_record, claim_token = await audit_crud.claim_pending_for_execution(
            db,
            audit_record_id=current_confirmation_id,
            uid=uid,
            session_id=session_id,
            decision_message_id=message_row.id,
            decision_raw_message=decision_raw_message,
            decided_by=current_confirmation.operator_username,
            commit=False,
        )
        if claimed_record is None or claim_token is None:
            # The competing transaction already consumed this confirmation. Its
            # rollback removed the provisional decision message, so persist the
            # original input once as ordinary foreground work without parsing it again.
            if idempotent_http_request:
                message_row = idempotent_message_row
                message_was_existing = idempotent_message_was_existing
                if message_row is None:
                    message_row, message_was_existing = await reserve_idempotent_message()
                if message_row.id is not None:
                    existing_work = await session_reply_work_item_crud.get_by_input_message(
                        db,
                        uid=uid,
                        session_id=session_id,
                        message_id=message_row.id,
                    )
                    if existing_work is not None:
                        return (
                            InternalMessage(
                                id=message_row.id,
                                role=MessageRole.USER,
                                content=message_row.content,
                                attachments=message_row.attachments,
                                guidance_prompt=message_row.guidance_prompt,
                                created_at=message_row.created_at.timestamp(),
                            ),
                            existing_work,
                            await self._resolve_submission_status(
                                db,
                                existing_work,
                                immediate_status="accepted",
                            ),
                            [],
                        )
                    if message_was_existing:
                        raise ParameterException(ERR_CHAT_REQUEST_WORK_UNAVAILABLE)
                message_row.type = MessageType.TEXT
                message_row.content = _serialize_message_content(message)
                message_row.attachments = attachments
                message_row.profile_id = profile_id
                message_row.is_processed = False
                db.add(message_row)
                await db.flush()
            else:
                message_row = None
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
                additional_system_prompt=cleaned_additional_system_prompt or None,
                **({"persisted_message_row": message_row} if message_row is not None else {}),
            )
            submission_status = await self._resolve_submission_status(
                db,
                work,
                immediate_status="accepted",
            )
            confirmation_update_events = await self._build_direct_confirmation_update_events(
                db,
                source=source,
                audit_record_id=current_confirmation_id,
                work=work,
            )
            return initial_message, work, submission_status, confirmation_update_events
        await update_confirmation_tool_results_for_decision(
            db,
            audit_record_id=claimed_record.id,
            before_message_id=message_row.id,
            decision=decision,
            raw_message=decision_raw_message,
        )
        confirmation_projection = await _sync_confirmation_message_status_projection(
            db,
            audit_record_id=claimed_record.id,
            commit=False,
        )
        guidance_prompt = None
        if source not in {"http", "ws"}:
            guidance_prompt = await message_crud.activate_and_get_guidance_prompt(
                db,
                session_id=session_id,
                uid=uid,
            )
            message_row.guidance_prompt = guidance_prompt
            db.add(message_row)
        session = await session_crud.get_by_session_id(db, session_id)
        show_tool_calls, expose_tool_call_content = get_tool_call_visibility(session, source)
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
            "show_tool_calls": show_tool_calls,
            "expose_tool_call_content": expose_tool_call_content,
            "language": get_current_locale(),
            "message_source": source,
            "request_ids": merge_work_request_ids(work, request_id=request_id),
            "guidance_prompt": guidance_prompt,
        }
        if cleaned_additional_system_prompt:
            work.execution_state["additional_system_prompt"] = cleaned_additional_system_prompt
        db.add(work)
        await db.commit()
        if confirmation_projection is not None and confirmation_projection.status_update is not None:
            await _broadcast_confirmation_status_update(
                db,
                status_update=confirmation_projection.status_update,
            )
        await db.refresh(message_row)
        await db.refresh(work)
        confirmation_update_events = await self._build_direct_confirmation_update_events(
            db,
            source=source,
            audit_record_id=claimed_record.id,
            work=work,
        )
        return (
            InternalMessage(
                id=message_row.id,
                role=MessageRole.USER,
                content=message_row.content,
                guidance_prompt=message_row.guidance_prompt,
                created_at=message_row.created_at.timestamp(),
            ),
            work,
            await self._resolve_submission_status(
                db,
                work,
                immediate_status="approved",
            ),
            confirmation_update_events,
        )
