import asyncio
from datetime import UTC, datetime

import pytest

from app.core.constants import NOTIFICATION_INBOX_MAX_ITEMS
from app.core.notifications.inbox import NotificationBatch, NotificationInbox
from app.core.notifications.protocol import Notification, NotificationResource, NotificationScope


def _notification(
    *,
    topic="session.reply",
    event="updated",
    uid="user-1",
    session_id="session-1",
    profile_id=1,
    worker_id="worker-1",
    resource_type="work",
    resource_id="resource-1",
    occurred_at=None,
):
    timestamp = occurred_at or datetime(2026, 11, 22, tzinfo=UTC)
    resource = None
    if resource_id is not None:
        resource = NotificationResource(type=resource_type, id=resource_id)

    return Notification(
        topic=topic,
        event=event,
        source="test",
        occurred_at=timestamp,
        available_at=timestamp,
        scope=NotificationScope(
            uid=uid,
            session_id=session_id,
            profile_id=profile_id,
            worker_id=worker_id,
        ),
        resource=resource,
        correlation_id="correlation-1",
        data={"event": event},
    )


@pytest.mark.asyncio
async def test_initial_full_scan_is_taken_and_wait_then_times_out():
    inbox = NotificationInbox()

    batch = inbox.take()

    assert batch.full_scan is True
    assert batch.notifications == ()
    assert await inbox.wait(0.01) is False


def test_repeated_wakes_for_same_resource_keep_the_latest_notification():
    inbox = NotificationInbox()
    inbox.take()
    first = _notification(event="started")
    latest = _notification(
        event="completed",
        occurred_at=datetime(2026, 11, 22, 0, 0, 1, tzinfo=UTC),
    )

    inbox.wake(first)
    inbox.wake(latest)
    batch = inbox.take()

    assert batch.full_scan is False
    assert batch.notifications == (latest,)


def test_same_resource_is_not_merged_across_topics_or_scopes():
    inbox = NotificationInbox()
    inbox.take()
    same_topic_and_scope = _notification(event="first")
    other_topic = _notification(topic="task.background", event="second")
    other_scope = _notification(session_id="session-2", event="third")

    inbox.wake(same_topic_and_scope)
    inbox.wake(other_topic)
    inbox.wake(other_scope)
    batch = inbox.take()

    assert batch.full_scan is False
    assert {(notification.topic, notification.scope.session_id, notification.event) for notification in batch.notifications} == {
        ("session.reply", "session-1", "first"),
        ("task.background", "session-1", "second"),
        ("session.reply", "session-2", "third"),
    }


def test_full_scan_wake_overrides_buffered_notifications():
    inbox = NotificationInbox()
    inbox.take()
    inbox.wake(_notification())

    inbox.wake()
    batch = inbox.take()

    assert batch.full_scan is True
    assert batch.notifications == ()


@pytest.mark.asyncio
async def test_notification_between_take_and_wait_is_immediately_visible():
    inbox = NotificationInbox()
    inbox.take()
    notification = _notification()
    inbox.wake(notification)

    assert await inbox.wait(0.01) is True
    assert inbox.take().notifications == (notification,)


def test_wake_during_processing_is_retained_for_the_next_take():
    inbox = NotificationInbox()
    inbox.take()
    notification = _notification()

    inbox.wake(notification)

    batch = inbox.take()
    assert batch.full_scan is False
    assert batch.notifications == (notification,)


def test_overflowing_distinct_resources_falls_back_to_full_scan():
    inbox = NotificationInbox()
    inbox.take()

    for resource_id in range(NOTIFICATION_INBOX_MAX_ITEMS + 1):
        inbox.wake(_notification(resource_id=f"resource-{resource_id}"))

    batch = inbox.take()

    assert batch.full_scan is True
    assert batch.notifications == ()


@pytest.mark.asyncio
async def test_wait_propagates_task_cancellation():
    inbox = NotificationInbox()
    inbox.take()
    task = asyncio.create_task(inbox.wait(60))
    await asyncio.sleep(0)

    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task


def test_resource_ids_returns_none_for_full_scan():
    batch = NotificationBatch(full_scan=True, notifications=(_notification(resource_id="12"),))

    assert batch.resource_ids("session.reply", "work") is None


def test_resource_ids_returns_empty_set_for_empty_notifications():
    batch = NotificationBatch(full_scan=False, notifications=())

    assert batch.resource_ids("session.reply", "work") == set()


def test_resource_ids_returns_distinct_matching_positive_integer_ids():
    batch = NotificationBatch(
        full_scan=False,
        notifications=(
            _notification(resource_id="12"),
            _notification(resource_id="34"),
            _notification(resource_id="12"),
        ),
    )

    assert batch.resource_ids("session.reply", "work") == {12, 34}


@pytest.mark.parametrize(
    ("topic", "resource_type", "resource_id"),
    [
        ("task.background", "work", "12"),
        ("session.reply", "other", "12"),
        ("session.reply", "work", None),
        ("session.reply", "work", "not-an-integer"),
        ("session.reply", "work", "0"),
        ("session.reply", "work", "-1"),
    ],
)
def test_resource_ids_returns_none_for_unusable_notifications(topic, resource_type, resource_id):
    batch = NotificationBatch(
        full_scan=False,
        notifications=(_notification(topic=topic, resource_type=resource_type, resource_id=resource_id),),
    )

    assert batch.resource_ids("session.reply", "work") is None
