import asyncio
import os

import app.warning_filters  # noqa: F401
from app.core.background_tasks.manager import background_task_manager
from app.core.background_tasks.recovery import clear_background_tasks_on_startup
from app.core.log import LogManager, get_logger
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.core.utils.context_summary.cleanup import (
    background_context_summary_cleaner,
)
from app.providers.database.bootstrap import create_database_tables
from app.tasks import background_log_cleaner, background_temp_cleaner
from app.workers.lease import run_startup_cleanup, run_with_worker_lease
from app.workers.signals import install_shutdown_signal_handlers

logger = get_logger(__name__)

BACKGROUND_TASK_WORKER_NAME = "background_task"


async def run_owned_background_task_worker(
    owned_stop_event: asyncio.Event,
    *,
    startup_ready: asyncio.Event | None = None,
) -> None:
    await run_startup_cleanup(
        owned_stop_event,
        worker_name="background_task",
        other_worker_name="session_reply",
        cleanup=clear_background_tasks_on_startup,
    )
    if startup_ready is not None:
        startup_ready.set()

    background_task_manager.start()
    cleaner_tasks = [
        asyncio.create_task(background_log_cleaner(7)),
        asyncio.create_task(background_temp_cleaner()),
        asyncio.create_task(background_context_summary_cleaner()),
    ]
    logger.info("Background task worker started")
    try:
        await owned_stop_event.wait()
    finally:
        await background_task_manager.stop()
        for task in cleaner_tasks:
            task.cancel()
        await asyncio.gather(*cleaner_tasks, return_exceptions=True)
        logger.info("Background task worker stopped")


async def run_background_task_worker() -> None:
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(stop_event)

    await create_database_tables()

    await run_with_worker_lease(
        BACKGROUND_TASK_WORKER_NAME,
        stop_event,
        run_owned_background_task_worker,
    )


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    asyncio.run(run_background_task_worker())


if __name__ == "__main__":
    main()
