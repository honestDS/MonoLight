import asyncio
import time
from collections.abc import AsyncGenerator, MutableSet
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseChatAdapter
from app.core.constants import ERR_LLM_UNEXPECTED_ERROR, ERR_SESSION_ID_REQUIRED
from app.core.crud.profile import profile_crud
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.core.session_notifier import session_notifier
from app.core.session_reply_queue.manager import session_reply_queue_manager
from app.models.message import MessageRole
from app.schemas.response import (
    FinishReason,
    LLMChoice,
    LLMChoiceMessage,
    LLMResponse,
)

logger = get_logger(__name__)


class WebSocketChatAdapter(BaseChatAdapter):
    async def send_session_event(self, uid: str, session_id: str, event: dict[str, Any]) -> None:
        await session_notifier.notify(uid, session_id, event)

    async def chat(
        self,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str,
        attachments: list[str] | None = None,
        request_id: str | None = None,
        active_tasks: MutableSet[asyncio.Task] | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        if not session_id:
            raise BaseBusinessException(message=ERR_SESSION_ID_REQUIRED)
        try:
            profile = await profile_crud.get_active(db, uid=uid)
            await ChatDispatcher.validate_initial_message_before_save(db, message, uid, session_id, profile, attachments)
            _initial_message, work = await session_reply_queue_manager.enqueue_foreground_message(
                db,
                uid=uid,
                session_id=session_id,
                profile=profile,
                message=message,
                attachments=attachments,
                source="ws",
            )
            async for chunk in session_reply_queue_manager.wait_for_stream(work.id):
                if request_id and isinstance(chunk, dict):
                    chunk.setdefault("request_id", request_id)
                yield chunk
        except BaseBusinessException as e:
            yield LLMResponse(
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
            logger.bind(uid=uid, session_id=session_id).exception(t("LOG_ADAPTER_WS_UNEXPECTED_ERROR", error=str(e)))
            yield LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ERR, content=t(ERR_LLM_UNEXPECTED_ERROR)),
                        finish_reason=FinishReason.ERROR,
                        created_at=time.time(),
                    )
                ],
                history=[],
            ).model_dump()


ws_chat_adapter = WebSocketChatAdapter()
