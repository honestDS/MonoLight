import asyncio
import json
import os
import secrets
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import psutil
import pytest
import pytest_asyncio

import app.core.notifications.client as notification_client_module
import app.core.notifications.config as notification_config_module
import app.core.notifications.server as notification_server_module
import start as launcher
from app.core.constants import (
    NOTIFICATION_MAX_FRAME_BYTES,
    NOTIFICATION_READY_FILE_ENV,
)
from app.core.notifications.client import NotificationClient
from app.core.notifications.config import NotificationConfig
from app.core.notifications.protocol import (
    ErrorFrame,
    EventFrame,
    HelloFrame,
    Notification,
    NotificationProtocolError,
    NotificationResource,
    NotificationScope,
    NotificationTopic,
    PingFrame,
    PongFrame,
    PublishedFrame,
    PublishFrame,
    ReadyFrame,
    encode_frame,
    read_frame,
)
from app.core.notifications.server import NotificationServer

_IO_TIMEOUT_SECONDS = 8.0


async def _cleanup_spawned_process(
    process: subprocess.Popen,
    stdout_reader: asyncio.Task | None = None,
) -> None:
    def _kill_spawned_process_tree() -> None:
        try:
            root = psutil.Process(process.pid)
            descendants = root.children(recursive=True)
        except psutil.NoSuchProcess:
            return

        for descendant in reversed(descendants):
            try:
                descendant.kill()
            except psutil.NoSuchProcess:
                continue

        try:
            root.kill()
        except psutil.NoSuchProcess:
            pass
        psutil.wait_procs(descendants, timeout=_IO_TIMEOUT_SECONDS)

    if stdout_reader is not None and not stdout_reader.done():
        await asyncio.to_thread(_kill_spawned_process_tree)
        await asyncio.wait_for(
            asyncio.gather(stdout_reader, return_exceptions=True),
            timeout=_IO_TIMEOUT_SECONDS,
        )

    try:
        await asyncio.to_thread(
            process.communicate,
            input="\n",
            timeout=_IO_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        await asyncio.to_thread(_kill_spawned_process_tree)
        await asyncio.to_thread(process.communicate, timeout=_IO_TIMEOUT_SECONDS)


def _notification_worker_script() -> str:
    return (
        "import asyncio\n"
        "import json\n"
        "import os\n"
        "import sys\n"
        "import threading\n"
        "from app.core.notifications.config import NotificationConfig\n"
        "from app.core.notifications.server import NotificationServer\n"
        "from app.workers import notification as worker\n"
        "\n"
        "worker.load_notification_config = lambda: NotificationConfig(\n"
        "    host='127.0.0.1', port=int(sys.argv[2]), token=sys.argv[1]\n"
        ")\n"
        "\n"
        "class ReportingNotificationServer(NotificationServer):\n"
        "    async def start(self):\n"
        "        await super().start()\n"
        "        print(json.dumps({\n"
        "            'host': self.address[0],\n"
        "            'port': self.address[1],\n"
        "            'pid': os.getpid(),\n"
        "        }), flush=True)\n"
        "\n"
        "worker.NotificationServer = ReportingNotificationServer\n"
        "\n"
        "def _install_test_shutdown_signal_handlers(stop_event):\n"
        "    loop = asyncio.get_running_loop()\n"
        "    def _stop_when_stdin_closes():\n"
        "        sys.stdin.readline()\n"
        "        loop.call_soon_threadsafe(stop_event.set)\n"
        "    threading.Thread(target=_stop_when_stdin_closes, daemon=True).start()\n"
        "\n"
        "worker.install_shutdown_signal_handlers = _install_test_shutdown_signal_handlers\n"
        "asyncio.run(worker.run_notification_worker())\n"
    )


async def _read_frame(reader: asyncio.StreamReader):
    return await asyncio.wait_for(read_frame(reader), timeout=_IO_TIMEOUT_SECONDS)


async def _send_frame(writer: asyncio.StreamWriter, frame) -> None:
    writer.write(encode_frame(frame))
    await asyncio.wait_for(writer.drain(), timeout=_IO_TIMEOUT_SECONDS)


async def _open_raw_client(
    runtime,
    *,
    client_id: str,
    subscriptions: tuple[str, ...] = (),
    token: str | None = None,
    request_id: str | None = None,
):
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            runtime.host,
            runtime.port,
            limit=NOTIFICATION_MAX_FRAME_BYTES,
        ),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    runtime.raw_writers.append(writer)
    hello = HelloFrame(
        request_id=request_id or f"{client_id}-hello",
        token=runtime.token if token is None else token,
        client_id=client_id,
        subscriptions=subscriptions,
    )
    await _send_frame(writer, hello)
    ready = await _read_frame(reader)
    assert isinstance(ready, ReadyFrame)
    return reader, writer, ready


