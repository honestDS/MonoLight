from typing import Any

import pytest

from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.manager import MessagePlatformPollingManager
from app.models.message_platform import MessagePlatform, MessagePlatformType


class StubMessagePlatformHandler(MessagePlatformHandler):
    platform_type = MessagePlatformType.WEIXIN_OPENCLAW
    sources = frozenset({"stub-source"})

    def is_pollable(self, platform: MessagePlatform | None) -> bool:
        return platform is not None

    async def run(self, platform_id: int) -> None:
        return None

    async def send_session_event(self, uid: str, session_id: str, source: str, event: dict[str, Any]) -> bool:
        return source in self.sources


def test_manager_registers_handler_by_platform_type():
    handler = StubMessagePlatformHandler()
    manager = MessagePlatformPollingManager((handler,))

    assert manager.get_handler(MessagePlatformType.WEIXIN_OPENCLAW) is handler


@pytest.mark.asyncio
async def test_manager_routes_session_event_by_source():
    manager = MessagePlatformPollingManager((StubMessagePlatformHandler(),))

    assert await manager.send_session_event("uid", "session", "stub-source", {"type": "completed"}) is True
    assert await manager.send_session_event("uid", "session", "unknown-source", {"type": "completed"}) is False


def test_manager_accepts_empty_handler_registry():
    manager = MessagePlatformPollingManager(())

    assert manager.get_handler(MessagePlatformType.WEIXIN_OPENCLAW) is None
