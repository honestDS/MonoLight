"""Async client for a local or shared TCP notification center."""

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager

from app.core.constants import (
    NOTIFICATION_HEARTBEAT_INTERVAL_SECONDS,
    NOTIFICATION_IO_TIMEOUT_SECONDS,
    NOTIFICATION_MAX_FRAME_BYTES,
    NOTIFICATION_RECONNECT_MAX_SECONDS,
    NOTIFICATION_RECONNECT_MIN_SECONDS,
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


class NotificationClient:
    """A stateless client bound to one notification-center identity."""

    __slots__ = ("_config", "_client_id")

    def __init__(self, config: NotificationConfig, client_id: str) -> None:
        HelloFrame(client_id=client_id, token=config.token)
        self._config = config
        self._client_id = client_id

    async def ping(self) -> ReadyFrame:
        """Verify the center is responsive and return its handshake identity."""
        async with self._connection(()) as (reader, writer, ready):
            ping = PingFrame()
            await self._send_frame(writer, ping)
            response = await self._read_frame(reader, NOTIFICATION_IO_TIMEOUT_SECONDS)
            self._require_pong(response, ping.request_id)
            return ready

    async def publish(self, notification: Notification) -> PublishedFrame:
        """Publish once; recipient_count only confirms center-side enqueueing.

        Failed or ambiguous acknowledgements are propagated without retrying or
        caching. The caller remains responsible for database-based recovery.
        """
        bound_notification = notification.model_copy(update={"source": self._client_id})
        publish = PublishFrame(notification=bound_notification)
        encoded_publish = encode_frame(publish)

        async with self._connection(()) as (reader, writer, _):
            writer.write(encoded_publish)
            await asyncio.wait_for(
                writer.drain(),
                timeout=NOTIFICATION_IO_TIMEOUT_SECONDS,
            )
            response = await self._read_frame(reader, NOTIFICATION_IO_TIMEOUT_SECONDS)
            return self._require_published(
                response,
                publish.request_id,
                bound_notification.notification_id,
            )

    async def listen(
        self,
        topics: Iterable[str],
    ) -> AsyncIterator[ReadyFrame | Notification]:
        """Yield readiness and notifications from a reconnecting subscription.

        Every ReadyFrame, including one yielded after reconnecting, requires
        consumers to recheck durable database state. Notifications are wake-up
        hints and are not a replacement for that recovery path.
        """
        subscriptions = tuple(topics)
        self._new_hello(subscriptions)
        reconnect_delay = NOTIFICATION_RECONNECT_MIN_SECONDS

        while True:
            try:
                async with self._connection(subscriptions) as (reader, writer, ready):
                    reconnect_delay = NOTIFICATION_RECONNECT_MIN_SECONDS
                    yield ready
                    async for notification in self._listen_connection(reader, writer):
                        yield notification
            except asyncio.CancelledError:
                raise
            except (
                EOFError,
                OSError,
                TimeoutError,
                NotificationProtocolError,
            ):
                await asyncio.sleep(reconnect_delay)
                reconnect_delay = min(
                    reconnect_delay * 2,
                    NOTIFICATION_RECONNECT_MAX_SECONDS,
                )

    @asynccontextmanager
    async def _connection(
        self,
        subscriptions: tuple[str, ...],
    ) -> AsyncIterator[tuple[asyncio.StreamReader, asyncio.StreamWriter, ReadyFrame]]:
        hello = self._new_hello(subscriptions)
        writer: asyncio.StreamWriter | None = None

        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(
                    self._config.connect_host,
                    self._config.port,
                    limit=NOTIFICATION_MAX_FRAME_BYTES,
                ),
                timeout=NOTIFICATION_IO_TIMEOUT_SECONDS,
            )
            await self._send_frame(writer, hello)
            response = await self._read_frame(reader, NOTIFICATION_IO_TIMEOUT_SECONDS)
            ready = self._require_ready(response, hello.request_id)
            yield reader, writer, ready
        finally:
            if writer is not None:
                writer.close()
                try:
                    await asyncio.wait_for(
                        writer.wait_closed(),
                        timeout=NOTIFICATION_IO_TIMEOUT_SECONDS,
                    )
                except (OSError, TimeoutError):
                    pass

    async def _listen_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> AsyncIterator[Notification]:
        loop = asyncio.get_running_loop()
        next_ping_at = loop.time() + NOTIFICATION_HEARTBEAT_INTERVAL_SECONDS
        pending_ping: PingFrame | None = None
        pong_deadline = 0.0

        while True:
            now = loop.time()
            if pending_ping is None and now >= next_ping_at:
                pending_ping = PingFrame()
                await self._send_frame(writer, pending_ping)
                pong_deadline = loop.time() + NOTIFICATION_IO_TIMEOUT_SECONDS
                continue

            if pending_ping is None:
                timeout = next_ping_at - now
            else:
                timeout = pong_deadline - now

            if timeout <= 0 and pending_ping is None:
                continue
            if timeout <= 0:
                raise TimeoutError

            try:
                frame = await self._read_frame(reader, timeout)
            except TimeoutError:
                if pending_ping is not None:
                    raise
                continue

            self._raise_for_error(frame)
            if isinstance(frame, EventFrame):
                yield frame.notification
                continue
            if pending_ping is not None and isinstance(frame, PongFrame) and frame.request_id == pending_ping.request_id:
                pending_ping = None
                pong_deadline = 0.0
                next_ping_at = loop.time() + NOTIFICATION_HEARTBEAT_INTERVAL_SECONDS
                continue
            raise NotificationProtocolError("unexpected_message")

    def _new_hello(self, subscriptions: tuple[str, ...]) -> HelloFrame:
        return HelloFrame(
            client_id=self._client_id,
            token=self._config.token,
            subscriptions=subscriptions,
        )

    async def _send_frame(
        self,
        writer: asyncio.StreamWriter,
        frame: HelloFrame | PingFrame,
    ) -> None:
        await asyncio.wait_for(
            write_frame(writer, frame),
            timeout=NOTIFICATION_IO_TIMEOUT_SECONDS,
        )

    async def _read_frame(
        self,
        reader: asyncio.StreamReader,
        timeout: float,
    ) -> NotificationFrame:
        return await asyncio.wait_for(read_frame(reader), timeout=timeout)

    @staticmethod
    def _raise_for_error(frame: NotificationFrame) -> None:
        if isinstance(frame, ErrorFrame):
            raise NotificationProtocolError(frame.code)

    @classmethod
    def _require_ready(cls, frame: NotificationFrame, request_id: str) -> ReadyFrame:
        cls._raise_for_error(frame)
        if not isinstance(frame, ReadyFrame):
            raise NotificationProtocolError("unexpected_message")
        if frame.request_id != request_id:
            raise NotificationProtocolError("unexpected_message")
        return frame

    @classmethod
    def _require_pong(cls, frame: NotificationFrame, request_id: str) -> None:
        cls._raise_for_error(frame)
        if not isinstance(frame, PongFrame):
            raise NotificationProtocolError("unexpected_message")
        if frame.request_id != request_id:
            raise NotificationProtocolError("unexpected_message")

    @classmethod
    def _require_published(
        cls,
        frame: NotificationFrame,
        request_id: str,
        notification_id: str,
    ) -> PublishedFrame:
        cls._raise_for_error(frame)
        if not isinstance(frame, PublishedFrame):
            raise NotificationProtocolError("unexpected_message")
        if frame.request_id != request_id:
            raise NotificationProtocolError("unexpected_message")
        if frame.notification_id != notification_id:
            raise NotificationProtocolError("unexpected_message")
        return frame


__all__ = ["NotificationClient"]
