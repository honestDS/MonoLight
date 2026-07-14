import asyncio
import json
import time
from collections.abc import AsyncGenerator, MutableSet
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseChatAdapter
from app.core.constants import ERR_LLM_UNEXPECTED_ERROR, ERR_VALIDATION_FAILED
from app.core.crud.profile import profile_crud
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.session_reply_queue.manager import session_reply_queue_manager
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
    ) -> AsyncGenerator[dict[str, Any]]:
        if not session_id:
            raise BaseBusinessException(message=ERR_VALIDATION_FAILED, detail="session_id is required")
        try:
            await ensure_web_session_writable(
                db,
                session_id=session_id,
                uid=uid,
            )
            profile = await profile_crud.get_active(db, uid=uid)
            await ChatDispatcher.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)
            _initial_message, work = await session_reply_queue_manager.enqueue_foreground_message(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                message=message,
                attachments=attachments,
                source="http",
                stream_requested=False,
                context_summary_events_requested=True,
            )
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
        active_tasks: MutableSet[asyncio.Task] | None = None,
    ):
        if not session_id:
            raise BaseBusinessException(message=ERR_VALIDATION_FAILED, detail="session_id is required")
        try:
            await ensure_web_session_writable(
                db,
                session_id=session_id,
                uid=uid,
            )
            profile = await profile_crud.get_active(db, uid=uid)
            await ChatDispatcher.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)
            _initial_message, work = await session_reply_queue_manager.enqueue_foreground_message(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                message=message,
                attachments=attachments,
                source="http",
            )
            llm_response = await session_reply_queue_manager.wait_for_result(work.id)
            if isinstance(llm_response, dict) and _response_has_background_tasks(llm_response):
                llm_response["has_background_tasks"] = True
                llm_response["background_task_poll_interval"] = 2
            return llm_response
        except BaseBusinessException as e:
            return LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ERR, content=t(e.message, default=e.message, **e.kwargs)),
                        finish_reason=FinishReason.ERROR,
                        created_at=time.time(),
                    )
                ],
                history=[],
            ).model_dump()
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
