from abc import ABC, abstractmethod
from typing import Any

from app.models.message_platform import MessagePlatform, MessagePlatformType


class MessagePlatformHandler(ABC):
    platform_type: MessagePlatformType
    sources: frozenset[str]
    use_stream_dispatch: bool = False

    @abstractmethod
    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        raise NotImplementedError

    @abstractmethod
    async def run(self, platform_id: int) -> None:
        raise NotImplementedError

    @abstractmethod
    async def send_session_event(self, uid: str, session_id: str, source: str, event: dict[str, Any]) -> bool:
        raise NotImplementedError
