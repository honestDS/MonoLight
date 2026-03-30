import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseChatAdapter
from app.core.dispatcher import ChatDispatcher


class WebChatAdapter(BaseChatAdapter):
    async def chat(
        self,
        db: AsyncSession,
        message: str,
        uid: str,
        session_id: str = None
    ):
        # 逻辑迁移：SessionID 生成与 Dispatcher 调用
        actual_session_id = session_id or str(uuid.uuid4())

        llm_response = await ChatDispatcher.dispatch(
            db=db,
            message=message,
            uid=uid,
            session_id=actual_session_id,
        )
        return llm_response

# 单例模式导出
web_chat_adapter = WebChatAdapter()