async def _publish_raw(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    notification: Notification,
    request_id: str,
) -> PublishedFrame:
    await _send_frame(
        writer,
        PublishFrame(request_id=request_id, notification=notification),
    )
    response = await _read_frame(reader)
    assert isinstance(response, PublishedFrame)
    assert response.request_id == request_id
    return response


def _notification(topic: str, marker: str, *, source: str = "forged-source") -> Notification:
    return Notification(
        topic=topic,
        event=f"{topic}.available",
        source=source,
        occurred_at=datetime.now(UTC),
        available_at=datetime.now(UTC) + timedelta(hours=12),
        scope=NotificationScope(
            uid=f"uid-{marker}",
            session_id=f"session-{marker}",
            worker_id=f"worker-{marker}",
            profile_id=73,
        ),
        resource=NotificationResource(type=f"{topic}.resource", id=f"resource-{marker}"),
        correlation_id=f"correlation-{marker}",
        data={"marker": marker, "sequence": 3, "details": {"ready": True}},
    )


@pytest_asyncio.fixture
async def notification_runtime():
    token = secrets.token_hex(32)
    server = NotificationServer(NotificationConfig(host="127.0.0.1", port=0, token=token))
    servers = [server]
    await asyncio.wait_for(server.start(), timeout=_IO_TIMEOUT_SECONDS)
    host, port = server.address
    runtime = SimpleNamespace(
        host=host,
        port=port,
        address=(host, port),
        token=token,
        config=NotificationConfig(host=host, port=port, token=token),
        server=server,
        servers=servers,
        listeners=[],
        raw_writers=[],
        tasks=[],
    )

    def make_client(client_id: str) -> NotificationClient:
        return NotificationClient(runtime.config, client_id)

    def make_listener(client: NotificationClient, topics: tuple[str, ...]):
        listener = client.listen(topics)
        runtime.listeners.append(listener)
        return listener

    def track_task(coroutine):
        task = asyncio.create_task(coroutine)
        runtime.tasks.append(task)
        return task

    runtime.client = make_client
    runtime.listen = make_listener
    runtime.track_task = track_task

    try:
        yield runtime
    finally:
        for task in runtime.tasks:
            if not task.done():
                task.cancel()
        if runtime.tasks:
            await asyncio.wait_for(
                asyncio.gather(*runtime.tasks, return_exceptions=True),
                timeout=_IO_TIMEOUT_SECONDS,
            )

        if runtime.listeners:
            await asyncio.wait_for(
                asyncio.gather(
                    *(listener.aclose() for listener in runtime.listeners),
                    return_exceptions=True,
                ),
                timeout=_IO_TIMEOUT_SECONDS,
            )

        for writer in runtime.raw_writers:
            writer.close()
        if runtime.raw_writers:
            await asyncio.wait_for(
                asyncio.gather(
                    *(writer.wait_closed() for writer in runtime.raw_writers),
                    return_exceptions=True,
                ),
                timeout=_IO_TIMEOUT_SECONDS,
            )

        for managed_server in reversed(runtime.servers):
            await asyncio.wait_for(
                managed_server.stop(),
                timeout=_IO_TIMEOUT_SECONDS,
            )


