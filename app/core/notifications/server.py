"""In-process state and TCP transport for notification pub/sub."""

import asyncio
import hmac
import os
from dataclasses import dataclass, field
from functools import partial
from uuid import uuid4

from app.core.constants import (
    NOTIFICATION_IO_TIMEOUT_SECONDS,
    NOTIFICATION_MAX_CONNECTIONS,
    NOTIFICATION_MAX_FRAME_BYTES,
    NOTIFICATION_MAX_PENDING_FRAMES,
)
from app.core.notifications.config import NotificationConfig
from app.core.notifications.protocol import (
    ErrorFrame,
    EventFrame,
    HelloFrame,
    Notification,
    NotificationFrame,
    NotificationProtocolError,
    PingFrame,
    PongFrame,
    PublishedFrame,
    PublishFrame,
    ReadyFrame,
    encode_frame,
    read_frame,
    write_frame,
)


@dataclass(eq=False, slots=True)
class _Peer:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    client_id: str
    subscriptions: frozenset[str]
    outgoing: asyncio.Queue[NotificationFrame] = field(default_factory=lambda: asyncio.Queue(maxsize=NOTIFICATION_MAX_PENDING_FRAMES))
    sender_task: asyncio.Task[None] | None = None
    closed: bool = False


class NotificationServer:
    """Authenticated TCP pub/sub center for trusted networks.

    A PublishedFrame acknowledges center acceptance and forwarding attempts;
    it does not indicate that a worker has processed the notification.
    """

    def __init__(self, config: NotificationConfig) -> None:
        self._config = config
        self._server: asyncio.Server | None = None
        self._server_id: str | None = None
        self._generation = 0
        self._accepting = False
        self._lifecycle_lock = asyncio.Lock()
        self._connection_tasks: set[asyncio.Task[None]] = set()
        self._writers: set[asyncio.StreamWriter] = set()
        self._peers: set[_Peer] = set()
        self._subscribers: dict[str, set[_Peer]] = {}

    @property
    def address(self) -> tuple[str, int]:
        server = self._server
        if server is None or not server.sockets:
            raise RuntimeError("notification_server_not_started")

        socket_address = server.sockets[0].getsockname()
        return str(socket_address[0]), int(socket_address[1])

    async def start(self) -> None:
        async with self._lifecycle_lock:
            if self._server is not None:
                raise RuntimeError("notification_server_already_started")

            self._generation += 1
            generation = self._generation
            self._server_id = uuid4().hex
            self._accepting = True
            try:
                self._server = await asyncio.start_server(
                    partial(self._accept_connection, generation),
                    self._config.host,
                    self._config.port,
                    limit=NOTIFICATION_MAX_FRAME_BYTES,
                )
            finally:
                if self._server is None:
                    self._accepting = False
                    self._generation += 1
                    self._server_id = None

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._accepting = False
            self._generation += 1
            server = self._server
            self._server = None
            self._server_id = None

            if server is not None:
                server.close()

            writers = tuple(self._writers)
            for writer in writers:
                writer.close()

            for peer in tuple(self._peers):
                self._drop_peer(peer)

            tasks = tuple(self._connection_tasks)
            for task in tasks:
                task.cancel()

            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

            writers = tuple(self._writers)
            if writers:
                await asyncio.gather(*(self._wait_writer_closed(writer) for writer in writers))

            if server is not None:
                await server.wait_closed()

            self._connection_tasks.clear()
            self._writers.clear()
            self._peers.clear()
            self._subscribers.clear()

    async def __aenter__(self) -> "NotificationServer":
        await self.start()
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        await self.stop()

    def _accept_connection(
        self,
        generation: int,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        if generation != self._generation or not self._accepting:
            writer.close()
            return

        self._writers.add(writer)
        if len(self._writers) > NOTIFICATION_MAX_CONNECTIONS:
            self._write_busy_and_close(writer)
            self._writers.discard(writer)
            return

        task = asyncio.create_task(self._handle_connection(reader, writer))
        self._connection_tasks.add(task)
        task.add_done_callback(self._connection_tasks.discard)

    @staticmethod
    def _write_busy_and_close(writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(encode_frame(ErrorFrame(code="server_busy")))
        except (ConnectionError, OSError, NotificationProtocolError):
            pass
        finally:
            writer.close()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        peer: _Peer | None = None
        try:
            hello = await self._authenticate(reader, writer)
            if hello is None or not self._accepting:
                return

            server_id = self._server_id
            if server_id is None:
                return

            peer = self._register_peer(reader, writer, hello, server_id)
            await self._serve_peer(peer)
        except (ConnectionError, OSError, EOFError, NotificationProtocolError):
            return
        finally:
            if peer is not None:
                self._drop_peer(peer)
                await self._stop_sender(peer)
            writer.close()
            await self._wait_writer_closed(writer)
            self._writers.discard(writer)

    async def _authenticate(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> HelloFrame | None:
        try:
            frame = await asyncio.wait_for(
                read_frame(reader),
                timeout=NOTIFICATION_IO_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            await self._send_initial_error(writer, "hello_timeout")
            return None
        except NotificationProtocolError as error:
            await self._send_initial_error(writer, error.code)
            return None
        except EOFError:
            return None

        if not isinstance(frame, HelloFrame):
            await self._send_initial_error(
                writer,
                "unexpected_message",
                getattr(frame, "request_id", None),
            )
            return None

        if not hmac.compare_digest(frame.token, self._config.token):
            await self._send_initial_error(
                writer,
                "authentication_failed",
                frame.request_id,
            )
            return None

        return frame

    @staticmethod
    async def _send_initial_error(
        writer: asyncio.StreamWriter,
        code: str,
        request_id: str | None = None,
    ) -> None:
        await write_frame(writer, ErrorFrame(code=code, request_id=request_id))

    def _register_peer(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        hello: HelloFrame,
        server_id: str,
    ) -> _Peer:
        subscriptions = frozenset(hello.subscriptions)
        peer = _Peer(
            reader=reader,
            writer=writer,
            client_id=hello.client_id,
            subscriptions=subscriptions,
        )
        self._peers.add(peer)
        for topic in subscriptions:
            self._subscribers.setdefault(topic, set()).add(peer)

        peer.sender_task = asyncio.create_task(self._send_loop(peer))
        self._queue_frame(
            peer,
            ReadyFrame(
                request_id=hello.request_id,
                server_id=server_id,
                pid=os.getpid(),
            ),
        )
        return peer

    async def _serve_peer(self, peer: _Peer) -> None:
        while True:
            try:
                frame = await read_frame(peer.reader)
            except NotificationProtocolError as error:
                await self._send_authenticated_error(peer, error.code)
                return
            except EOFError:
                return

            if isinstance(frame, PingFrame):
                response = PongFrame(request_id=frame.request_id)
            elif isinstance(frame, PublishFrame):
                notification = frame.notification.model_copy(update={"source": peer.client_id})
                try:
                    recipient_count = self._broadcast(notification)
                except NotificationProtocolError as error:
                    await self._send_authenticated_error(
                        peer,
                        error.code,
                        frame.request_id,
                    )
                    return
                response = PublishedFrame(
                    request_id=frame.request_id,
                    notification_id=notification.notification_id,
                    recipient_count=recipient_count,
                )
            else:
                await self._send_authenticated_error(
                    peer,
                    "unexpected_message",
                    getattr(frame, "request_id", None),
                )
                return

            if not self._queue_frame(peer, response):
                return

    async def _send_authenticated_error(
        self,
        peer: _Peer,
        code: str,
        request_id: str | None = None,
    ) -> None:
        await self._stop_sender(peer)
        if peer.writer.is_closing():
            return
        await write_frame(peer.writer, ErrorFrame(code=code, request_id=request_id))

    async def _stop_sender(self, peer: _Peer) -> None:
        sender = peer.sender_task
        if sender is None:
            return
        if not sender.done():
            sender.cancel()
        await asyncio.gather(sender, return_exceptions=True)

    async def _send_loop(self, peer: _Peer) -> None:
        cancelled = False
        try:
            while True:
                frame = await peer.outgoing.get()
                try:
                    await write_frame(peer.writer, frame)
                finally:
                    peer.outgoing.task_done()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if not cancelled:
                self._drop_peer(peer)

    def _queue_frame(self, peer: _Peer, frame: NotificationFrame) -> bool:
        sender = peer.sender_task
        if peer.closed or peer.writer.is_closing() or sender is None or sender.done():
            self._drop_peer(peer)
            return False

        try:
            peer.outgoing.put_nowait(frame)
        except asyncio.QueueFull:
            self._drop_peer(peer)
            return False

        return True

    def _broadcast(self, notification: Notification) -> int:
        event = EventFrame(notification=notification)
        encode_frame(event)

        subscribers = self._subscribers.get(notification.topic)
        if not subscribers:
            return 0

        recipient_count = 0
        for peer in tuple(subscribers):
            if not self._queue_frame(peer, event):
                continue
            recipient_count += 1
        return recipient_count

    def _drop_peer(self, peer: _Peer) -> None:
        if peer.closed:
            return

        peer.closed = True
        self._peers.discard(peer)
        for topic in peer.subscriptions:
            subscribers = self._subscribers.get(topic)
            if subscribers is None:
                continue
            subscribers.discard(peer)
            if subscribers:
                continue
            del self._subscribers[topic]

        peer.writer.close()
        sender = peer.sender_task
        if sender is None or sender.done() or sender is asyncio.current_task():
            return
        sender.cancel()

    @staticmethod
    async def _wait_writer_closed(writer: asyncio.StreamWriter) -> None:
        try:
            await asyncio.wait_for(
                writer.wait_closed(),
                timeout=NOTIFICATION_IO_TIMEOUT_SECONDS,
            )
        except (TimeoutError, ConnectionError, OSError):
            try:
                writer.transport.abort()
            except (ConnectionError, OSError):
                return
            return


__all__ = ["NotificationServer"]
