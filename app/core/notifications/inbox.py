"""Bounded, coalescing notification inbox for one process consumer."""

import asyncio
from dataclasses import dataclass

from app.core.constants import NOTIFICATION_INBOX_MAX_ITEMS
from app.core.notifications.protocol import Notification


@dataclass(frozen=True)
class NotificationBatch:
    full_scan: bool
    notifications: tuple[Notification, ...]

    def resource_ids(self, topic: str, resource_type: str) -> set[int] | None:
        if self.full_scan:
            return None
        resource_ids: set[int] = set()
        for notification in self.notifications:
            resource = notification.resource
            if notification.topic != topic or resource is None or resource.type != resource_type:
                return None
            try:
                resource_id = int(resource.id)
            except ValueError:
                return None
            if resource_id <= 0:
                return None
            resource_ids.add(resource_id)
        return resource_ids


class NotificationInbox:
    def __init__(self) -> None:
        self._event = asyncio.Event()
        self._notifications: dict[
            tuple[str, str | None, str | None, int | None, str | None, str | None, str | None],
            Notification,
        ] = {}
        self._full_scan = True
        self._event.set()

    def wake(self, notification: Notification | None = None) -> None:
        if notification is None:
            self._full_scan = True
            self._notifications.clear()
            self._event.set()
            return

        if self._full_scan:
            return

        scope = notification.scope
        resource = notification.resource
        resource_type = None
        resource_id = None
        if resource is not None:
            resource_type = resource.type
            resource_id = resource.id

        key = (
            notification.topic,
            scope.uid,
            scope.session_id,
            scope.profile_id,
            scope.worker_id,
            resource_type,
            resource_id,
        )
        if key not in self._notifications and len(self._notifications) >= NOTIFICATION_INBOX_MAX_ITEMS:
            self._full_scan = True
            self._notifications.clear()
            self._event.set()
            return

        self._notifications[key] = notification
        self._event.set()

    def take(self) -> NotificationBatch:
        batch = NotificationBatch(
            full_scan=self._full_scan,
            notifications=tuple(self._notifications.values()),
        )
        self._event.clear()
        self._notifications.clear()
        self._full_scan = False
        return batch

    async def wait(self, timeout: float) -> bool:
        try:
            await asyncio.wait_for(self._event.wait(), max(0, timeout))
        except TimeoutError:
            return False
        return True
