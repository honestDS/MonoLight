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
async def test_collector_dispatches_unmerged_messages_in_session_order():
    dispatch_order: list[str] = []
    a1_started = asyncio.Event()
    a2_started = asyncio.Event()
    b_completed = asyncio.Event()
    release_a1 = asyncio.Event()

    async def dispatch(message: CollectedMessage) -> None:
        text = message.texts[0]
        dispatch_order.append(text)
        if text == "A1":
            a1_started.set()
            await release_a1.wait()
        elif text == "A2":
            a2_started.set()
        elif text == "B":
            b_completed.set()

    collector = InboundMessageCollector(
        quiet_period_seconds=0,
        max_wait_seconds=2,
        merge=merge_message,
        dispatch=dispatch,
    )

    try:
        await collector.add("session-a", CollectedMessage(session_id="session-a", texts=["A1"]))
        await asyncio.wait_for(a1_started.wait(), timeout=2)
        await collector.add("session-a", CollectedMessage(session_id="session-a", texts=["A2"]))
        await collector.add("session-b", CollectedMessage(session_id="session-b", texts=["B"]))

        await asyncio.wait_for(b_completed.wait(), timeout=2)
        assert not a2_started.is_set()
    finally:
        release_a1.set()
        await collector.close()

    assert dispatch_order.count("A1") == 1
    assert dispatch_order.count("A2") == 1
    assert dispatch_order.index("A1") < dispatch_order.index("A2")


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
