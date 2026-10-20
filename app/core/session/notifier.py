import asyncio
import hashlib
import json
from collections import defaultdict
from typing import Any

from app.core.crud.session.event import session_event_crud
from app.core.log import get_logger
from app.providers.database import AsyncSessionLocal

logger = get_logger(__name__)

SESSION_EVENT_POLL_INTERVAL_SECONDS = 0.25
SESSION_EVENT_FETCH_LIMIT = 100
SESSION_EVENT_CLEANUP_INTERVAL_SECONDS = 60 * 60


def _resolve_session_event_identity(event: dict[str, Any]) -> dict[str, Any]:
    event_type = str(event.get("type") or "")
    event_source = str(event.get("source") or "")
    if event.get("event_id") is not None:
        return {"event_id": event["event_id"], "type": event_type}
    if event_source == "background_task" and event.get("background_task_id") is not None:
        return {"background_task_id": event["background_task_id"], "type": event_type}
    if event_source == "scheduled_task" and event.get("trigger_message_id") is not None:
        return {"trigger_message_id": event["trigger_message_id"], "type": event_type}
    return {"event": event}


def build_session_event_dedupe_key(uid: str, session_id: str, source: str, event: dict[str, Any]) -> str:
    payload = json.dumps(
        {
            "uid": uid,
            "session_id": session_id,
            "source": source,
            "identity": _resolve_session_event_identity(event),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


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

    async def notify(
        self,
        uid: str,
        session_id: str,
        event: dict[str, Any],
        *,
        dedupe_key: str | None = None,
    ) -> bool:
        normalized_event = json.loads(json.dumps(event, ensure_ascii=False, default=str))
        resolved_dedupe_key = dedupe_key or build_session_event_dedupe_key(uid, session_id, "session", normalized_event)
        async with AsyncSessionLocal() as db:
            _item, created = await session_event_crud.publish(
                db,
                dedupe_key=resolved_dedupe_key,
                uid=uid,
                session_id=session_id,
                event=normalized_event,
            )
        return created

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
                    await self._notify_local(item.uid, item.session_id, item.event, event_sequence_no=item.id)
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

    async def _notify_local(self, uid: str, session_id: str, event: dict[str, Any], *, event_sequence_no: int) -> None:
        async with self._lock:
            queues = list(self._queues.get((uid, session_id), set()))

        outbound_event = {
            **event,
            "event_sequence_no": event_sequence_no,
        }
        for queue in queues:
            try:
                queue.put_nowait(outbound_event)
            except Exception:
                logger.bind(uid=uid, session_id=session_id).warning("Failed to enqueue proactive session event", exc_info=True)


session_notifier = SessionNotifier()
