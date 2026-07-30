import asyncio
import os

import app.warning_filters  # noqa: F401
from app.core.log import LogManager, get_logger
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.core.terminal.manager import terminal_worker_coordinator
from app.providers.database.bootstrap import create_database_tables
from app.workers.lease import run_with_worker_lease
from app.workers.signals import install_shutdown_signal_handlers

logger = get_logger(__name__)

TERMINAL_WORKER_NAME = "terminal"


async def run_terminal_worker() -> None:
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(stop_event)
    await create_database_tables()

    async def run_owned_worker(owned_stop_event: asyncio.Event) -> None:
        terminal_worker_coordinator.start()
        logger.info("Terminal worker started")
        try:
            await owned_stop_event.wait()
        finally:
            await terminal_worker_coordinator.stop()
            logger.info("Terminal worker stopped")

    await run_with_worker_lease(
        TERMINAL_WORKER_NAME,
        stop_event,
        run_owned_worker,
    )


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    asyncio.run(run_terminal_worker())


if __name__ == "__main__":
    main()
