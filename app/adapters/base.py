from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncSession


class BaseChatAdapter(ABC):
    @abstractmethod
    async def chat(self, db: AsyncSession, message: str, uid: str, session_id: str = None):
        pass

    async def release_session_lock(self, session_id: str):
        """释放会话锁"""
        if session_id:
            from app.core.crud.active_session import active_session_crud
            from app.providers.database import AsyncSessionLocal

            async with AsyncSessionLocal() as db:
                await active_session_crud.release_lock(db, session_id)
