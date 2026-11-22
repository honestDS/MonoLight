import asyncio
import os

import app.warning_filters  # noqa: F401
from app.core.background_tasks.scheduler import ScheduledTaskScheduler
from app.core.log import LogManager, get_logger
from app.core.message_platforms.manager import MessagePlatformPollingManager
from app.core.notifications.inbox import NotificationInbox
from app.core.notifications.protocol import NotificationTopic
from app.core.notifications.runtime import create_notification_runtime
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.providers.database import engine
from app.providers.database.bootstrap import create_database_tables
from app.workers.lease import run_with_worker_lease
from app.workers.signals import install_shutdown_signal_handlers

logger = get_logger(__name__)

MESSAGE_PLATFORM_WORKER_NAME = "message_platform"


async def run_owned_message_platform_worker(
    owned_stop_event: asyncio.Event,
    *,
    config_inbox: NotificationInbox,
    outbox_inbox: NotificationInbox,
    scheduled_inbox: NotificationInbox,
) -> None:
    manager = MessagePlatformPollingManager()
    scheduler = ScheduledTaskScheduler()
    manager.start(config_inbox=config_inbox, outbox_inbox=outbox_inbox)
    scheduler.start(inbox=scheduled_inbox)
    logger.info("Background worker started")
    try:
        await owned_stop_event.wait()
    finally:
        await scheduler.stop()
        await manager.stop()
        logger.info("Background worker stopped")


async def run_message_platform_worker() -> None:
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(stop_event)

    await create_database_tables()

    notifications = await create_notification_runtime(
        MESSAGE_PLATFORM_WORKER_NAME,
        bind=engine.sync_engine,
    )
    config_inbox = notifications.subscribe((NotificationTopic.MESSAGE_PLATFORM,))
    outbox_inbox = notifications.subscribe((NotificationTopic.OUTBOX,))
    scheduled_inbox = notifications.subscribe((NotificationTopic.SCHEDULED_TASK,))

    async with notifications:

        async def _run_owned(owned_stop_event: asyncio.Event) -> None:
            await run_owned_message_platform_worker(
                owned_stop_event,
                config_inbox=config_inbox,
                outbox_inbox=outbox_inbox,
                scheduled_inbox=scheduled_inbox,
            )

        await run_with_worker_lease(
            MESSAGE_PLATFORM_WORKER_NAME,
            stop_event,
            _run_owned,
        )


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    asyncio.run(run_message_platform_worker())


if __name__ == "__main__":
    main()
