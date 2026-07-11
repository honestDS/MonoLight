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
