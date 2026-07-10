import asyncio
import os

import app.warning_filters  # noqa: F401
from app.core.background_tasks.scheduler import scheduled_task_scheduler
from app.core.log import LogManager, get_logger
from app.core.message_platforms.manager import message_platform_polling_manager
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.providers.database.bootstrap import create_database_tables
from app.workers.lease import run_with_worker_lease
from app.workers.signals import install_shutdown_signal_handlers

logger = get_logger(__name__)

MESSAGE_PLATFORM_WORKER_NAME = "message_platform"


async def run_message_platform_worker() -> None:
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(stop_event)

    await create_database_tables()

    async def run_owned_worker(owned_stop_event: asyncio.Event) -> None:
        message_platform_polling_manager.start()
        scheduled_task_scheduler.start()
        logger.info("Background worker started")
        try:
            await owned_stop_event.wait()
        finally:
            await scheduled_task_scheduler.stop()
            await message_platform_polling_manager.stop()
            logger.info("Background worker stopped")

    await run_with_worker_lease(
        MESSAGE_PLATFORM_WORKER_NAME,
        stop_event,
        run_owned_worker,
    )


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    asyncio.run(run_message_platform_worker())


if __name__ == "__main__":
    main()
