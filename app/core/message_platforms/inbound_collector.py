from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass


@dataclass
class _PendingGroup[MessageT]:
    message: MessageT
    first_received_at: float
    last_received_at: float
    revision: int = 0


class InboundMessageCollector[KeyT: Hashable, MessageT]:
    def __init__(
        self,
        *,
        quiet_period_seconds: float,
        max_wait_seconds: float,
        merge: Callable[[MessageT, MessageT], MessageT],
        dispatch: Callable[[MessageT], Awaitable[None]],
    ) -> None:
        if quiet_period_seconds < 0:
            raise ValueError("quiet_period_seconds must not be negative")
        if max_wait_seconds <= 0:
            raise ValueError("max_wait_seconds must be positive")
        if max_wait_seconds < quiet_period_seconds:
            raise ValueError("max_wait_seconds must not be shorter than quiet_period_seconds")

        self._quiet_period_seconds = quiet_period_seconds
        self._max_wait_seconds = max_wait_seconds
        self._merge = merge
        self._dispatch = dispatch
        self._pending: dict[KeyT, _PendingGroup[MessageT]] = {}
        self._timer_tasks: dict[KeyT, asyncio.Task[None]] = {}
        self._dispatch_tasks: set[asyncio.Task[None]] = set()
        self._lock = asyncio.Lock()
        self._closed = False

    async def add(self, key: KeyT, message: MessageT) -> None:
        now = time.monotonic()
        async with self._lock:
            if self._closed:
                raise RuntimeError("inbound message collector is closed")

            pending = self._pending.get(key)
            if pending is None:
                pending = _PendingGroup(
                    message=message,
                    first_received_at=now,
                    last_received_at=now,
                )
                self._pending[key] = pending
            else:
                pending.message = self._merge(pending.message, message)
                pending.last_received_at = now
                pending.revision += 1

            timer = self._timer_tasks.get(key)
            if timer is not None:
                timer.cancel()
            self._timer_tasks[key] = asyncio.create_task(self._wait_and_dispatch(key, pending.revision))

    async def flush(self, key: KeyT | None = None) -> None:
        async with self._lock:
            keys = [key] if key is not None else list(self._pending)
            messages: list[MessageT] = []
            timers: list[asyncio.Task[None]] = []
            for pending_key in keys:
                pending = self._pending.pop(pending_key, None)
                if pending is not None:
                    messages.append(pending.message)
                timer = self._timer_tasks.pop(pending_key, None)
                if timer is not None and timer is not asyncio.current_task():
                    timer.cancel()
                    timers.append(timer)

        if timers:
            await asyncio.gather(*timers, return_exceptions=True)
        for message in messages:
            self._start_dispatch(message)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True

        await self.flush()
        if self._dispatch_tasks:
            await asyncio.gather(*tuple(self._dispatch_tasks), return_exceptions=True)

    async def _wait_and_dispatch(self, key: KeyT, revision: int) -> None:
        try:
            async with self._lock:
                pending = self._pending.get(key)
                if pending is None or pending.revision != revision:
                    return
                now = time.monotonic()
                quiet_deadline = pending.last_received_at + self._quiet_period_seconds
                max_deadline = pending.first_received_at + self._max_wait_seconds
                delay = max(0.0, min(quiet_deadline, max_deadline) - now)

            await asyncio.sleep(delay)

            async with self._lock:
                pending = self._pending.get(key)
                if pending is None or pending.revision != revision:
                    return
                now = time.monotonic()
                quiet_elapsed = now - pending.last_received_at >= self._quiet_period_seconds
                max_wait_elapsed = now - pending.first_received_at >= self._max_wait_seconds
                if not quiet_elapsed and not max_wait_elapsed:
                    self._timer_tasks[key] = asyncio.create_task(self._wait_and_dispatch(key, revision))
                    return
                message = self._pending.pop(key).message
                self._timer_tasks.pop(key, None)

            self._start_dispatch(message)
        except asyncio.CancelledError:
            raise

    def _start_dispatch(self, message: MessageT) -> None:
        task = asyncio.create_task(self._dispatch(message))
        self._dispatch_tasks.add(task)
        task.add_done_callback(self._complete_dispatch)

    def _complete_dispatch(self, task: asyncio.Task[None]) -> None:
        self._dispatch_tasks.discard(task)
        if not task.cancelled():
            task.exception()


