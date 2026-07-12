import asyncio
from dataclasses import dataclass, field

import pytest

from app.core.message_platforms.inbound_collector import InboundMessageCollector


@dataclass
class CollectedMessage:
    session_id: str
    texts: list[str] = field(default_factory=list)
    attachments: list[str] = field(default_factory=list)


def merge_message(left: CollectedMessage, right: CollectedMessage) -> CollectedMessage:
    return CollectedMessage(
        session_id=left.session_id,
        texts=[*left.texts, *right.texts],
        attachments=[*left.attachments, *right.attachments],
    )


@pytest.mark.asyncio
async def test_collector_merges_messages_arriving_within_quiet_period():
    dispatched: list[CollectedMessage] = []
    event = asyncio.Event()

    async def dispatch(message: CollectedMessage) -> None:
        dispatched.append(message)
        event.set()

    collector = InboundMessageCollector(
        quiet_period_seconds=0.05,
        max_wait_seconds=0.2,
        merge=merge_message,
        dispatch=dispatch,
    )

    await collector.add("session-1", CollectedMessage(session_id="session-1", texts=["text"]))
    await asyncio.sleep(0.02)
    await collector.add("session-1", CollectedMessage(session_id="session-1", attachments=["image.jpg"]))

    await asyncio.wait_for(event.wait(), timeout=0.3)
    await collector.close()

    assert dispatched == [
        CollectedMessage(
            session_id="session-1",
            texts=["text"],
            attachments=["image.jpg"],
        )
    ]


@pytest.mark.asyncio
async def test_collector_dispatches_at_max_wait_during_continuous_input():
    dispatched: list[CollectedMessage] = []
    event = asyncio.Event()

    async def dispatch(message: CollectedMessage) -> None:
        dispatched.append(message)
        event.set()

    collector = InboundMessageCollector(
        quiet_period_seconds=0.1,
        max_wait_seconds=0.12,
        merge=merge_message,
        dispatch=dispatch,
    )

    await collector.add("session-1", CollectedMessage(session_id="session-1", texts=["one"]))
    await asyncio.sleep(0.04)
    await collector.add("session-1", CollectedMessage(session_id="session-1", texts=["two"]))
    await asyncio.sleep(0.04)
    await collector.add("session-1", CollectedMessage(session_id="session-1", texts=["three"]))

    await asyncio.wait_for(event.wait(), timeout=0.2)
    await collector.close()

    assert dispatched == [
        CollectedMessage(
            session_id="session-1",
            texts=["one", "two", "three"],
        )
    ]


@pytest.mark.asyncio
async def test_collector_keeps_sessions_isolated():
    dispatched: list[CollectedMessage] = []

    async def dispatch(message: CollectedMessage) -> None:
        dispatched.append(message)

    collector = InboundMessageCollector(
        quiet_period_seconds=1,
        max_wait_seconds=2,
        merge=merge_message,
        dispatch=dispatch,
    )

    await collector.add("session-1", CollectedMessage(session_id="session-1", texts=["one"]))
    await collector.add("session-2", CollectedMessage(session_id="session-2", texts=["two"]))
    await collector.close()

    assert sorted(dispatched, key=lambda item: item.session_id) == [
        CollectedMessage(session_id="session-1", texts=["one"]),
        CollectedMessage(session_id="session-2", texts=["two"]),
    ]


@pytest.mark.asyncio
async def test_collector_close_flushes_pending_messages():
    dispatched: list[CollectedMessage] = []

    async def dispatch(message: CollectedMessage) -> None:
        dispatched.append(message)

    collector = InboundMessageCollector(
        quiet_period_seconds=10,
        max_wait_seconds=20,
        merge=merge_message,
        dispatch=dispatch,
    )

    await collector.add("session-1", CollectedMessage(session_id="session-1", texts=["pending"]))
    await collector.close()

    assert dispatched == [CollectedMessage(session_id="session-1", texts=["pending"])]

