from abc import ABC, abstractmethod

from sqlalchemy.ext.asyncio import AsyncSession


class BaseChatAdapter(ABC):
    @abstractmethod
    async def chat(
        self,
        db: AsyncSession,
        message: str,
        uid: str,
        session_id: str = None
    ):
        pass
