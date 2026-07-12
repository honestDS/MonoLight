import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.core import log_broadcaster as broadcaster_module
from app.core.log import build_process_log_path
from app.core.log_broadcaster import LogBroadcaster


def test_process_log_paths_are_isolated_by_process_id():
    first = build_process_log_path("data/logs/monolight.log", process_id=1001)
    second = build_process_log_path("data/logs/monolight.log", process_id=1002)
    tool = build_process_log_path("data/logs/tools.log", process_id=1001)

    assert first.endswith("monolight.1001.log")
    assert second.endswith("monolight.1002.log")
    assert tool.endswith("tools.1001.log")
    assert first != second


class FakeSessionContext:
    async def __aenter__(self):
        return object()

    async def __aexit__(self, exc_type, exc_value, traceback):
        return None


class RecordingWebSocket:
    def __init__(self):
        self.accepted = False
        self.messages = []

    async def accept(self):
        self.accepted = True

    async def send_text(self, message):
        self.messages.append(json.loads(message))


class FailingWebSocket:
    async def accept(self):
        return None

    async def send_text(self, _message):
        raise RuntimeError("disconnected")


class BlockingWebSocket(RecordingWebSocket):
    def __init__(self):
        super().__init__()
        self.send_started = asyncio.Event()
        self.release_send = asyncio.Event()

    async def send_text(self, message):
        self.send_started.set()
        await self.release_send.wait()
        await super().send_text(message)


@pytest.mark.asyncio
async def test_database_logs_are_broadcast_by_each_broadcaster_instance(monkeypatch):
    created_at = datetime(2026, 7, 11, 8, 0, 0, 123000)
    database_logs = [
        SimpleNamespace(
            id=11,
            created_at=created_at,
            level="INFO",
            module="app.workers.background_task",
            message="Background task completed",
            uid="uid",
            session_id="session",
            extra='{"task_id": 7}',
        )
    ]

    async def fake_get_latest_id(_db):
        return 10

    async def fake_list_after_id(_db, *, after_id, limit):
        assert limit > 0
        if after_id == 10:
            return database_logs
        return []

    monkeypatch.setattr(broadcaster_module, "AsyncSessionLocal", FakeSessionContext)
    monkeypatch.setattr(broadcaster_module.system_log_crud, "get_latest_id", fake_get_latest_id)
    monkeypatch.setattr(broadcaster_module.system_log_crud, "list_after_id", fake_list_after_id)

    first_broadcaster = LogBroadcaster()
    second_broadcaster = LogBroadcaster()
    first_messages = []
    second_messages = []

    async def first_broadcast(log_entry, *, log_id=None):
        first_messages.append((log_id, log_entry))

    async def second_broadcast(log_entry, *, log_id=None):
        second_messages.append((log_id, log_entry))

    monkeypatch.setattr(first_broadcaster, "broadcast", first_broadcast)
    monkeypatch.setattr(second_broadcaster, "broadcast", second_broadcast)

    await first_broadcaster.start()
    await second_broadcaster.start()
    try:
        await asyncio.wait_for(_wait_for_messages(first_messages, second_messages), timeout=1)
    finally:
        await first_broadcaster.stop()
        await second_broadcaster.stop()

    expected = {
        "timestamp": "2026-07-11 08:00:00.123",
        "level": "INFO",
        "module": "app.workers.background_task",
        "message": "Background task completed",
        "uid": "uid",
        "session_id": "session",
        "extra": {"task_id": 7},
    }
    assert first_messages == [(11, expected)]
    assert second_messages == [(11, expected)]


