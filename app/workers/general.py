import asyncio
import os

import app.warning_filters  # noqa: F401
from app.core.log import LogManager
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.providers.database.bootstrap import create_database_tables
from app.workers.background_task import (
    BACKGROUND_TASK_WORKER_NAME,
    run_owned_background_task_worker,
)
from app.workers.lease import run_with_worker_lease
from app.workers.message_platform import (
    MESSAGE_PLATFORM_WORKER_NAME,
    run_owned_message_platform_worker,
)
from app.workers.session_reply import (
    SESSION_REPLY_WORKER_NAME,
    run_owned_session_reply_worker,
)
from app.workers.signals import install_shutdown_signal_handlers


async def run_general_worker() -> None:
    shared_stop_event = asyncio.Event()
    startup_ready = asyncio.Event()
    install_shutdown_signal_handlers(shared_stop_event)

    await create_database_tables()

    async def run_role(name, callback) -> None:
        try:
            await run_with_worker_lease(name, shared_stop_event, callback)
        finally:
            shared_stop_event.set()

    async def run_background_task_role() -> None:
        async def run_owned_background_task(owned_stop_event: asyncio.Event) -> None:
            await run_owned_background_task_worker(
                owned_stop_event,
                startup_ready=startup_ready,
            )

        try:
            await run_with_worker_lease(
                BACKGROUND_TASK_WORKER_NAME,
                shared_stop_event,
                run_owned_background_task,
            )
        finally:
            startup_ready.set()
            shared_stop_event.set()

    async def run_session_reply_role(owned_stop_event: asyncio.Event) -> None:
        await run_owned_session_reply_worker(
            owned_stop_event,
            startup_cleaned=True,
        )

    async with asyncio.TaskGroup() as task_group:
        task_group.create_task(run_background_task_role())
        await startup_ready.wait()
        task_group.create_task(
            run_role(
                MESSAGE_PLATFORM_WORKER_NAME,
                run_owned_message_platform_worker,
            )
        )
        task_group.create_task(
            run_role(
                SESSION_REPLY_WORKER_NAME,
                run_session_reply_role,
            )
        )


def main() -> None:
    LogManager.setup(
        log_path=os.getenv("LOG_FILE_PATH", str(DEFAULT_LOG_FILE_PATH)),
        level=os.getenv("LOG_LEVEL", "INFO"),
    )
    asyncio.run(run_general_worker())


if __name__ == "__main__":
    main()
