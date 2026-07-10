import asyncio
from contextlib import suppress
from typing import Any

from app.core.crud.message_platform import message_platform_crud
from app.core.log import get_logger
from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.weixin_openclaw import weixin_openclaw_platform_handler
from app.models.message_platform import MessagePlatformType
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

REFRESH_INTERVAL_SECONDS = 10


class MessagePlatformPollingManager:
    def __init__(self, handlers: tuple[MessagePlatformHandler, ...] | None = None) -> None:
        registered_handlers = handlers if handlers is not None else (weixin_openclaw_platform_handler,)
        self._handlers = {handler.platform_type: handler for handler in registered_handlers}
        self._source_handlers = {source: handler for handler in registered_handlers for source in handler.sources}
        self._refresh_task: asyncio.Task | None = None
        self._tasks: dict[int, asyncio.Task] = {}
        self._stopping = False
        self._lock = asyncio.Lock()

    def start(self) -> None:
        if self._refresh_task is None or self._refresh_task.done():
            self._stopping = False
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._tasks.values())
        self._tasks.clear()
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            tasks.append(self._refresh_task)
            self._refresh_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

    async def reload(self) -> None:
        async with self._lock:
            await self._sync_enabled_tasks()

    async def restart_platform(self, platform_id: int) -> None:
        task = self._tasks.pop(platform_id, None)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.reload()

    async def _refresh_loop(self) -> None:
        while not self._stopping:
            try:
                await self.reload()
            except Exception:
                logger.exception("message platform task refresh failed")
            await asyncio.sleep(REFRESH_INTERVAL_SECONDS)

    async def _sync_enabled_tasks(self) -> None:
        async with AsyncSessionLocal() as db:
            platforms = await message_platform_crud.list_enabled(db)

        runnable_platforms: dict[int, MessagePlatformHandler] = {}
        for platform in platforms:
            if platform.id is None:
                continue
            handler = self._handlers.get(platform.platform_type)
            if handler is not None and handler.is_pollable(platform):
                runnable_platforms[platform.id] = handler

        for platform_id, task in list(self._tasks.items()):
            if platform_id not in runnable_platforms or task.done():
                self._tasks.pop(platform_id, None)
                if not task.done():
                    task.cancel()

        for platform_id, handler in runnable_platforms.items():
            if platform_id not in self._tasks:
                self._tasks[platform_id] = asyncio.create_task(handler.run(platform_id))

    async def send_session_event(self, uid: str, session_id: str, source: str, event: dict[str, Any]) -> bool:
        handler = self._source_handlers.get(source)
        if handler is None:
            return False
        return await handler.send_session_event(uid, session_id, source, event)

    def get_handler(self, platform_type: MessagePlatformType) -> MessagePlatformHandler | None:
        return self._handlers.get(platform_type)


message_platform_polling_manager = MessagePlatformPollingManager()
