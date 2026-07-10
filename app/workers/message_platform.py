import asyncio
import os
import signal

import app.warning_filters  # noqa: F401
from app.core.background_tasks.scheduler import scheduled_task_scheduler
from app.core.log import LogManager, get_logger
from app.core.message_platforms.manager import message_platform_polling_manager
from app.core.message_platforms.process_lock import ProcessFileLock
from app.core.paths import DEFAULT_LOG_FILE_PATH, MESSAGE_PLATFORM_WORKER_LOCK_PATH
from app.providers.database.bootstrap import create_database_tables

logger = get_logger(__name__)


async def run_message_platform_worker() -> None:
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signal_name in ("SIGINT", "SIGTERM"):
        shutdown_signal = getattr(signal, signal_name, None)
        if shutdown_signal is None:
            continue
        try:
            loop.add_signal_handler(shutdown_signal, stop_event.set)
        except NotImplementedError:
            continue

    await create_database_tables()

    message_platform_polling_manager.start()
    scheduled_task_scheduler.start()
    logger.info("Background worker started")
    try:
        await stop_event.wait()
    finally:
        await scheduled_task_scheduler.stop()
        await message_platform_polling_manager.stop()
        logger.info("Background worker stopped")


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    with ProcessFileLock(MESSAGE_PLATFORM_WORKER_LOCK_PATH):
        asyncio.run(run_message_platform_worker())


if __name__ == "__main__":
    main()
