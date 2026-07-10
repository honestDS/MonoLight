import asyncio
import json
from collections import defaultdict
from typing import Any

from app.core.crud.session_event import session_event_crud
from app.core.log import get_logger
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

SESSION_EVENT_POLL_INTERVAL_SECONDS = 0.25
SESSION_EVENT_FETCH_LIMIT = 100
SESSION_EVENT_CLEANUP_INTERVAL_SECONDS = 60 * 60


class SessionNotifier:
    def __init__(self) -> None:
        self._queues: dict[tuple[str, str], set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_event_id = 0
        self._next_cleanup_at = 0.0

    async def start(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            return
        async with AsyncSessionLocal() as db:
            self._last_event_id = await session_event_crud.get_latest_id(db)
        self._stop_event.clear()
        self._next_cleanup_at = 0.0
        self._poll_task = asyncio.create_task(self._poll_events())

    async def stop(self) -> None:
        self._stop_event.set()
        poll_task = self._poll_task
        self._poll_task = None
        if poll_task is not None:
            await poll_task

    async def register(self, uid: str, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._queues[(uid, session_id)].add(queue)

    async def unregister(self, uid: str, session_id: str, queue: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            queues = self._queues.get((uid, session_id))
            if not queues:
                return
            queues.discard(queue)
            if not queues:
                self._queues.pop((uid, session_id), None)

    async def notify(self, uid: str, session_id: str, event: dict[str, Any]) -> None:
        normalized_event = json.loads(json.dumps(event, ensure_ascii=False, default=str))
        async with AsyncSessionLocal() as db:
            await session_event_crud.publish(db, uid=uid, session_id=session_id, event=normalized_event)

    async def _poll_events(self) -> None:
        while not self._stop_event.is_set():
            try:
                await self._cleanup_events_if_due()
                async with AsyncSessionLocal() as db:
                    events = await session_event_crud.list_after_id(db, after_id=self._last_event_id, limit=SESSION_EVENT_FETCH_LIMIT)
                for item in events:
                    if item.id is None:
                        continue
                    self._last_event_id = item.id
                    await self._notify_local(item.uid, item.session_id, item.event)
                if events:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Failed to poll proactive session events")
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=SESSION_EVENT_POLL_INTERVAL_SECONDS)
            except TimeoutError:
                pass

    async def _cleanup_events_if_due(self) -> None:
        loop_time = asyncio.get_running_loop().time()
        if loop_time < self._next_cleanup_at:
            return
        async with AsyncSessionLocal() as db:
            await session_event_crud.cleanup_expired(db)
        self._next_cleanup_at = loop_time + SESSION_EVENT_CLEANUP_INTERVAL_SECONDS

    async def _notify_local(self, uid: str, session_id: str, event: dict[str, Any]) -> None:
        async with self._lock:
            queues = list(self._queues.get((uid, session_id), set()))

        for queue in queues:
            try:
                queue.put_nowait(event)
            except Exception:
                logger.bind(uid=uid, session_id=session_id).warning("Failed to enqueue proactive session event", exc_info=True)


session_notifier = SessionNotifier()
