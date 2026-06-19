import asyncio
import time
from collections.abc import MutableSet
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseChatAdapter
from app.core.constants import ERR_LLM_UNEXPECTED_ERROR
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.models.message import MessageRole
from app.schemas.response import (
    FinishReason,
    LLMChoice,
    LLMChoiceMessage,
    LLMResponse,
)

logger = get_logger(__name__)


class WebChatAdapter(BaseChatAdapter):
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
            raise BaseBusinessException(message="session_id is required")
        try:
            llm_response = await ChatDispatcher.dispatch(
                db=db,
                message=message,
                uid=uid,
                session_id=session_id,
                attachments=attachments,
                active_tasks=active_tasks,
            )
            return llm_response
        except BaseBusinessException as e:
            return LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ERR, content=t(e.message, **e.kwargs)),
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
