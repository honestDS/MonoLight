"""Coordinate notification publishing and subscribed inbox wake-ups."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Iterable
from typing import TYPE_CHECKING
from uuid import uuid4

from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.core.notifications.client import NotificationClient
from app.core.notifications.config import load_notification_config
from app.core.notifications.inbox import NotificationInbox
from app.core.notifications.protocol import ReadyFrame
from app.core.notifications.transactions import TransactionNotificationPublisher

if TYPE_CHECKING:
    from types import TracebackType


class NotificationRuntime:
    def __init__(
        self,
        client: NotificationClient,
        *,
        bind: Engine | None = None,
        session_class: type[Session] = Session,
    ) -> None:
        self.client = client
        self.publisher = TransactionNotificationPublisher(
            client,
            bind=bind,
            session_class=session_class,
        )
        self.routes: dict[str, list[NotificationInbox]] = {}
        self.listener_task: asyncio.Task[None] | None = None
        self.ready = asyncio.Event()
        self._started = False
        self._subscriptions_frozen = False

    def subscribe(self, topics: Iterable[str]) -> NotificationInbox:
        if self._subscriptions_frozen:
            raise RuntimeError("cannot_subscribe_after_start")

        inbox = NotificationInbox()
        for topic in dict.fromkeys(topics):
            self.routes.setdefault(topic, []).append(inbox)
        return inbox

    def start(self) -> None:
        if self._started:
            return

        self.publisher.start()
        if self.routes:
            self.listener_task = asyncio.get_running_loop().create_task(
                self._listen(),
                name="notification-runtime-listener",
            )
        self._subscriptions_frozen = True
        self._started = True

    async def _listen(self) -> None:
        async for frame in self.client.listen(tuple(self.routes)):
            if isinstance(frame, ReadyFrame):
                self.ready.set()
                self._wake_all()
                continue

            for inbox in self.routes.get(frame.topic, ()):
                inbox.wake(frame)

    def _wake_all(self) -> None:
        inboxes = dict.fromkeys(inbox for topic_inboxes in self.routes.values() for inbox in topic_inboxes)
        for inbox in inboxes:
            inbox.wake()

    async def wait_ready(self) -> None:
        await self.ready.wait()

    async def flush(self) -> None:
        await self.publisher.flush()

    async def stop(self) -> None:
        task = self.listener_task
        try:
            if task is not None:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        finally:
            self.listener_task = None
            try:
                await self.publisher.stop()
            finally:
                self._started = False
                self.ready.clear()

    async def __aenter__(self) -> NotificationRuntime:
        self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.stop()


async def create_notification_runtime(
    process_name: str,
    *,
    bind: Engine | None = None,
    session_class: type[Session] = Session,
) -> NotificationRuntime:
    config = await asyncio.to_thread(load_notification_config)
    client = NotificationClient(
        config,
        f"{process_name}:{os.getpid()}:{uuid4().hex}",
    )
    return NotificationRuntime(client, bind=bind, session_class=session_class)


__all__ = ["NotificationRuntime", "create_notification_runtime"]
