import json
import time
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.adapters.base import BaseChatAdapter
from app.core.audit.confirmation import build_confirmation_update_events
from app.core.constants import ERR_LLM_UNEXPECTED_ERROR, ERR_SESSION_ID_REQUIRED
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.profile_selection import resolve_profile_for_session
from app.core.session_reply_queue.manager import (
    build_input_queued_event,
    build_session_reply_work_event_id,
    is_submission_queued,
    session_reply_queue_manager,
)
from app.core.utils.session import ensure_web_session_writable
from app.models.message import MessageRole
from app.schemas.response import (
    FinishReason,
    LLMChoice,
    LLMChoiceMessage,
    LLMResponse,
)

logger = get_logger(__name__)


def _response_has_background_tasks(llm_response: dict[str, Any]) -> bool:
    for item in llm_response.get("history") or []:
        content = item.get("content") if isinstance(item, dict) else None
        if not isinstance(content, str):
            continue
        try:
            payload = json.loads(content)
        except Exception:
            continue
        if isinstance(payload, dict) and payload.get("status") == "queued" and payload.get("task_id"):
            return True
    return False


def _get_confirmation_audit_record_id(events: list[dict[str, Any]]) -> int | None:
    for event in events:
        audit_record_id = event.get("audit_record_id") if isinstance(event, dict) else None
        if isinstance(audit_record_id, int) and not isinstance(audit_record_id, bool) and audit_record_id > 0:
            return audit_record_id
    return None


async def _load_final_confirmation_update_events(
    db: AsyncSession,
    events: list[dict[str, Any]],
    work: Any,
) -> list[dict[str, Any]] | None:
    audit_record_id = _get_confirmation_audit_record_id(events)
    if audit_record_id is None:
        return None

    execution_state = work.execution_state if isinstance(work.execution_state, dict) else {}
    final_session_factory = async_sessionmaker(
        bind=db.bind,
        class_=type(db),
        expire_on_commit=False,
    )
    async with final_session_factory() as final_db:
        return await build_confirmation_update_events(
            final_db,
            audit_record_id=audit_record_id,
            include_tool_results=bool(execution_state.get("show_tool_calls", True)),
        )


class WebChatAdapter(BaseChatAdapter):
    async def send_session_event(self, uid: str, session_id: str, event: dict[str, Any]) -> None:
        logger.bind(uid=uid, session_id=session_id, event_type=event.get("type")).debug("Web adapter session event persisted for polling")

    async def chat_stream(
        self,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str,
        attachments: list[str] | None = None,
        request_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        if not session_id:
            raise BaseBusinessException(message=ERR_SESSION_ID_REQUIRED)
        try:
            await ensure_web_session_writable(
                db,
                session_id=session_id,
                uid=uid,
            )
            profile = await resolve_profile_for_session(db, uid=uid, session_id=session_id)
            await ChatDispatcher.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)
            _initial_message, work, submission_status, confirmation_update_events = await session_reply_queue_manager.submit_user_message(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                message=message,
                attachments=attachments,
                source="http",
                stream_requested=True,
                context_summary_events_requested=True,
                request_id=request_id,
            )
            for event in confirmation_update_events:
                yield event
            if request_id and is_submission_queued(submission_status):
                yield build_input_queued_event(session_id, request_id, work.id, submission_status)
            async for event in session_reply_queue_manager.wait_for_stream(work.id):
                if event.get("type") == "done":
                    response = event.get("response")
                    if isinstance(response, dict) and _response_has_background_tasks(response):
                        response["has_background_tasks"] = True
                        response["background_task_poll_interval"] = 2
                yield event
        except BaseBusinessException as e:
            yield {
                "type": "error",
                "message": t(e.message, default=e.message, **e.kwargs),
                "session_id": session_id,
            }
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).error(t("LOG_ADAPTER_WEB_UNEXPECTED_ERROR", error=str(e)), exc_info=True)
            yield {
                "type": "error",
                "message": t(ERR_LLM_UNEXPECTED_ERROR),
                "session_id": session_id,
            }

    async def chat(
        self,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str,
        attachments: list[str] | None = None,
        request_id: str | None = None,
    ):
        if not session_id:
            raise BaseBusinessException(message=ERR_SESSION_ID_REQUIRED)
        work = None
        try:
            await ensure_web_session_writable(
                db,
                session_id=session_id,
                uid=uid,
            )
            profile = await resolve_profile_for_session(db, uid=uid, session_id=session_id)
            await ChatDispatcher.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)
            _initial_message, work, _status, confirmation_update_events = await session_reply_queue_manager.submit_user_message(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                message=message,
                attachments=attachments,
                source="http",
                request_id=request_id,
            )
            llm_response = await session_reply_queue_manager.wait_for_result(work.id)
            if isinstance(llm_response, dict) and confirmation_update_events:
                final_confirmation_update_events = await _load_final_confirmation_update_events(db, confirmation_update_events, work)
                events_to_merge = confirmation_update_events if final_confirmation_update_events is None else final_confirmation_update_events
                existing_events = llm_response.get("session_events")
                merged_events = list(existing_events) if isinstance(existing_events, list) else ([existing_events] if existing_events is not None else [])
                event_positions = {}
                deduplicated_events = []
                for event in [*merged_events, *events_to_merge]:
                    event_id = event.get("event_id") if isinstance(event, dict) else None
                    if event_id is not None:
                        existing_position = event_positions.get(event_id)
                        if existing_position is not None:
                            deduplicated_events[existing_position] = event
                            continue
                        event_positions[event_id] = len(deduplicated_events)
                    deduplicated_events.append(event)
                llm_response["session_events"] = deduplicated_events
            if isinstance(llm_response, dict) and _response_has_background_tasks(llm_response):
                llm_response["has_background_tasks"] = True
                llm_response["background_task_poll_interval"] = 2
            return llm_response
        except BaseBusinessException as e:
            response = LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ERR, content=t(e.message, default=e.message, **e.kwargs)),
                        finish_reason=FinishReason.ERROR,
                        created_at=time.time(),
                    )
                ],
                history=[],
            ).model_dump()
            failure_data = e.data if isinstance(e.data, dict) else {}
            resolved_work_id = failure_data.get("work_id")
            resolved_event_id = failure_data.get("event_id")
            if resolved_work_id is not None and isinstance(resolved_event_id, str):
                response["work_id"] = resolved_work_id
                response["event_id"] = resolved_event_id
            elif work is not None:
                response["work_id"] = work.id
                response["event_id"] = build_session_reply_work_event_id(work, error=True)
            return response
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).error(t("LOG_ADAPTER_WEB_UNEXPECTED_ERROR", error=str(e)), exc_info=True)
            return LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ERR, content=t(ERR_LLM_UNEXPECTED_ERROR)),
                        finish_reason=FinishReason.ERROR,
                        created_at=time.time(),
                    )
                ],
                history=[],
            ).model_dump()


web_chat_adapter = WebChatAdapter()
