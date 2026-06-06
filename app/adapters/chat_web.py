import time
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseChatAdapter
from app.core.constants import ERR_LLM_UNEXPECTED_ERROR
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException
from app.core.log import get_logger
from app.models.message import MessageRole
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse

logger = get_logger(__name__)


class WebChatAdapter(BaseChatAdapter):
    async def chat(
        self,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str,
        attachments: list[str] | None = None,
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
            )
            return llm_response
        except BaseBusinessException as e:
            return LLMResponse(choices=[LLMChoice(message=LLMChoiceMessage(role=MessageRole.ERR, content=e.message), finish_reason="error", created_at=time.time())], history=[]).model_dump()
        except Exception as e:
            logger.exception(f"Unexpected error in WebChatAdapter: {str(e)}")
            return LLMResponse(choices=[LLMChoice(message=LLMChoiceMessage(role=MessageRole.ERR, content=ERR_LLM_UNEXPECTED_ERROR), finish_reason="error", created_at=time.time())], history=[]).model_dump()


web_chat_adapter = WebChatAdapter()
