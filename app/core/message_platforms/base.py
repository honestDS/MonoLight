from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.crud.message_platform import message_platform_crud
from app.models.message_platform import MessagePlatform, MessagePlatformType


class MessagePlatformHandler(ABC):
    platform_type: MessagePlatformType
    sources: frozenset[str]

    async def _get_platform_by_id(self, db: AsyncSession, platform_id: int) -> MessagePlatform | None:
        return await message_platform_crud.get(db, platform_id)

    @staticmethod
    def _resolve_use_stream_dispatch(platform: MessagePlatform | None) -> bool:
        return bool(platform.use_stream_dispatch) if platform is not None else False

    @abstractmethod
    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def run(self, platform_id: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_session_event(self, uid: str, session_id: str, source: str, event: dict[str, Any]) -> bool:
        raise NotImplementedError
