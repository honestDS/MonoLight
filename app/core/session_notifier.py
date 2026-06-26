import asyncio
from collections import defaultdict
from typing import Any

from app.core.log import get_logger

logger = get_logger(__name__)


class SessionNotifier:
    def __init__(self) -> None:
        self._queues: dict[tuple[str, str], set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

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
        async with self._lock:
            queues = list(self._queues.get((uid, session_id), set()))

        for queue in queues:
            try:
                queue.put_nowait(event)
            except Exception:
                logger.bind(uid=uid, session_id=session_id).warning("Failed to enqueue proactive session event", exc_info=True)


session_notifier = SessionNotifier()
