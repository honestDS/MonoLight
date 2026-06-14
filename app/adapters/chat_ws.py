import time
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseChatAdapter
from app.core.constants import ERR_LLM_UNEXPECTED_ERROR
from app.core.dispatcher import ChatDispatcher
from app.core.exceptions import BaseBusinessException
from app.core.i18n import t
from app.core.log import get_logger
from app.models.message import MessageRole
from app.schemas.response import LLMChoice, LLMChoiceMessage, LLMResponse

logger = get_logger(__name__)


class WebSocketChatAdapter(BaseChatAdapter):
    async def chat(
        self,
        db: AsyncSession,
        message: str | list[dict[str, Any]],
        uid: str,
        session_id: str,
        attachments: list[str] | None = None,
        request_id: str | None = None,
    ) -> AsyncGenerator[dict[str, Any]]:
        if not session_id:
            raise BaseBusinessException(message="session_id is required")
        try:
            async for chunk in ChatDispatcher.dispatch_stream(
                db=db,
                message=message,
                uid=uid,
                session_id=session_id,
                attachments=attachments,
                request_id=request_id,
            ):
                yield chunk
        except BaseBusinessException as e:
            yield LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ERR, content=t(e.message, **e.kwargs)),
                        finish_reason=True,
                        created_at=time.time(),
                    )
                ],
                history=[],
            ).model_dump()
        except Exception as e:
            logger.bind(uid=uid, session_id=session_id).error(f"Unexpected error in WebSocketChatAdapter: {str(e)}", exc_info=True)
            yield LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ERR, content=t(ERR_LLM_UNEXPECTED_ERROR)),
                        finish_reason=True,
                        created_at=time.time(),
                    )
                ],
                history=[],
            ).model_dump()


ws_chat_adapter = WebSocketChatAdapter()
