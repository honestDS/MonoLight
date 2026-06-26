from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession


class BaseChatAdapter(ABC):
    @abstractmethod
    async def chat(self, db: AsyncSession, message: str, uid: str, session_id: str = None):
        pass

    @abstractmethod
    async def send_session_event(self, uid: str, session_id: str, event: dict[str, Any]) -> None:
        pass

    async def release_session_lock(self, session_id: str):
        """释放会话锁"""
        if session_id:
            from app.core.crud.active_session import active_session_crud
            from app.providers.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                await active_session_crud.release_lock(db, session_id)
