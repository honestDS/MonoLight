import asyncio
import os

import app.warning_filters  # noqa: F401
from app.core.background_tasks.recovery import clear_background_tasks_on_startup
from app.core.log import LogManager, get_logger
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.core.session_reply_queue.consumer import session_reply_consumer
from app.providers.database.bootstrap import create_database_tables
from app.workers.lease import run_startup_cleanup, run_with_worker_lease
from app.workers.signals import install_shutdown_signal_handlers

logger = get_logger(__name__)

SESSION_REPLY_WORKER_NAME = "session_reply"


async def run_owned_session_reply_worker(
    owned_stop_event: asyncio.Event,
    *,
    startup_cleaned: bool = False,
) -> None:
    if not startup_cleaned:
        await run_startup_cleanup(
            owned_stop_event,
            worker_name="session_reply",
            other_worker_name="background_task",
            cleanup=clear_background_tasks_on_startup,
        )
    session_reply_consumer.start()
    logger.info("Session reply worker started")
    try:
        await owned_stop_event.wait()
    finally:
        await session_reply_consumer.stop()
        logger.info("Session reply worker stopped")


async def run_session_reply_worker() -> None:
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(stop_event)
    await create_database_tables()

    await run_with_worker_lease(
        SESSION_REPLY_WORKER_NAME,
        stop_event,
        run_owned_session_reply_worker,
    )


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    asyncio.run(run_session_reply_worker())


if __name__ == "__main__":
    main()
