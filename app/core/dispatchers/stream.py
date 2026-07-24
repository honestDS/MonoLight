import asyncio
from collections.abc import AsyncGenerator, Awaitable, Callable, MutableSet
from functools import partial
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.dispatchers.interactive import InteractiveDispatcherMixin
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.utils.context_summary.common import ContextSummaryWorkValidityChecker
from app.core.utils.dispatcher.user_input_batch import UserInputBatch
from app.models.message import InternalMessage

logger = get_logger(__name__)


class StreamDispatcherMixin(InteractiveDispatcherMixin):
    @staticmethod
    async def _emit_event(
        event: dict[str, Any],
        *,
        event_queue: asyncio.Queue[tuple[str, Any]],
        session_id: str,
        request_id: str | None,
        response_state: dict[str, str | None],
    ) -> None:
        normalized_event = {
            **event,
            "session_id": session_id,
        }
        if request_id is not None:
            normalized_event.setdefault("request_id", request_id)
        response_id = normalized_event.get("response_id")
        if isinstance(response_id, str):
            response_state["latest_response_id"] = response_id
        await event_queue.put(("event", normalized_event))

    @classmethod
    async def _run_dispatch(
        cls,
        *,
        event_queue: asyncio.Queue[tuple[str, Any]],
        dispatch_kwargs: dict[str, Any],
        uid: str,
        session_id: str,
    ) -> None:
        try:
            response = await cls._dispatch_interactive(**dispatch_kwargs)
        except BaseBusinessException as exc:
            await event_queue.put(("error", t(exc.message, default=exc.message, **exc.kwargs)))
        except Exception as exc:
            logger.bind(uid=uid, session_id=session_id).error(t("LOG_DISPATCHER_STREAM_ERROR"), exc_info=True)
            await event_queue.put(("error", t(str(exc), default=str(exc))))
        else:
            await event_queue.put(("done", response))

    @staticmethod
    def _build_task_start_event(session_id: str, request_id: str | None) -> dict[str, Any]:
        event = {
            "type": "task_start",
            "session_id": session_id,
        }
        if request_id is not None:
            event["request_id"] = request_id
        return event

    @staticmethod
    def _build_error_event(message: Any, session_id: str, request_id: str | None) -> dict[str, Any]:
        event = {
            "type": "error",
            "message": message,
            "session_id": session_id,
        }
        if request_id is not None:
            event["request_id"] = request_id
        return event

    @staticmethod
    def _build_done_event(
        response: dict[str, Any],
        *,
        session_id: str,
        request_id: str | None,
        response_id: str | None,
    ) -> dict[str, Any]:
        event = {
            "type": "done",
            "session_id": session_id,
            "history": response.get("history", []),
            "files": response.get("files"),
            "response": response,
            "response_id": response_id,
        }
        if request_id is not None:
            event["request_id"] = request_id
        return event

    @classmethod
    async def dispatch_stream(
        cls,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str = "default",
        attachments: list[str] | None = None,
        request_id: str | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
        session_source: str = "ws",
        persisted_initial_message: InternalMessage | None = None,
        history_before_id: int | None = None,
        frozen_user_message_ids: list[int] | None = None,
        final_message_dedupe_key: str | None = None,
        persisted_profile_id: int | None = None,
        context_summary_lifecycle_callback: Callable[[dict[str, object]], Awaitable[None]] | None = None,
        context_summary_events_requested: bool = False,
        additional_user_messages_fetcher: Callable[[], Awaitable[UserInputBatch | list[InternalMessage] | None]] | None = None,
        execution_resume_state: dict[str, Any] | None = None,
        execution_checkpoint_callback: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
        context_summary_work_validity_checker: ContextSummaryWorkValidityChecker | None = None,
        expose_tool_call_content: bool = True,
    ) -> AsyncGenerator[dict[str, Any]]:
        event_queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue(maxsize=100)
        response_state: dict[str, str | None] = {"latest_response_id": None}
        emit_event = partial(
            cls._emit_event,
            event_queue=event_queue,
            session_id=session_id,
            request_id=request_id,
            response_state=response_state,
        )
        dispatch_kwargs = {
            "db": db,
            "message": message,
            "uid": uid,
            "session_id": session_id,
            "attachments": attachments,
            "active_tasks": active_tasks,
            "session_source": session_source,
            "persisted_initial_message": persisted_initial_message,
            "history_before_id": history_before_id,
            "frozen_user_message_ids": frozen_user_message_ids,
            "final_message_dedupe_key": final_message_dedupe_key,
            "persisted_profile_id": persisted_profile_id,
            "stream_event_callback": emit_event,
            "context_summary_lifecycle_callback": context_summary_lifecycle_callback or (emit_event if context_summary_events_requested else None),
            "additional_user_messages_fetcher": additional_user_messages_fetcher,
            "execution_resume_state": execution_resume_state,
            "execution_checkpoint_callback": execution_checkpoint_callback,
            "context_summary_work_validity_checker": context_summary_work_validity_checker,
            "expose_tool_call_content": expose_tool_call_content,
            "dispatcher_mode": "stream",
        }
        dispatch_task = asyncio.create_task(
            cls._run_dispatch(
                event_queue=event_queue,
                dispatch_kwargs=dispatch_kwargs,
                uid=uid,
                session_id=session_id,
            )
        )
        if active_tasks is not None:
            active_tasks.add(dispatch_task)
        try:
            yield cls._build_task_start_event(session_id, request_id)

            while True:
                item_type, payload = await event_queue.get()
                if item_type == "event":
                    yield payload
                    continue
                if item_type == "error":
                    yield cls._build_error_event(payload, session_id, request_id)
                    break

                yield cls._build_done_event(
                    payload,
                    session_id=session_id,
                    request_id=request_id,
                    response_id=response_state["latest_response_id"],
                )
                break
        finally:
            if not dispatch_task.done():
                dispatch_task.cancel()
            await asyncio.gather(dispatch_task, return_exceptions=True)
            if active_tasks is not None:
                active_tasks.discard(dispatch_task)
