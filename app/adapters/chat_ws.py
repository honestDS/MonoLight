import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import BaseChatAdapter
from app.core.dispatcher import ChatDispatcher


class WebSocketChatAdapter(BaseChatAdapter):
    async def chat(
        self,
        db: AsyncSession,
        message: str,
        uid: str,
        session_id: str = None
    ):
        """
        WebSocket 对话适配器实现
        目前复用 ChatDispatcher 的同步分发逻辑，后续可扩展流式输出支持
        """
        actual_session_id = session_id or str(uuid.uuid4())

        # 核心逻辑：调用调度器
        llm_response = await ChatDispatcher.dispatch(
            db=db,
            message=message,
            uid=uid,
            session_id=actual_session_id,
        )
        return llm_response

# 单例导出
ws_chat_adapter = WebSocketChatAdapter()
