import asyncio
import os

import app.warning_filters  # noqa: F401
from app.core.knowledge_jobs import create_knowledge_job_consumer
from app.core.log import LogManager, get_logger
from app.core.memory_jobs import create_memory_job_consumer
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.providers.database.bootstrap import create_database_tables
from app.workers.signals import install_shutdown_signal_handlers

logger = get_logger(__name__)

MEMORY_WORKER_NAME = "memory"


async def run_memory_worker() -> None:
    stop_event = asyncio.Event()
    install_shutdown_signal_handlers(stop_event)
    await create_database_tables()

    memory_consumer = create_memory_job_consumer()
    knowledge_consumer = create_knowledge_job_consumer()
    memory_consumer.start()
    knowledge_consumer.start()
    logger.info(f"{MEMORY_WORKER_NAME.capitalize()} worker started")
    try:
        await stop_event.wait()
    finally:
        await knowledge_consumer.stop()
        await memory_consumer.stop()
        logger.info(f"{MEMORY_WORKER_NAME.capitalize()} worker stopped")


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    asyncio.run(run_memory_worker())


if __name__ == "__main__":
    main()
