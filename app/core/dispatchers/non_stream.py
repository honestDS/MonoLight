import asyncio
from collections.abc import Awaitable, Callable, MutableSet
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dispatchers.foreground import ForegroundDispatcherMixin
from app.core.utils.context_summary.common import ContextSummaryWorkValidityChecker
from app.models.message import InternalMessage


class NonStreamDispatcherMixin(ForegroundDispatcherMixin):
    @classmethod
    async def dispatch(
        cls,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str = "default",
        attachments: list[str] | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
        session_source: str = "http",
        persisted_initial_message: InternalMessage | None = None,
        history_before_id: int | None = None,
        frozen_user_message_ids: list[int] | None = None,
        final_message_dedupe_key: str | None = None,
        persisted_profile_id: int | None = None,
        stream_event_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        context_summary_lifecycle_callback: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        additional_user_messages_fetcher: Callable[[], Awaitable[list[InternalMessage]]] | None = None,
        execution_resume_state: dict[str, Any] | None = None,
        execution_checkpoint_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        context_summary_work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
        expose_tool_call_content: bool = True,
    ):
        return await cls._dispatch_foreground(
            db=db,
            message=message,
            uid=uid,
            session_id=session_id,
            attachments=attachments,
            active_tasks=active_tasks,
            session_source=session_source,
            persisted_initial_message=persisted_initial_message,
            history_before_id=history_before_id,
            frozen_user_message_ids=frozen_user_message_ids,
            final_message_dedupe_key=final_message_dedupe_key,
            persisted_profile_id=persisted_profile_id,
            stream_event_callback=stream_event_callback,
            context_summary_lifecycle_callback=context_summary_lifecycle_callback,
            additional_user_messages_fetcher=additional_user_messages_fetcher,
            execution_resume_state=execution_resume_state,
            execution_checkpoint_callback=execution_checkpoint_callback,
            context_summary_work_validity_checker=context_summary_work_validity_checker,
            expose_tool_call_content=expose_tool_call_content,
            dispatcher_mode="non_stream",
        )