@pytest.mark.asyncio
async def test_topics_broadcast_exact_complete_notifications_without_replay(
    notification_runtime,
):
    runtime = notification_runtime
    topics = (
        NotificationTopic.MEMORY_JOB.value,
        NotificationTopic.TERMINAL_SESSION.value,
        "custom.reindex",
        NotificationTopic.BACKGROUND_TASK.value,
    )
    publisher = runtime.client("pre-subscription-publisher")

    for index, topic in enumerate(topics):
        published = await asyncio.wait_for(
            publisher.publish(_notification(topic, f"before-{index}")),
            timeout=_IO_TIMEOUT_SECONDS,
        )
        assert published.recipient_count == 0

    first_listener = runtime.listen(
        runtime.client("subscriber-one"),
        (*topics, topics[-1], topics[-1]),
    )
    second_listener = runtime.listen(runtime.client("subscriber-two"), topics)
    first_ready = await asyncio.wait_for(anext(first_listener), timeout=_IO_TIMEOUT_SECONDS)
    second_ready = await asyncio.wait_for(anext(second_listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(first_ready, ReadyFrame)
    assert isinstance(second_ready, ReadyFrame)

    publisher_reader, publisher_writer, _ = await _open_raw_client(
        runtime,
        client_id="verified-publisher",
    )

    unmatched = _notification(f"{topics[-1]}.child", "unmatched")
    unmatched_result = await _publish_raw(
        publisher_reader,
        publisher_writer,
        unmatched,
        "unmatched-publish",
    )
    assert unmatched_result.recipient_count == 0

    loop = asyncio.get_running_loop()
    started_at = loop.time()
    for index, topic in enumerate(topics):
        original = _notification(topic, f"delivered-{index}")
        result = await _publish_raw(
            publisher_reader,
            publisher_writer,
            original,
            f"publish-{index}",
        )
        assert result.notification_id == original.notification_id
        assert result.recipient_count == 2

        first_delivery = await asyncio.wait_for(
            anext(first_listener),
            timeout=_IO_TIMEOUT_SECONDS,
        )
        second_delivery = await asyncio.wait_for(
            anext(second_listener),
            timeout=_IO_TIMEOUT_SECONDS,
        )
        expected = original.model_copy(update={"source": "verified-publisher"})
        assert first_delivery == expected
        assert second_delivery == expected
        assert first_delivery.available_at > datetime.now(UTC)

    sentinel = _notification(topics[-1], "duplicate-subscription-sentinel")
    sentinel_result = await _publish_raw(
        publisher_reader,
        publisher_writer,
        sentinel,
        "duplicate-subscription-sentinel",
    )
    assert sentinel_result.recipient_count == 2
    first_sentinel = await asyncio.wait_for(
        anext(first_listener),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    second_sentinel = await asyncio.wait_for(
        anext(second_listener),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    expected_sentinel = sentinel.model_copy(update={"source": "verified-publisher"})
    assert first_sentinel == expected_sentinel
    assert second_sentinel == expected_sentinel
    assert loop.time() - started_at < _IO_TIMEOUT_SECONDS


@pytest.mark.asyncio
async def test_raw_protocol_rejects_invalid_connections_and_keeps_serving(
    notification_runtime,
):
    runtime = notification_runtime

    async def assert_error_and_close(reader, expected_code: str, request_id: str | None = None):
        response = await _read_frame(reader)
        assert isinstance(response, ErrorFrame)
        assert response.code == expected_code
        if request_id is not None:
            assert response.request_id == request_id
        assert await asyncio.wait_for(reader.read(1), timeout=_IO_TIMEOUT_SECONDS) == b""

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(runtime.host, runtime.port),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    runtime.raw_writers.append(writer)
    bad_token_hello = HelloFrame(
        token=secrets.token_hex(32),
        client_id="wrong-token-client",
        request_id="wrong-token-request",
    )
    await _send_frame(writer, bad_token_hello)
    await assert_error_and_close(reader, "authentication_failed", "wrong-token-request")

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(runtime.host, runtime.port),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    runtime.raw_writers.append(writer)
    pre_auth_publish = PublishFrame(
        request_id="publish-before-hello",
        notification=_notification("custom.raw", "before-hello"),
    )
    await _send_frame(writer, pre_auth_publish)
    await assert_error_and_close(reader, "unexpected_message", "publish-before-hello")

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(runtime.host, runtime.port),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    runtime.raw_writers.append(writer)
    invalid_version = {
        "type": "hello",
        "version": 2,
        "request_id": "unsupported-version-request",
        "token": runtime.token,
        "client_id": "unsupported-version-client",
        "subscriptions": [],
    }
    writer.write(json.dumps(invalid_version, separators=(",", ":")).encode() + b"\n")
    await asyncio.wait_for(writer.drain(), timeout=_IO_TIMEOUT_SECONDS)
    await assert_error_and_close(reader, "invalid_message")

    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            runtime.host,
            runtime.port,
            limit=NOTIFICATION_MAX_FRAME_BYTES,
        ),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    runtime.raw_writers.append(writer)
    writer.write(b"x" * (NOTIFICATION_MAX_FRAME_BYTES + 1) + b"\n")
    await asyncio.wait_for(writer.drain(), timeout=_IO_TIMEOUT_SECONDS)
    await assert_error_and_close(reader, "frame_too_large")

    duplicate_reader, duplicate_writer, _ = await _open_raw_client(
        runtime,
        client_id="duplicate-hello-client",
    )
    duplicate_hello = HelloFrame(
        token=runtime.token,
        client_id="duplicate-hello-client",
        request_id="duplicate-hello-request",
    )
    await _send_frame(duplicate_writer, duplicate_hello)
    await assert_error_and_close(
        duplicate_reader,
        "unexpected_message",
        "duplicate-hello-request",
    )

    ready = await asyncio.wait_for(
        runtime.client("healthy-after-errors").ping(),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert ready.pid == os.getpid()


@pytest.mark.asyncio
async def test_raw_protocol_accepts_fragmented_hello_and_coalesced_pings(
    notification_runtime,
):
    runtime = notification_runtime
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(
            runtime.host,
            runtime.port,
            limit=NOTIFICATION_MAX_FRAME_BYTES,
        ),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    runtime.raw_writers.append(writer)
    hello = HelloFrame(
        token=runtime.token,
        client_id="fragmented-hello-client",
        request_id="fragmented-hello-request",
    )
    encoded_hello = encode_frame(hello)
    split_at = len(encoded_hello) // 2
    writer.write(encoded_hello[:split_at])
    await asyncio.wait_for(writer.drain(), timeout=_IO_TIMEOUT_SECONDS)
    await asyncio.sleep(0.05)
    writer.write(encoded_hello[split_at:])
    await asyncio.wait_for(writer.drain(), timeout=_IO_TIMEOUT_SECONDS)

    ready = await _read_frame(reader)
    assert isinstance(ready, ReadyFrame)
    assert ready.request_id == "fragmented-hello-request"

    ping_ids = ("coalesced-ping-one", "coalesced-ping-two", "coalesced-ping-three")
    writer.write(b"".join(encode_frame(PingFrame(request_id=request_id)) for request_id in ping_ids))
    await asyncio.wait_for(writer.drain(), timeout=_IO_TIMEOUT_SECONDS)
    pongs = [await _read_frame(reader) for _ in ping_ids]
    assert all(isinstance(pong, PongFrame) for pong in pongs)
    assert [pong.request_id for pong in pongs] == list(ping_ids)


@pytest.mark.asyncio
async def test_stop_closes_authenticated_and_unhello_sockets_and_releases_port(
    notification_runtime,
):
    runtime = notification_runtime
    subscriber_reader, _, _ = await _open_raw_client(
        runtime,
        client_id="live-subscriber",
        subscriptions=("custom.shutdown",),
    )
    pending_reader, pending_writer = await asyncio.wait_for(
        asyncio.open_connection(runtime.host, runtime.port),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    runtime.raw_writers.append(pending_writer)
    await asyncio.sleep(0.05)

    await asyncio.wait_for(runtime.server.stop(), timeout=_IO_TIMEOUT_SECONDS)
    assert await asyncio.wait_for(subscriber_reader.read(1), timeout=_IO_TIMEOUT_SECONDS) == b""
    assert await asyncio.wait_for(pending_reader.read(1), timeout=_IO_TIMEOUT_SECONDS) == b""

    rebound_server = NotificationServer(runtime.config)
    runtime.servers.append(rebound_server)
    await asyncio.wait_for(rebound_server.start(), timeout=_IO_TIMEOUT_SECONDS)
    ready = await asyncio.wait_for(
        runtime.client("after-stop-rebind").ping(),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert ready.pid == os.getpid()
    await asyncio.wait_for(rebound_server.stop(), timeout=_IO_TIMEOUT_SECONDS)


@pytest.mark.asyncio
async def test_listen_reconnects_resubscribes_and_yields_new_ready(
    notification_runtime,
    monkeypatch,
):
    runtime = notification_runtime
    monkeypatch.setattr(notification_client_module, "NOTIFICATION_RECONNECT_MIN_SECONDS", 0.05)
    monkeypatch.setattr(notification_client_module, "NOTIFICATION_RECONNECT_MAX_SECONDS", 0.1)
    listener = runtime.listen(
        runtime.client("reconnecting-subscriber"),
        ("custom.reconnect",),
    )
    first_ready = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(first_ready, ReadyFrame)

    await asyncio.wait_for(runtime.server.stop(), timeout=_IO_TIMEOUT_SECONDS)
    restarted_server = NotificationServer(runtime.config)
    runtime.servers.append(restarted_server)
    await asyncio.wait_for(restarted_server.start(), timeout=_IO_TIMEOUT_SECONDS)

    second_ready = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(second_ready, ReadyFrame)
    assert second_ready.request_id != first_ready.request_id
    assert second_ready.server_id != first_ready.server_id

    notification = _notification("custom.reconnect", "after-reconnect")
    published = await asyncio.wait_for(
        runtime.client("reconnect-publisher").publish(notification),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert published.recipient_count == 1
    delivery = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert delivery == notification.model_copy(update={"source": "reconnect-publisher"})


@pytest.mark.asyncio
async def test_slow_subscriber_isolated_when_pending_frame_limit_is_reached(
    notification_runtime,
    monkeypatch,
):
    runtime = notification_runtime
    monkeypatch.setattr(notification_server_module, "NOTIFICATION_MAX_PENDING_FRAMES", 2)
    original_write_frame = notification_server_module.write_frame
    slow_ready_request_id = "slow-subscriber-ready"
    server_writers_by_ready_request = {}
    slow_event_write_started = asyncio.Event()
    release_slow_event_write = asyncio.Event()

    async def block_only_slow_event_write(writer, frame):
        if isinstance(frame, ReadyFrame):
            server_writers_by_ready_request[frame.request_id] = writer
        if isinstance(frame, EventFrame) and server_writers_by_ready_request.get(slow_ready_request_id) is writer:
            slow_event_write_started.set()
            await release_slow_event_write.wait()
        await original_write_frame(writer, frame)

    monkeypatch.setattr(
        notification_server_module,
        "write_frame",
        block_only_slow_event_write,
    )
    slow_reader, _, slow_ready = await _open_raw_client(
        runtime,
        client_id="slow-subscriber",
        subscriptions=("custom.pressure",),
        request_id=slow_ready_request_id,
    )
    fast_reader, _, _ = await _open_raw_client(
        runtime,
        client_id="fast-subscriber",
        subscriptions=("custom.pressure",),
        request_id="fast-subscriber-ready",
    )
    assert slow_ready.request_id == slow_ready_request_id

    publisher = runtime.client("pressure-publisher")
    notifications = [_notification("custom.pressure", f"pressure-{index}") for index in range(4)]
    recipient_counts = []

    try:
        first_published = await asyncio.wait_for(
            publisher.publish(notifications[0]),
            timeout=_IO_TIMEOUT_SECONDS,
        )
        recipient_counts.append(first_published.recipient_count)
        await asyncio.wait_for(
            slow_event_write_started.wait(),
            timeout=_IO_TIMEOUT_SECONDS,
        )

        for index, notification in enumerate(notifications[1:], start=1):
            published = await asyncio.wait_for(
                publisher.publish(notification),
                timeout=_IO_TIMEOUT_SECONDS,
            )
            recipient_counts.append(published.recipient_count)

        fast_deliveries = [await _read_frame(fast_reader) for _ in notifications]
        assert all(isinstance(delivery, EventFrame) for delivery in fast_deliveries)
        expected_deliveries = [notification.model_copy(update={"source": "pressure-publisher"}) for notification in notifications]
        assert [delivery.notification for delivery in fast_deliveries] == expected_deliveries
        assert recipient_counts == [2, 2, 2, 1]
        assert await asyncio.wait_for(slow_reader.read(1), timeout=_IO_TIMEOUT_SECONDS) == b""
    finally:
        release_slow_event_write.set()


@pytest.mark.asyncio
async def test_heartbeat_deadline_survives_events_and_reconnects(
    notification_runtime,
    monkeypatch,
):
    runtime = notification_runtime
    monkeypatch.setattr(notification_client_module, "NOTIFICATION_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    monkeypatch.setattr(notification_client_module, "NOTIFICATION_IO_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(notification_client_module, "NOTIFICATION_RECONNECT_MIN_SECONDS", 0.03)
    monkeypatch.setattr(notification_client_module, "NOTIFICATION_RECONNECT_MAX_SECONDS", 0.06)
    original_write_frame = notification_server_module.write_frame
    dropped_pong = asyncio.Event()

    async def discard_server_pongs(writer, frame):
        if isinstance(frame, PongFrame):
            dropped_pong.set()
            return
        await original_write_frame(writer, frame)

    monkeypatch.setattr(notification_server_module, "write_frame", discard_server_pongs)
    listener = runtime.listen(
        runtime.client("heartbeat-subscriber"),
        ("custom.heartbeat",),
    )
    first_ready = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(first_ready, ReadyFrame)
    stop_publishing = asyncio.Event()
    received_notifications = []
    publisher = runtime.client("heartbeat-publisher")

    async def publish_during_pending_pong():
        await asyncio.wait_for(dropped_pong.wait(), timeout=_IO_TIMEOUT_SECONDS)
        index = 0
        while not stop_publishing.is_set():
            await asyncio.wait_for(
                publisher.publish(_notification("custom.heartbeat", f"heartbeat-{index}")),
                timeout=_IO_TIMEOUT_SECONDS,
            )
            index += 1
            try:
                await asyncio.wait_for(stop_publishing.wait(), timeout=0.04)
            except TimeoutError:
                continue

    publishing_task = runtime.track_task(publish_during_pending_pong())
    second_ready = None
    try:
        async with asyncio.timeout(_IO_TIMEOUT_SECONDS * 2):
            while not isinstance(second_ready, ReadyFrame):
                item = await anext(listener)
                if isinstance(item, ReadyFrame):
                    second_ready = item
                else:
                    assert isinstance(item, Notification)
                    received_notifications.append(item)
    finally:
        stop_publishing.set()

    await asyncio.wait_for(publishing_task, timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(second_ready, ReadyFrame)
    assert second_ready.request_id != first_ready.request_id
    assert len(received_notifications) >= 2


@pytest.mark.asyncio
async def test_launcher_readiness_waits_for_report_then_authenticates(
    notification_runtime,
    tmp_path,
):
    runtime = notification_runtime
    direct_ready = await asyncio.wait_for(
        runtime.client("direct-readiness-check").ping(),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert direct_ready.pid == os.getpid()

    ready_path = tmp_path / "notification-ready"
    ready_path.touch()
    process = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
    await asyncio.wait_for(
        launcher._wait_for_notification_service(
            process,
            runtime.config,
            ready_path=ready_path,
        ),
        timeout=_IO_TIMEOUT_SECONDS,
    )


@pytest.mark.asyncio
async def test_launcher_readiness_accepts_worker_ready_report_before_ping(tmp_path):
    token = secrets.token_hex(32)
    ready_path = tmp_path / "notification-ready"
    env = os.environ.copy()
    env[NOTIFICATION_READY_FILE_ENV] = str(ready_path)
    process = subprocess.Popen(
        [sys.executable, "-c", _notification_worker_script(), token, "0"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    stdout_reader = asyncio.create_task(asyncio.to_thread(process.stdout.readline))

    try:
        output = await asyncio.wait_for(
            asyncio.shield(stdout_reader),
            timeout=_IO_TIMEOUT_SECONDS,
        )
        assert output
        server_info = json.loads(output)
        config = NotificationConfig(
            host=server_info["host"],
            port=server_info["port"],
            token=token,
        )
        await asyncio.wait_for(
            launcher._wait_for_notification_service(
                process,
                config,
                ready_path=ready_path,
            ),
            timeout=_IO_TIMEOUT_SECONDS,
        )

        assert ready_path.is_file()
        ready = await asyncio.wait_for(
            NotificationClient(config, "spawned-launcher-readiness").ping(),
            timeout=_IO_TIMEOUT_SECONDS,
        )
        assert ready.pid == server_info["pid"]
        assert process.poll() is None
    finally:
        await _cleanup_spawned_process(process, stdout_reader)


@pytest.mark.asyncio
async def test_launcher_readiness_rejects_existing_service_after_worker_bind_failure(
    notification_runtime,
    tmp_path,
):
    runtime = notification_runtime
    ready_path = tmp_path / "notification-ready"
    env = os.environ.copy()
    env[NOTIFICATION_READY_FILE_ENV] = str(ready_path)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            _notification_worker_script(),
            runtime.token,
            str(runtime.port),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )

    try:
        with pytest.raises(RuntimeError, match="exited during startup"):
            await asyncio.wait_for(
                launcher._wait_for_notification_service(
                    process,
                    runtime.config,
                    ready_path=ready_path,
                ),
                timeout=_IO_TIMEOUT_SECONDS,
            )
        assert not ready_path.exists()
        assert process.poll() is not None
        assert process.returncode != 0

        ready = await asyncio.wait_for(
            runtime.client("existing-center-after-bind-failure").ping(),
            timeout=_IO_TIMEOUT_SECONDS,
        )
        assert ready.pid == os.getpid()
    finally:
        await _cleanup_spawned_process(process)


@pytest.mark.asyncio
async def test_connection_limit_counts_clients_before_hello_and_recovers(
    notification_runtime,
    monkeypatch,
):
    runtime = notification_runtime
    monkeypatch.setattr(notification_server_module, "NOTIFICATION_MAX_CONNECTIONS", 1)
    monkeypatch.setattr(notification_server_module, "NOTIFICATION_IO_TIMEOUT_SECONDS", 1.0)

    first_reader, first_writer = await asyncio.wait_for(
        asyncio.open_connection(
            runtime.host,
            runtime.port,
            limit=NOTIFICATION_MAX_FRAME_BYTES,
        ),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    runtime.raw_writers.append(first_writer)
    await asyncio.sleep(0.05)

    second_reader, second_writer = await asyncio.wait_for(
        asyncio.open_connection(
            runtime.host,
            runtime.port,
            limit=NOTIFICATION_MAX_FRAME_BYTES,
        ),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    runtime.raw_writers.append(second_writer)
    busy = await _read_frame(second_reader)
    assert isinstance(busy, ErrorFrame)
    assert busy.code == "server_busy"
    assert await asyncio.wait_for(second_reader.read(1), timeout=_IO_TIMEOUT_SECONDS) == b""

    timed_out = await _read_frame(first_reader)
    assert isinstance(timed_out, ErrorFrame)
    assert timed_out.code == "hello_timeout"
    assert await asyncio.wait_for(first_reader.read(1), timeout=_IO_TIMEOUT_SECONDS) == b""

    ready = await asyncio.wait_for(
        runtime.client("after-connection-limit").ping(),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert ready.pid == os.getpid()


@pytest.mark.asyncio
async def test_source_binding_overflow_rejects_only_publisher(
    notification_runtime,
):
    runtime = notification_runtime
    topic = "custom.boundary"
    listener = runtime.listen(runtime.client("boundary-subscriber"), (topic,))
    ready = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(ready, ReadyFrame)

    publisher_id = "p" * 128
    publisher_reader, publisher_writer, _ = await _open_raw_client(
        runtime,
        client_id=publisher_id,
        request_id="boundary-publisher-hello",
    )
    notification = Notification(
        notification_id="fixed-boundary-notification",
        topic=topic,
        event=f"{topic}.created",
        source="x",
        occurred_at=datetime.now(UTC),
        scope=NotificationScope(uid="boundary-user", profile_id=73),
        resource=NotificationResource(type=f"{topic}.resource", id="boundary-resource"),
        correlation_id="fixed-boundary-correlation",
        data={"padding": ""},
    )
    request_id = "fixed-boundary-publish-request"
    empty_padding_frame = PublishFrame(request_id=request_id, notification=notification)
    padding_length = NOTIFICATION_MAX_FRAME_BYTES - len(encode_frame(empty_padding_frame))
    assert padding_length > 0

    notification = notification.model_copy(update={"data": {"padding": "p" * padding_length}})
    publish_frame = PublishFrame(request_id=request_id, notification=notification)
    encoded_publish = encode_frame(publish_frame)
    assert len(encoded_publish) == NOTIFICATION_MAX_FRAME_BYTES

    publisher_writer.write(encoded_publish)
    await asyncio.wait_for(publisher_writer.drain(), timeout=_IO_TIMEOUT_SECONDS)
    response = await _read_frame(publisher_reader)
    assert isinstance(response, ErrorFrame)
    assert response.code == "frame_too_large"
    assert response.request_id == request_id
    assert await asyncio.wait_for(publisher_reader.read(1), timeout=_IO_TIMEOUT_SECONDS) == b""

    followup = _notification(topic, "after-source-overflow")
    published = await asyncio.wait_for(
        runtime.client("normal-boundary-publisher").publish(followup),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert published.recipient_count == 1
    delivery = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(delivery, Notification)
    assert delivery == followup.model_copy(update={"source": "normal-boundary-publisher"})


@pytest.mark.parametrize("mismatched_id", ("request_id", "notification_id"))
@pytest.mark.asyncio
async def test_publish_rejects_mismatched_ack_without_retry(
    notification_runtime,
    monkeypatch,
    mismatched_id: str,
):
    runtime = notification_runtime
    topic = "custom.ack_validation"
    listener = runtime.listen(runtime.client("ack-subscriber"), (topic,))
    ready = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(ready, ReadyFrame)

    original_write_frame = notification_server_module.write_frame

    async def corrupt_published_ack(writer, frame):
        if isinstance(frame, PublishedFrame):
            frame = frame.model_copy(update={mismatched_id: f"wrong-{mismatched_id}"})
        await original_write_frame(writer, frame)

    monkeypatch.setattr(
        notification_server_module,
        "write_frame",
        corrupt_published_ack,
    )
    publisher = runtime.client("ack-publisher")
    notification = _notification(topic, f"bad-{mismatched_id}")
    try:
        with pytest.raises(NotificationProtocolError) as error:
            await asyncio.wait_for(
                publisher.publish(notification),
                timeout=_IO_TIMEOUT_SECONDS,
            )
        assert error.value.code == "unexpected_message"
    finally:
        monkeypatch.setattr(notification_server_module, "write_frame", original_write_frame)

    first_delivery = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    expected = notification.model_copy(update={"source": "ack-publisher"})
    assert first_delivery == expected

    sentinel = _notification(topic, f"sentinel-{mismatched_id}")
    published = await asyncio.wait_for(
        publisher.publish(sentinel),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert published.recipient_count == 1
    next_delivery = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert next_delivery == sentinel.model_copy(update={"source": "ack-publisher"})

    ready = await asyncio.wait_for(
        runtime.client("after-ack-mismatch").ping(),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert ready.pid == os.getpid()


@pytest.mark.asyncio
async def test_idle_listener_heartbeat_keeps_connection_ready_for_notifications(
    notification_runtime,
    monkeypatch,
):
    runtime = notification_runtime
    monkeypatch.setattr(notification_client_module, "NOTIFICATION_HEARTBEAT_INTERVAL_SECONDS", 0.05)
    original_write_frame = notification_server_module.write_frame
    pong_written = asyncio.Event()

    async def signal_after_real_pong(writer, frame):
        await original_write_frame(writer, frame)
        if isinstance(frame, PongFrame):
            pong_written.set()

    monkeypatch.setattr(
        notification_server_module,
        "write_frame",
        signal_after_real_pong,
    )
    topic = "custom.idle_heartbeat"
    listener = runtime.listen(runtime.client("idle-heartbeat-subscriber"), (topic,))
    ready = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(ready, ReadyFrame)

    waiting_delivery = runtime.track_task(anext(listener))
    try:
        await asyncio.wait_for(pong_written.wait(), timeout=_IO_TIMEOUT_SECONDS)

        notification = _notification(topic, "after-real-pong")
        published = await asyncio.wait_for(
            runtime.client("idle-heartbeat-publisher").publish(notification),
            timeout=_IO_TIMEOUT_SECONDS,
        )
        assert published.recipient_count == 1
        delivery = await asyncio.wait_for(waiting_delivery, timeout=_IO_TIMEOUT_SECONDS)
        assert isinstance(delivery, Notification)
        assert delivery == notification.model_copy(update={"source": "idle-heartbeat-publisher"})
    finally:
        if not waiting_delivery.done():
            waiting_delivery.cancel()
        await asyncio.wait_for(
            asyncio.gather(waiting_delivery, return_exceptions=True),
            timeout=_IO_TIMEOUT_SECONDS,
        )


@pytest.mark.asyncio
async def test_wildcard_listener_accepts_local_client_via_loopback(notification_runtime):
    runtime = notification_runtime
    server = NotificationServer(NotificationConfig(host="0.0.0.0", port=0, token=runtime.token))
    runtime.servers.append(server)
    await asyncio.wait_for(server.start(), timeout=_IO_TIMEOUT_SECONDS)

    listening_host, port = server.address
    assert listening_host == "0.0.0.0"
    client_config = NotificationConfig(host="0.0.0.0", port=port, token=runtime.token)
    assert client_config.server_host is None
    assert client_config.connect_host == "127.0.0.1"

    subscriber = NotificationClient(client_config, "wildcard-subscriber")
    listener = subscriber.listen(("custom.wildcard",))
    runtime.listeners.append(listener)
    ready = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(ready, ReadyFrame)

    notification = _notification("custom.wildcard", "wildcard-delivery")
    published = await asyncio.wait_for(
        NotificationClient(client_config, "wildcard-publisher").publish(notification),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert published.recipient_count == 1
    delivery = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert delivery == notification.model_copy(update={"source": "wildcard-publisher"})


@pytest.mark.asyncio
async def test_remote_server_host_override_authenticates_routes_and_reports_readiness(
    notification_runtime,
    monkeypatch,
):
    runtime = notification_runtime
    token = secrets.token_hex(32)
    monkeypatch.setattr(notification_config_module, "load_dotenv", lambda: None)
    monkeypatch.setattr(
        notification_config_module,
        "get_channel_encryption_key",
        lambda: pytest.fail("explicit notification token must not load system secrets"),
    )
    monkeypatch.setenv("NOTIFICATION_HOST", "0.0.0.0")
    monkeypatch.setenv("NOTIFICATION_SERVER_HOST", "")
    monkeypatch.setenv("NOTIFICATION_PORT", "1")
    monkeypatch.setenv("NOTIFICATION_TOKEN", token)

    server_config = replace(notification_config_module.load_notification_config(), port=0)
    assert server_config.host == "0.0.0.0"
    assert server_config.server_host is None
    assert server_config.port == 0
    assert server_config.token == token

    server = NotificationServer(server_config)
    runtime.servers.append(server)
    await asyncio.wait_for(server.start(), timeout=_IO_TIMEOUT_SECONDS)
    shared_port = server.address[1]
    monkeypatch.setenv("NOTIFICATION_PORT", str(shared_port))

    # Documentation IPs model distinct node-local addresses; test sockets stay local.
    monkeypatch.setenv("NOTIFICATION_HOST", "192.0.2.10")
    monkeypatch.setenv("NOTIFICATION_SERVER_HOST", "127.0.0.1")
    publisher_config = notification_config_module.load_notification_config()
    monkeypatch.setenv("NOTIFICATION_HOST", "192.0.2.20")
    subscriber_config = notification_config_module.load_notification_config()
    assert publisher_config.connect_host == subscriber_config.connect_host == "127.0.0.1"
    assert publisher_config.port == subscriber_config.port == shared_port
    assert publisher_config.token == subscriber_config.token == token

    subscriber = NotificationClient(subscriber_config, "remote-subscriber")
    listener = subscriber.listen(("custom.shared_endpoint",))
    runtime.listeners.append(listener)
    ready = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert isinstance(ready, ReadyFrame)

    wrong_token = ("0" if token[0] != "0" else "1") + token[1:]
    wrong_remote_config = replace(publisher_config, token=wrong_token)
    wrong_client = NotificationClient(wrong_remote_config, "wrong-token-client")
    with pytest.raises(NotificationProtocolError) as error:
        await asyncio.wait_for(wrong_client.ping(), timeout=_IO_TIMEOUT_SECONDS)
    assert error.value.code == "authentication_failed"

    publisher = NotificationClient(publisher_config, "remote-publisher")
    publisher_ready = await asyncio.wait_for(publisher.ping(), timeout=_IO_TIMEOUT_SECONDS)
    assert publisher_ready.pid == os.getpid()
    notification = _notification("custom.shared_endpoint", "shared-endpoint-delivery")
    published = await asyncio.wait_for(
        publisher.publish(notification),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    assert published.recipient_count == 1
    delivery = await asyncio.wait_for(anext(listener), timeout=_IO_TIMEOUT_SECONDS)
    assert delivery == notification.model_copy(update={"source": "remote-publisher"})

    await asyncio.wait_for(
        launcher._wait_for_notification_service(None, subscriber_config),
        timeout=_IO_TIMEOUT_SECONDS,
    )
    monkeypatch.setattr(launcher, "NOTIFICATION_START_TIMEOUT_SECONDS", 0.25)
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            launcher._wait_for_notification_service(None, wrong_remote_config),
            timeout=_IO_TIMEOUT_SECONDS,
        )
