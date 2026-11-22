"""Publish compact database notifications only after transaction commits."""

import asyncio
from collections.abc import Callable
from datetime import datetime
from typing import cast

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session, SessionTransaction

from app.core.constants import (
    NOTIFICATION_INBOX_MAX_ITEMS,
    NOTIFICATION_IO_TIMEOUT_SECONDS,
    NOTIFICATION_PUBLISH_QUEUE_SIZE,
)
from app.core.log import get_logger
from app.core.notifications.client import NotificationClient
from app.core.notifications.protocol import Notification, NotificationResource, NotificationScope

logger = get_logger(__name__)
_SESSION_NOTIFICATION_BUFFERS_KEY = "_database_notification_buffers"

type _NotificationKey = tuple[str, str | None, str | None, int | None, str | None, str | None, str | None]
type _PendingNotifications = dict[_NotificationKey, Notification]
type _TransactionBuffers = dict[SessionTransaction, _PendingNotifications]


def _notification_key(notification: Notification) -> _NotificationKey:
    scope = notification.scope
    resource = notification.resource
    resource_type = None
    resource_id = None
    if resource is not None:
        resource_type = resource.type
        resource_id = resource.id
    return (
        notification.topic,
        scope.uid,
        scope.session_id,
        scope.profile_id,
        scope.worker_id,
        resource_type,
        resource_id,
    )


def _log_dropped_notification(reason: str, notification: Notification) -> None:
    resource = notification.resource
    resource_type = None
    resource_id = None
    if resource is not None:
        resource_type = resource.type
        resource_id = resource.id
    logger.warning(
        "{} topic={} resource_type={} resource_id={}; database reconciliation will recover",
        reason,
        notification.topic,
        resource_type,
        resource_id,
    )


def _store_notification(
    pending: _PendingNotifications,
    key: _NotificationKey,
    notification: Notification,
) -> None:
    if key in pending:
        pending[key] = notification
        return
    if len(pending) >= NOTIFICATION_INBOX_MAX_ITEMS:
        _log_dropped_notification("transaction notification inbox full", notification)
        return
    pending[key] = notification


def notify_after_commit(
    db: AsyncSession,
    topic: str,
    *,
    event: str = "changed",
    resource_type: str | None = None,
    resource_id: str | int | None = None,
    uid: str | None = None,
    session_id: str | None = None,
    profile_id: int | None = None,
    available_at: datetime | None = None,
) -> None:
    """Buffer a compact notification in the current synchronous transaction."""
    if (resource_type is None) != (resource_id is None):
        raise ValueError("resource_type_and_id_must_be_provided_together")

    resource = None
    if resource_type is not None and resource_id is not None:
        resource = NotificationResource(type=resource_type, id=str(resource_id))

    notification = Notification(
        topic=topic,
        event=event,
        source="database",
        scope=NotificationScope(uid=uid, session_id=session_id, profile_id=profile_id),
        resource=resource,
        available_at=available_at,
    )

    session = db.sync_session
    transaction = session.get_nested_transaction() or session.get_transaction()
    if transaction is None:
        transaction = session.begin()

    buffers = cast(_TransactionBuffers, session.info.setdefault(_SESSION_NOTIFICATION_BUFFERS_KEY, {}))
    pending = buffers.get(transaction)
    if pending is None:
        pending = {}
        buffers[transaction] = pending
    _store_notification(pending, _notification_key(notification), notification)


class TransactionNotificationPublisher:
    """Move committed transaction notifications to one bounded async publisher."""

    def __init__(
        self,
        client: NotificationClient,
        *,
        bind: Engine | None = None,
        session_class: type[Session] = Session,
    ) -> None:
        self._client = client
        self._bind = bind
        self._session_class = session_class
        self._queue: asyncio.Queue[Notification] = asyncio.Queue(maxsize=NOTIFICATION_PUBLISH_QUEUE_SIZE)
        self._task: asyncio.Task[None] | None = None
        self._started = False
        self._listeners_registered = False
        self._after_commit_listener: Callable[[Session], None] = self._handle_after_commit
        self._after_transaction_end_listener: Callable[[Session, SessionTransaction], None] = self._handle_after_transaction_end

    def start(self) -> None:
        """Register transaction events and start the sole publisher task."""
        if self._started:
            return
        loop = asyncio.get_running_loop()
        event.listen(self._session_class, "after_commit", self._after_commit_listener)
        event.listen(
            self._session_class,
            "after_transaction_end",
            self._after_transaction_end_listener,
        )
        self._listeners_registered = True
        self._task = loop.create_task(self._publish_loop(), name="transaction-notification-publisher")
        self._started = True

    async def flush(self) -> None:
        """Wait until all queued notifications have been processed."""
        await self._queue.join()

    async def stop(self) -> None:
        """Unregister transaction events, make a bounded flush, and release queued work."""
        self._remove_listeners()
        task = self._task
        if task is None:
            return
        try:
            await asyncio.wait_for(self.flush(), timeout=NOTIFICATION_IO_TIMEOUT_SECONDS)
        except TimeoutError:
            pass
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self._task = None
            self._started = False
            self._clear_queue()

    def _remove_listeners(self) -> None:
        if not self._listeners_registered:
            return
        event.remove(self._session_class, "after_commit", self._after_commit_listener)
        event.remove(
            self._session_class,
            "after_transaction_end",
            self._after_transaction_end_listener,
        )
        self._listeners_registered = False

    def _matches_bind(self, session: Session) -> bool:
        if self._bind is None:
            return True
        return session.get_bind() is self._bind

    def _handle_after_commit(self, session: Session) -> None:
        if not self._matches_bind(session):
            return
        transaction = session.get_nested_transaction() or session.get_transaction()
        if transaction is None:
            return

        buffers = cast(_TransactionBuffers, session.info.get(_SESSION_NOTIFICATION_BUFFERS_KEY, {}))
        pending = buffers.pop(transaction, None)
        if pending is None:
            return

        parent = transaction.parent
        if parent is None:
            self._enqueue_notifications(pending)
            return

        parent_pending = buffers.get(parent)
        if parent_pending is None:
            parent_pending = {}
            buffers[parent] = parent_pending
        for key, notification in pending.items():
            _store_notification(parent_pending, key, notification)

    def _handle_after_transaction_end(self, session: Session, transaction: SessionTransaction) -> None:
        if not self._matches_bind(session):
            return
        buffers = cast(_TransactionBuffers, session.info.get(_SESSION_NOTIFICATION_BUFFERS_KEY, {}))
        if transaction.parent is None:
            buffers.clear()
        else:
            buffers.pop(transaction, None)
        if buffers:
            return
        session.info.pop(_SESSION_NOTIFICATION_BUFFERS_KEY, None)

    def _enqueue_notifications(
        self,
        notifications: _PendingNotifications,
    ) -> None:
        for notification in notifications.values():
            try:
                self._queue.put_nowait(notification)
            except asyncio.QueueFull:
                _log_dropped_notification("notification publish queue full", notification)

    async def _publish_loop(self) -> None:
        while True:
            notification = await self._queue.get()
            try:
                await self._client.publish(notification)
            except asyncio.CancelledError:
                raise
            except Exception:
                resource = notification.resource
                resource_type = None
                resource_id = None
                if resource is not None:
                    resource_type = resource.type
                    resource_id = resource.id
                logger.error(
                    "notification publish failed topic={} resource_type={} resource_id={}",
                    notification.topic,
                    resource_type,
                    resource_id,
                )
            finally:
                self._queue.task_done()

    def _clear_queue(self) -> None:
        while True:
            try:
                self._queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            self._queue.task_done()