@pytest.mark.asyncio
async def test_connect_snapshot_cursor_prevents_duplicate_and_missing_logs():
    broadcaster = LogBroadcaster()
    websocket = RecordingWebSocket()
    initialization_order = []

    async def initialize():
        initialization_order.append("history")
        return 11, {"type": "history", "logs": []}

    await broadcaster.connect(websocket, initialize)
    try:
        await broadcaster.broadcast({"message": "already in history"}, log_id=11)
        await broadcaster.broadcast({"message": "new realtime log"}, log_id=12)
        await _wait_until(lambda: len(websocket.messages) == 2)

        assert websocket.accepted is True
        assert initialization_order == ["history"]
        assert websocket.messages == [{"type": "history", "logs": []}, {"message": "new realtime log"}]
        assert broadcaster.active_connections[websocket].after_id == 12
    finally:
        await broadcaster.disconnect(websocket)


@pytest.mark.asyncio
async def test_failed_websocket_is_removed_immediately():
    broadcaster = LogBroadcaster()
    websocket = FailingWebSocket()

    async def initialize():
        return 0, {"type": "history", "logs": []}

    await broadcaster.connect(websocket, initialize)
    await broadcaster.broadcast({"message": "test"}, log_id=1)
    await _wait_until(lambda: websocket not in broadcaster.active_connections)

    assert websocket not in broadcaster.active_connections


@pytest.mark.asyncio
async def test_slow_websocket_does_not_block_other_connections():
    broadcaster = LogBroadcaster()
    slow_websocket = BlockingWebSocket()
    fast_websocket = RecordingWebSocket()

    async def initialize():
        return 0, {"type": "history", "logs": []}

    await broadcaster.connect(slow_websocket, initialize)
    await broadcaster.connect(fast_websocket, initialize)
    try:
        await broadcaster.broadcast({"message": "first"}, log_id=1)
        await slow_websocket.send_started.wait()
        await broadcaster.broadcast({"message": "second"}, log_id=2)
        await _wait_until(lambda: len(fast_websocket.messages) == 3)

        assert slow_websocket.messages == []
        assert fast_websocket.messages == [{"type": "history", "logs": []}, {"message": "first"}, {"message": "second"}]
    finally:
        slow_websocket.release_send.set()
        await broadcaster.disconnect(slow_websocket)
        await broadcaster.disconnect(fast_websocket)


@pytest.mark.asyncio
async def test_messages_are_sent_in_order_per_connection():
    broadcaster = LogBroadcaster()
    websocket = RecordingWebSocket()

    async def initialize():
        return 0, {"type": "history", "logs": []}

    await broadcaster.connect(websocket, initialize)
    try:
        await broadcaster.broadcast({"message": "first"}, log_id=1)
        await broadcaster.broadcast({"message": "second"}, log_id=2)
        await broadcaster.broadcast({"message": "third"}, log_id=3)
        await _wait_until(lambda: len(websocket.messages) == 4)

        assert websocket.messages == [{"type": "history", "logs": []}, {"message": "first"}, {"message": "second"}, {"message": "third"}]
        assert broadcaster.active_connections[websocket].after_id == 3
    finally:
        await broadcaster.disconnect(websocket)


def test_poll_error_is_reported_to_stderr_without_log_sink_recursion(monkeypatch, capsys):
    broadcaster = LogBroadcaster()
    monotonic_values = iter([100.0, 101.0, 131.0])
    monkeypatch.setattr(broadcaster_module.time, "monotonic", lambda: next(monotonic_values))

    broadcaster._report_poll_error(RuntimeError("database unavailable"))
    broadcaster._report_poll_error(RuntimeError("still unavailable"))
    broadcaster._report_poll_error(RuntimeError("database unavailable again"))

    stderr = capsys.readouterr().err
    assert stderr.count("Log broadcaster database polling failed") == 2
    assert "database unavailable" in stderr
    assert "database unavailable again" in stderr


async def _wait_for_messages(first_messages, second_messages):
    while not first_messages or not second_messages:
        await asyncio.sleep(0.01)

    json.dumps(first_messages[0])
    json.dumps(second_messages[0])


async def _wait_until(predicate):
    await asyncio.wait_for(_poll_until(predicate), timeout=1)


async def _poll_until(predicate):
    while not predicate():
        await asyncio.sleep(0.01)
