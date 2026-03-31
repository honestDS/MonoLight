import uuid
import time
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.message import MessageRole

from app.adapters.base import BaseChatAdapter
from app.core.dispatcher import ChatDispatcher
from app.core.log import get_logger
from app.core.constants import ERR_LLM_UNEXPECTED_ERROR

from app.core.exceptions import (
    BaseBusinessException
)

from app.schemas.response import (
    LLMResponse,
    LLMChoice,
    LLMChoiceMessage
)

logger = get_logger(__name__)

class WebSocketChatAdapter(BaseChatAdapter):
    async def chat(
        self,
        db: AsyncSession,
        message: str,
        uid: str,
        session_id: str = None
    ):
        actual_session_id = session_id or str(uuid.uuid4())
        try:
            llm_response = await ChatDispatcher.dispatch(
                db=db,
                message=message,
                uid=uid,
                session_id=actual_session_id,
            )
            return llm_response
        except BaseBusinessException as e:
            return LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ERR, content=e.message),
                        finish_reason=True,
                        created_at=time.time()
                    )
                ],
                history=[]
            ).model_dump()
        except Exception as e:
            logger.exception(f"Unexpected error in WebSocketChatAdapter: {str(e)}")
            return LLMResponse(
                choices=[
                    LLMChoice(
                        message=LLMChoiceMessage(role=MessageRole.ERR, content=ERR_LLM_UNEXPECTED_ERROR),
                        finish_reason=True,
                        created_at=time.time()
                    )
                ],
                history=[]
            ).model_dump()

ws_chat_adapter = WebSocketChatAdapter()
