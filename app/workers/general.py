import asyncio
import os

import app.warning_filters  # noqa: F401
from app.core.log import LogManager
from app.core.notifications.protocol import NotificationTopic
from app.core.notifications.runtime import create_notification_runtime
from app.core.paths import DEFAULT_LOG_FILE_PATH
from app.providers.database import engine
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
    notifications = await create_notification_runtime("general", bind=engine.sync_engine)
    background_inbox = notifications.subscribe(
        (
            NotificationTopic.BACKGROUND_TASK,
            NotificationTopic.PROFILE_CONFIG,
            NotificationTopic.SYSTEM_SETTINGS,
        )
    )
    reply_inbox = notifications.subscribe(
        (
            NotificationTopic.SESSION_REPLY,
            NotificationTopic.PROFILE_CONFIG,
            NotificationTopic.SYSTEM_SETTINGS,
        )
    )
    platform_inbox = notifications.subscribe((NotificationTopic.MESSAGE_PLATFORM,))
    outbox_inbox = notifications.subscribe((NotificationTopic.OUTBOX,))
    scheduled_inbox = notifications.subscribe((NotificationTopic.SCHEDULED_TASK,))

    async def _run_role(name, callback) -> None:
        try:
            await run_with_worker_lease(name, shared_stop_event, callback)
        finally:
            shared_stop_event.set()

    async def _run_background_task_role() -> None:
        async def _run_owned_background_task(owned_stop_event: asyncio.Event) -> None:
            await run_owned_background_task_worker(
                owned_stop_event,
                inbox=background_inbox,
                startup_ready=startup_ready,
            )

        try:
            await run_with_worker_lease(
                BACKGROUND_TASK_WORKER_NAME,
                shared_stop_event,
                _run_owned_background_task,
            )
        finally:
            startup_ready.set()
            shared_stop_event.set()

    async def _run_session_reply_role(owned_stop_event: asyncio.Event) -> None:
        await run_owned_session_reply_worker(
            owned_stop_event,
            inbox=reply_inbox,
            startup_cleaned=True,
        )

    async def _run_message_platform_role(owned_stop_event: asyncio.Event) -> None:
        await run_owned_message_platform_worker(
            owned_stop_event,
            config_inbox=platform_inbox,
            outbox_inbox=outbox_inbox,
            scheduled_inbox=scheduled_inbox,
        )

    async with notifications:
        async with asyncio.TaskGroup() as task_group:
            task_group.create_task(_run_background_task_role())
            await startup_ready.wait()
            task_group.create_task(
                _run_role(
                    MESSAGE_PLATFORM_WORKER_NAME,
                    _run_message_platform_role,
                )
            )
            task_group.create_task(
                _run_role(
                    SESSION_REPLY_WORKER_NAME,
                    _run_session_reply_role,
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
