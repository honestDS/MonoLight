from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.audit.confirmation import (
    build_confirmation_update_events,
)
from app.core.constants import (
    ERR_PERSISTED_USER_MESSAGE_MISMATCH,
    ERR_PROFILE_NOT_FOUND,
)
from app.core.crud.session.message import message_crud
from app.core.crud.session.reply_work_item import session_reply_work_item_crud
from app.core.crud.session.session import session_crud
from app.core.exceptions import ResourceNotFoundException
from app.core.i18n import get_current_locale, t
from app.models.message import InternalMessage, Message, MessageRole, MessageType
from app.models.profile import Profile
from app.models.session_reply_work_item import (
    SessionReplySourceType,
    SessionReplyWorkItem,
    SessionReplyWorkType,
)

from .manager_common import (
    _serialize_message_content,
    build_foreground_message_dedupe_key,
    get_tool_call_visibility,
    merge_work_request_ids,
)

__all__ = [
    "SessionReplyEnqueue",
]


class SessionReplyEnqueue:
    async def _build_direct_confirmation_update_events(
        self,
        db: AsyncSession,
        *,
        source: str,
        audit_record_id: int | None,
        work: SessionReplyWorkItem,
    ) -> list[dict[str, Any]]:
        if source not in {"http", "ws"} or audit_record_id is None:
            return []
        execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
        return await build_confirmation_update_events(
            db,
            audit_record_id=audit_record_id,
            include_tool_results=bool(execution_state.get("show_tool_calls", True)),
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
        additional_system_prompt: str | None = None,
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
                additional_system_prompt=additional_system_prompt,
            )
        initial_message, work, _status, _confirmation_update_events = await self.submit_user_message(
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
            additional_system_prompt=additional_system_prompt,
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
        commit: bool = True,
        additional_system_prompt: str | None = None,
    ) -> tuple[InternalMessage, SessionReplyWorkItem]:
        profile_id = profile.id if profile and profile.id else -1
        session = None
        if profile_id > 0:
            session = await session_crud.upsert_profile(
                db,
                session_id=session_id,
                uid=uid,
                profile_id=profile_id,
                source=source,
            )
            if session is None:
                raise ResourceNotFoundException(ERR_PROFILE_NOT_FOUND)
        else:
            session = await session_crud.get_by_session_id(db, session_id)
        show_tool_calls, expose_tool_call_content = get_tool_call_visibility(session, source)

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

        guidance_prompt = None
        if source not in {"http", "ws"}:
            guidance_prompt = await message_crud.activate_and_get_guidance_prompt(
                db,
                session_id=session_id,
                uid=uid,
            )
            message_row.guidance_prompt = guidance_prompt
            db.add(message_row)

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
                "show_tool_calls": show_tool_calls,
                "expose_tool_call_content": expose_tool_call_content,
                "language": get_current_locale(),
                "message_source": source,
                "audit_decision_response": audit_decision_response,
            }
        state = dict(work.execution_state) if isinstance(work.execution_state, dict) else {}
        state["request_ids"] = merge_work_request_ids(work, request_id=request_id)
        state["guidance_prompt"] = message_row.guidance_prompt
        cleaned_additional_system_prompt = additional_system_prompt.strip() if isinstance(additional_system_prompt, str) else ""
        if cleaned_additional_system_prompt:
            state["additional_system_prompt"] = cleaned_additional_system_prompt
        work.execution_state = state
        db.add(work)
        if commit:
            await db.commit()
            await db.refresh(message_row)
            await db.refresh(work)
        else:
            await db.flush()
        return (
            InternalMessage(
                id=message_row.id,
                role=MessageRole.USER,
                content=message_row.content,
                attachments=message_row.attachments,
                guidance_prompt=message_row.guidance_prompt,
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
