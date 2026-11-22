import asyncio
import os

import app.warning_filters  # noqa: F401
from app.core.background_tasks.recovery import clear_background_tasks_on_startup
from app.core.log import LogManager, get_logger
from app.core.notifications.inbox import NotificationInbox
from app.core.notifications.protocol import NotificationTopic
from app.core.notifications.runtime import create_notification_runtime
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.core.session_reply_queue.consumer import SessionReplyConsumer
from app.providers.database.bootstrap import create_database_tables
from app.providers.database.client import engine
from app.workers.lease import run_startup_cleanup, run_with_worker_lease
from app.workers.signals import install_shutdown_signal_handlers

logger = get_logger(__name__)

SESSION_REPLY_WORKER_NAME = "session_reply"


async def run_owned_session_reply_worker(
    owned_stop_event: asyncio.Event,
    *,
    inbox: NotificationInbox,
    startup_cleaned: bool = False,
) -> None:
    if not startup_cleaned:
        await run_startup_cleanup(
            owned_stop_event,
            worker_name="session_reply",
            other_worker_name="background_task",
            cleanup=clear_background_tasks_on_startup,
        )
    consumer = SessionReplyConsumer()
    try:
        consumer.start(inbox=inbox)
        logger.info("Session reply worker started")
        await owned_stop_event.wait()
    finally:
        await consumer.stop()
        logger.info("Session reply worker stopped")


async def run_session_reply_worker() -> None:
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(stop_event)
    await create_database_tables()

    notifications = await create_notification_runtime(
        SESSION_REPLY_WORKER_NAME,
        bind=engine.sync_engine,
    )
    inbox = notifications.subscribe(
        (
            NotificationTopic.SESSION_REPLY,
            NotificationTopic.PROFILE_CONFIG,
            NotificationTopic.SYSTEM_SETTINGS,
        )
    )

    async def _run_owned(owned_stop_event: asyncio.Event) -> None:
        await run_owned_session_reply_worker(owned_stop_event, inbox=inbox)

    async with notifications:
        await run_with_worker_lease(
            SESSION_REPLY_WORKER_NAME,
            stop_event,
            _run_owned,
        )


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    asyncio.run(run_session_reply_worker())


if __name__ == "__main__":
    main()
