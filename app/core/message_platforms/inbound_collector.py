from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Hashable
from dataclasses import dataclass, field

from app.core.constants import (
    ERR_INBOUND_MESSAGE_COLLECTOR_CLOSED,
    ERR_VALUE_MUST_BE_NON_NEGATIVE,
    ERR_VALUE_MUST_BE_POSITIVE,
    ERR_VALUE_MUST_NOT_BE_SHORTER_THAN,
)
from app.core.i18n import t


@dataclass
class _PendingGroup[MessageT]:
    message: MessageT
    first_received_at: float
    last_received_at: float
    revision: int = 0


@dataclass
class _KeyDispatch:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    tasks: set[asyncio.Task[None]] = field(default_factory=set)


def _validate_collection_periods(
    quiet_period_seconds: float,
    max_wait_seconds: float,
) -> None:
    if quiet_period_seconds < 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_NON_NEGATIVE, field="quiet_period_seconds"))
    if max_wait_seconds <= 0:
        raise ValueError(t(ERR_VALUE_MUST_BE_POSITIVE, field="max_wait_seconds"))
    if max_wait_seconds < quiet_period_seconds:
        raise ValueError(
            t(
                ERR_VALUE_MUST_NOT_BE_SHORTER_THAN,
                field="max_wait_seconds",
                other_field="quiet_period_seconds",
            )
        )


class InboundMessageCollector[KeyT: Hashable, MessageT]:
    def __init__(
        self,
        *,
        quiet_period_seconds: float,
        max_wait_seconds: float,
        merge: Callable[[MessageT, MessageT], MessageT],
        dispatch: Callable[[MessageT], Awaitable[None]],
    ) -> None:
        _validate_collection_periods(quiet_period_seconds, max_wait_seconds)

        self._quiet_period_seconds = quiet_period_seconds
        self._max_wait_seconds = max_wait_seconds
        self._merge = merge
        self._dispatch = dispatch
        self._pending: dict[KeyT, _PendingGroup[MessageT]] = {}
        self._timer_tasks: dict[KeyT, asyncio.Task[None]] = {}
        self._dispatch_by_key: dict[KeyT, _KeyDispatch] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def add(self, key: KeyT, message: MessageT) -> None:
        now = time.monotonic()
        async with self._lock:
            if self._closed:
                raise RuntimeError(t(ERR_INBOUND_MESSAGE_COLLECTOR_CLOSED))
            if self._quiet_period_seconds == 0:
                self._start_dispatch(key, message)
                return

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

    async def set_quiet_period(self, quiet_period_seconds: float) -> None:
        async with self._lock:
            if quiet_period_seconds == self._quiet_period_seconds:
                return
            _validate_collection_periods(quiet_period_seconds, self._max_wait_seconds)
            self._quiet_period_seconds = quiet_period_seconds
            timers = self._flush_pending(None)

        if timers:
            await asyncio.gather(*timers, return_exceptions=True)

    async def flush(self, key: KeyT | None = None) -> None:
        async with self._lock:
            timers = self._flush_pending(key)

        if timers:
            await asyncio.gather(*timers, return_exceptions=True)

    def _flush_pending(self, key: KeyT | None) -> list[asyncio.Task[None]]:
        keys = [key] if key is not None else list(self._pending)
        timers: list[asyncio.Task[None]] = []
        for pending_key in keys:
            pending = self._pending.pop(pending_key, None)
            if pending is not None:
                self._start_dispatch(pending_key, pending.message)
            timer = self._timer_tasks.pop(pending_key, None)
            if timer is not None and timer is not asyncio.current_task():
                timer.cancel()
                timers.append(timer)
        return timers

    async def flush_and_wait(self, key: KeyT) -> None:
        await self.flush(key)
        while True:
            async with self._lock:
                group = self._dispatch_by_key.get(key)
                if group is None:
                    return
                tasks = tuple(task for task in group.tasks if not task.done())
            if not tasks:
                return
            await asyncio.gather(*tasks, return_exceptions=True)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True

        await self.flush()
        tasks = tuple(task for group in self._dispatch_by_key.values() for task in group.tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

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

            self._start_dispatch(key, message)
        except asyncio.CancelledError:
            raise

    def _start_dispatch(self, key: KeyT, message: MessageT) -> None:
        group = self._dispatch_by_key.setdefault(key, _KeyDispatch())
        task = asyncio.create_task(self._dispatch_in_order(group, message))
        group.tasks.add(task)
        task.add_done_callback(lambda completed: self._complete_dispatch(key, group, completed))

    async def _dispatch_in_order(self, group: _KeyDispatch, message: MessageT) -> None:
        async with group.lock:
            await self._dispatch(message)

    def _complete_dispatch(self, key: KeyT, group: _KeyDispatch, task: asyncio.Task[None]) -> None:
        group.tasks.discard(task)
        if not group.tasks and self._dispatch_by_key.get(key) is group:
            self._dispatch_by_key.pop(key)
        if not task.cancelled():
            task.exception()
