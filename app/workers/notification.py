import asyncio
import os
from pathlib import Path

import app.warning_filters  # noqa: F401
from app.core.constants import NOTIFICATION_READY_FILE_ENV
from app.core.log import LogManager, get_logger
from app.core.notifications.config import load_notification_config
from app.core.notifications.server import NotificationServer
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.workers.signals import install_shutdown_signal_handlers

logger = get_logger(__name__)


async def run_notification_worker() -> None:
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(stop_event)

    config = await asyncio.to_thread(load_notification_config)
    server = NotificationServer(config)
    try:
        await server.start()
        ready_file = os.getenv(NOTIFICATION_READY_FILE_ENV)
        if ready_file:
            await asyncio.to_thread(Path(ready_file).touch, exist_ok=False)
        logger.info("Notification worker started")
        await stop_event.wait()
    finally:
        await server.stop()
        logger.info("Notification worker stopped")


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    asyncio.run(run_notification_worker())


if __name__ == "__main__":
    main()
