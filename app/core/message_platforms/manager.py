import asyncio
import uuid
from typing import Any

from app.core.constants import (
    ERR_MESSAGE_PLATFORM_EVENT_NOT_SENT,
    NOTIFICATION_ERROR_RETRY_SECONDS,
    NOTIFICATION_RECONCILE_INTERVAL_SECONDS,
)
from app.core.crud.message_platform.outbox import message_platform_outbox_crud
from app.core.crud.message_platform.platform import message_platform_crud
from app.core.i18n import t
from app.core.log import get_logger
from app.core.message_platforms.base import MessagePlatformHandler
from app.core.message_platforms.weixin_openclaw import weixin_openclaw_platform_handler
from app.core.notifications.inbox import NotificationInbox
from app.core.notifications.protocol import NotificationTopic
from app.core.utils.time import get_local_time
from app.models.message_platform import MessagePlatformType
from app.models.message_platform_outbox import MessagePlatformOutbox, MessagePlatformOutboxStatus
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

OUTBOX_FETCH_LIMIT = 20
OUTBOX_CLEANUP_INTERVAL_SECONDS = 24 * 60 * 60
OUTBOX_DELIVERY_TIMEOUT_SECONDS = 300


class MessagePlatformPollingManager:
    def __init__(self, handlers: tuple[MessagePlatformHandler, ...] | None = None) -> None:
        registered_handlers = handlers if handlers is not None else (weixin_openclaw_platform_handler,)
        self._handlers = {handler.platform_type: handler for handler in registered_handlers}
        self._source_handlers = {source: handler for handler in registered_handlers for source in handler.sources}
        self._platform_tasks: dict[int, asyncio.Task] = {}
        self._platform_configs: dict[int, dict[str, Any]] = {}
        self._supervisor_task: asyncio.Task | None = None
        self._outbox_task: asyncio.Task | None = None
        self._config_inbox = NotificationInbox()
        self._outbox_inbox = NotificationInbox()
        self._stopping = False
        self._reload_lock = asyncio.Lock()
        self._worker_id = uuid.uuid4().hex

    @property
    def is_running(self) -> bool:
        return self._supervisor_task is not None and not self._supervisor_task.done() and self._outbox_task is not None and not self._outbox_task.done()

    def start(
        self,
        *,
        config_inbox: NotificationInbox | None = None,
        outbox_inbox: NotificationInbox | None = None,
    ) -> None:
        self._stopping = False
        if self._supervisor_task is None or self._supervisor_task.done():
            self._config_inbox = config_inbox if config_inbox is not None else NotificationInbox()
            self._config_inbox.wake()
            self._supervisor_task = asyncio.create_task(self._supervisor_loop())
        if self._outbox_task is None or self._outbox_task.done():
            self._outbox_inbox = outbox_inbox if outbox_inbox is not None else NotificationInbox()
            self._outbox_inbox.wake()
            self._outbox_task = asyncio.create_task(self._outbox_loop())

    async def stop(self) -> None:
        self._stopping = True
        tasks = list(self._platform_tasks.values())
        self._platform_tasks.clear()
        self._platform_configs.clear()
        for task in (self._supervisor_task, self._outbox_task):
            if task is not None:
                tasks.append(task)
        self._supervisor_task = None
        self._outbox_task = None

        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def reload(
        self,
        *,
        platform_ids: set[int] | None = None,
        restart_ids: set[int] | None = None,
    ) -> None:
        async with self._reload_lock:
            await self._sync_platform_tasks(platform_ids=platform_ids, restart_ids=restart_ids)

    async def restart_platform(self, platform_id: int) -> None:
        await self.reload(platform_ids={platform_id}, restart_ids={platform_id})

    async def _supervisor_loop(self) -> None:
        next_reconcile_at = 0.0
        while not self._stopping:
            try:
                loop = asyncio.get_running_loop()
                now = loop.time()
                batch = self._config_inbox.take()
                full_scan = batch.full_scan or now >= next_reconcile_at
                if full_scan:
                    next_reconcile_at = now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS
                    platform_ids = None
                else:
                    platform_ids = batch.resource_ids(NotificationTopic.MESSAGE_PLATFORM, "message_platform")

                if platform_ids is None or platform_ids:
                    await self.reload(platform_ids=platform_ids)
                await self._config_inbox.wait(max(0.0, next_reconcile_at - loop.time()))
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(t("LOG_MESSAGE_PLATFORM_REFRESH_FAILED"))
                self._config_inbox.wake()
                await asyncio.sleep(NOTIFICATION_ERROR_RETRY_SECONDS)

    async def _sync_platform_tasks(
        self,
        *,
        platform_ids: set[int] | None = None,
        restart_ids: set[int] | None = None,
    ) -> None:
        async with AsyncSessionLocal() as db:
            platforms = await message_platform_crud.list_enabled(db, platform_ids=platform_ids)

        runnable_platforms: dict[int, MessagePlatformHandler] = {}
        platform_configs: dict[int, dict[str, Any]] = {}
        for platform in platforms:
            if platform.id is None:
                continue
            handler = self._handlers.get(platform.platform_type)
            if handler is None or not handler.is_pollable(platform):
                continue
            runnable_platforms[platform.id] = handler
            platform_config = platform.model_dump(include={"platform_type", "uid", "profile_id", "config", "language", "account_id", "use_stream_dispatch"})
            platform_config["recovery_request_id"] = (platform.state or {}).get("recovery_request_id")
            platform_configs[platform.id] = platform_config

        tasks_to_wait: list[asyncio.Task] = []
        for platform_id, task in list(self._platform_tasks.items()):
            if platform_ids is not None and platform_id not in platform_ids:
                continue
            should_stop = platform_id not in runnable_platforms or task.done() or self._platform_configs.get(platform_id) != platform_configs.get(platform_id) or (restart_ids is not None and platform_id in restart_ids)
            if not should_stop:
                continue
            self._platform_tasks.pop(platform_id, None)
            self._platform_configs.pop(platform_id, None)
            if task.done():
                continue
            task.cancel()
            tasks_to_wait.append(task)
        if tasks_to_wait:
            await asyncio.gather(*tasks_to_wait, return_exceptions=True)

        for platform_id, handler in runnable_platforms.items():
            if platform_id in self._platform_tasks:
                continue
            self._platform_configs[platform_id] = platform_configs[platform_id]
            self._platform_tasks[platform_id] = asyncio.create_task(self._run_platform(handler, platform_id))

    async def _run_platform(self, handler: MessagePlatformHandler, platform_id: int) -> None:
        try:
            await handler.run(platform_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception(t("LOG_MESSAGE_PLATFORM_REFRESH_FAILED"))
        await asyncio.sleep(NOTIFICATION_ERROR_RETRY_SECONDS)
        self._config_inbox.wake()

    async def _outbox_loop(self) -> None:
        next_cleanup_at = 0.0
        next_reconcile_at = 0.0
        next_due_at = 0.0
        while not self._stopping:
            try:
                loop = asyncio.get_running_loop()
                now = loop.time()
                batch = self._outbox_inbox.take()
                if now >= next_cleanup_at:
                    await self.cleanup_outbox()
                    next_cleanup_at = loop.time() + OUTBOX_CLEANUP_INTERVAL_SECONDS

                full_scan = batch.full_scan or now >= next_reconcile_at or now >= next_due_at
                if full_scan:
                    next_reconcile_at = now + NOTIFICATION_RECONCILE_INTERVAL_SECONDS
                    item_ids = None
                else:
                    item_ids = batch.resource_ids(NotificationTopic.OUTBOX, "message_platform_outbox")
                if item_ids is None or item_ids:
                    await self.process_outbox_batch(item_ids=item_ids)

                async with AsyncSessionLocal() as db:
                    next_attempt_at = await message_platform_outbox_crud.get_next_attempt_at(db)
                local_time = get_local_time()
                if next_attempt_at is not None and next_attempt_at.tzinfo is None:
                    next_attempt_at = next_attempt_at.replace(tzinfo=local_time.tzinfo)
                next_due_at = float("inf")
                if next_attempt_at is not None:
                    next_due_at = loop.time() + max(0.0, (next_attempt_at - local_time).total_seconds())

                wait = max(0.0, min(next_cleanup_at, next_reconcile_at, next_due_at) - loop.time())
                await self._outbox_inbox.wait(wait)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(t("LOG_MESSAGE_PLATFORM_OUTBOX_POLL_FAILED"))
                self._outbox_inbox.wake()
                await asyncio.sleep(NOTIFICATION_ERROR_RETRY_SECONDS)

    async def cleanup_outbox(self) -> int:
        async with AsyncSessionLocal() as db:
            return await message_platform_outbox_crud.cleanup_terminal_items(db)

    async def process_outbox_batch(
        self,
        *,
        limit: int = OUTBOX_FETCH_LIMIT,
        item_ids: set[int] | None = None,
    ) -> int:
        async with AsyncSessionLocal() as db:
            candidate_ids = await message_platform_outbox_crud.list_claimable_ids(db, limit=limit, item_ids=item_ids)

        processed_count = 0
        for item_id in candidate_ids:
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
