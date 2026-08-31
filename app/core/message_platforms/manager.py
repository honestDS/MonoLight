import asyncio
import uuid
from contextlib import suppress
from typing import Any

from app.core.constants import ERR_MESSAGE_PLATFORM_EVENT_NOT_SENT
from app.core.crud.message_platform.outbox import message_platform_outbox_crud
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.weixin_openclaw import weixin_openclaw_platform_handler
from app.models.message_platform import MessagePlatformType
from app.models.message_platform_outbox import MessagePlatformOutbox, MessagePlatformOutboxStatus
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

PLATFORM_REFRESH_INTERVAL_SECONDS = 10
OUTBOX_POLL_INTERVAL_SECONDS = 1
OUTBOX_FETCH_LIMIT = 20
OUTBOX_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
OUTBOX_DELIVERY_TIMEOUT_SECONDS = 300


class MessagePlatformPollingManager:
    def __init__(self, handlers: tuple[MessagePlatformHandler, ...] | None = None) -> None:
        registered_handlers = handlers if handlers is not None else (weixin_openclaw_platform_handler,)
        self._handlers = {handler.platform_type: handler for handler in registered_handlers}
        self._source_handlers = {source: handler for handler in registered_handlers for source in handler.sources}
        self._platform_tasks: dict[int, asyncio.Task] = {}
        self._supervisor_task: asyncio.Task | None = None
        self._outbox_task: asyncio.Task | None = None
        self._stopping = False
        self._reload_lock = asyncio.Lock()
        self._worker_id = uuid.uuid4().hex

    @property
    def is_running(self) -> bool:
        return self._supervisor_task is not None and not self._supervisor_task.done() and self._outbox_task is not None and not self._outbox_task.done()

    def start(self) -> None:
        self._stopping = False
        if self._supervisor_task is None or self._supervisor_task.done():
            self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        if self._outbox_task is None or self._outbox_task.done():
            self._outbox_task = asyncio.create_task(self._outbox_loop())

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._platform_tasks.values())
        self._platform_tasks.clear()
        for task in (self._supervisor_task, self._outbox_task):
            if task is not None:
                tasks.append(task)
        self._supervisor_task = None
        self._outbox_task = None

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reload(self) -> None:
        async with self._reload_lock:
            await self._sync_platform_tasks()

    async def restart_platform(self, platform_id: int) -> None:
        task = self._platform_tasks.pop(platform_id, None)
        if task is not None:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        await self.reload()

    async def _supervisor_loop(self) -> None:
        while not self._stopping:
            try:
                await self.reload()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(t("LOG_MESSAGE_PLATFORM_REFRESH_FAILED"))
            await self._wait_or_stop(PLATFORM_REFRESH_INTERVAL_SECONDS)

    async def _sync_platform_tasks(self) -> None:
        async with AsyncSessionLocal() as db:
            platforms = await message_platform_crud.list_enabled(db)

        runnable_platforms: dict[int, MessagePlatformHandler] = {}
        for platform in platforms:
            if platform.id is None:
                continue
            handler = self._handlers.get(platform.platform_type)
            if handler is not None and handler.is_pollable(platform):
                runnable_platforms[platform.id] = handler

        tasks_to_wait: list[asyncio.Task] = []
        for platform_id, task in list(self._platform_tasks.items()):
            if platform_id not in runnable_platforms or task.done():
                self._platform_tasks.pop(platform_id, None)
                if not task.done():
                    task.cancel()
                    tasks_to_wait.append(task)
        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)

        for platform_id, handler in runnable_platforms.items():
            if platform_id not in self._platform_tasks:
                self._platform_tasks[platform_id] = asyncio.create_task(handler.run(platform_id))

    async def _outbox_loop(self) -> None:
        next_cleanup_at = 0.0
        while not self._stopping:
            processed_count = 0
            try:
                loop_time = asyncio.get_running_loop().time()
                if loop_time >= next_cleanup_at:
                    await self.cleanup_outbox()
                    next_cleanup_at = loop_time + OUTBOX_CLEANUP_INTERVAL_SECONDS
                processed_count = await self.process_outbox_batch()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(t("LOG_MESSAGE_PLATFORM_OUTBOX_POLL_FAILED"))
            if processed_count == 0:
                await self._wait_or_stop(OUTBOX_POLL_INTERVAL_SECONDS)

    async def cleanup_outbox(self) -> int:
        async with AsyncSessionLocal() as db:
            return await message_platform_outbox_crud.cleanup_terminal_items(db)

    async def process_outbox_batch(self, *, limit: int = OUTBOX_FETCH_LIMIT) -> int:
        async with AsyncSessionLocal() as db:
            item_ids = await message_platform_outbox_crud.list_claimable_ids(db, limit=limit)

        processed_count = 0
        for item_id in item_ids:
            if self._stopping:
                break
            async with AsyncSessionLocal() as db:
                item = await message_platform_outbox_crud.try_claim(db, item_id=item_id, worker_id=self._worker_id)
            if item is None:
                continue
            processed_count += 1
            await self._deliver_outbox_item(item)
        return processed_count

    async def _deliver_outbox_item(self, item: MessagePlatformOutbox) -> None:
        if item.id is None:
            return
        try:
            sent = await asyncio.wait_for(
                self.send_session_event(item.uid, item.session_id, item.source, item.event),
                timeout=OUTBOX_DELIVERY_TIMEOUT_SECONDS,
            )
            if not sent:
                raise RuntimeError(t(ERR_MESSAGE_PLATFORM_EVENT_NOT_SENT))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            async with AsyncSessionLocal() as db:
                next_status = await message_platform_outbox_crud.mark_retry_or_failed(
                    db,
                    item_id=item.id,
                    worker_id=self._worker_id,
                    attempt_count=item.attempt_count,
                    error=str(exc),
                )
            log = logger.bind(
                outbox_id=item.id,
                uid=item.uid,
                session_id=item.session_id,
                session_source=item.source,
                attempt_count=item.attempt_count,
                error=str(exc),
            )
            if next_status == MessagePlatformOutboxStatus.FAILED:
                log.error(t("LOG_MESSAGE_PLATFORM_OUTBOX_FAILED"))
            elif next_status == MessagePlatformOutboxStatus.PENDING:
                log.warning(t("LOG_MESSAGE_PLATFORM_OUTBOX_RETRY"))
            return

        async with AsyncSessionLocal() as db:
            marked = await message_platform_outbox_crud.mark_sent(db, item_id=item.id, worker_id=self._worker_id)
        if marked:
            logger.bind(
                outbox_id=item.id,
                uid=item.uid,
                session_id=item.session_id,
                session_source=item.source,
                attempt_count=item.attempt_count,
            ).info(t("LOG_MESSAGE_PLATFORM_OUTBOX_SENT"))

    async def send_session_event(self, uid: str, session_id: str, source: str, event: dict[str, Any]) -> bool:
        handler = self._source_handlers.get(source)
        if handler is None:
            return False
        return await handler.send_session_event(uid, session_id, source, event)

    def get_handler(self, platform_type: MessagePlatformType) -> MessagePlatformHandler | None:
        return self._handlers.get(platform_type)

    async def _wait_or_stop(self, timeout_seconds: float) -> None:
        if self._stopping:
            return
        try:
            await asyncio.sleep(timeout_seconds)
        except asyncio.CancelledError:
            raise


message_platform_polling_manager = MessagePlatformPollingManager()
