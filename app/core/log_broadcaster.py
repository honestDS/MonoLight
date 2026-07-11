import asyncio
import json
import sys
import time
import traceback
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import WebSocket

from app.core.crud.log import system_log_crud
from app.providers.database import AsyncSessionLocal

LOG_POLL_INTERVAL_SECONDS = 0.1
LOG_FETCH_LIMIT = 200
LOG_POLL_ERROR_REPORT_INTERVAL_SECONDS = 30.0
LOG_SEND_QUEUE_SIZE = 500


@dataclass
class LogConnection:
    after_id: int
    queue: asyncio.Queue[tuple[str, int | None]]
    sender_task: asyncio.Task


class LogBroadcaster:
    """
    日志广播管理器，负责管理 WebSocket 连接并将日志实时推送给订阅者。
    """

    def __init__(self):
        self.active_connections: dict[WebSocket, LogConnection] = {}
        self.lock = asyncio.Lock()
        self._poll_task: asyncio.Task | None = None
        self._stop_event = asyncio.Event()
        self._last_log_id = 0
        self._last_poll_error_report_at: float | None = None

    async def start(self) -> None:
        if self._poll_task is not None and not self._poll_task.done():
            return
        async with AsyncSessionLocal() as db:
            self._last_log_id = await system_log_crud.get_latest_id(db)
        self._stop_event.clear()
        self._poll_task = asyncio.create_task(self._poll_logs())

    async def stop(self) -> None:
        self._stop_event.set()
        poll_task = self._poll_task
        self._poll_task = None
        if poll_task is not None:
            await poll_task
        async with self.lock:
            connections = list(self.active_connections.items())
            self.active_connections.clear()
        for _websocket, connection in connections:
            connection.sender_task.cancel()
        if connections:
            await asyncio.gather(*(connection.sender_task for _websocket, connection in connections), return_exceptions=True)

    async def connect(self, websocket: WebSocket, initialize: Callable[[], Awaitable[tuple[int, dict]]]) -> None:
        """
        接受连接，在广播锁内发送历史快照，并以快照游标注册实时订阅。
        """
        await websocket.accept()
        async with self.lock:
            latest_history_id, history_payload = await initialize()
            queue: asyncio.Queue[tuple[str, int | None]] = asyncio.Queue(maxsize=LOG_SEND_QUEUE_SIZE)
            queue.put_nowait((json.dumps(history_payload, ensure_ascii=False), None))
            sender_task = asyncio.create_task(self._send_messages(websocket, queue))
            self.active_connections[websocket] = LogConnection(after_id=latest_history_id, queue=queue, sender_task=sender_task)

    async def disconnect(self, websocket: WebSocket):
        """
        移除连接
        """
        connection = await self._remove_connection(websocket)
        if connection is None or connection.sender_task is asyncio.current_task():
            return
        connection.sender_task.cancel()
        await asyncio.gather(connection.sender_task, return_exceptions=True)

    async def broadcast(self, log_entry: dict, *, log_id: int | None = None):
        """
        异步推送日志给所有订阅者
        """
        message = json.dumps(log_entry, ensure_ascii=False)

        async with self.lock:
            overflowed = []
            for websocket, connection in self.active_connections.items():
                if log_id is not None and log_id <= connection.after_id:
                    continue
                try:
                    connection.queue.put_nowait((message, log_id))
                except asyncio.QueueFull:
                    overflowed.append(websocket)

        for websocket in overflowed:
            await self.disconnect(websocket)

    async def _poll_logs(self) -> None:
        while not self._stop_event.is_set():
            try:
                async with AsyncSessionLocal() as db:
                    logs = await system_log_crud.list_after_id(db, after_id=self._last_log_id, limit=LOG_FETCH_LIMIT)
                for log in logs:
                    if log.id is None:
                        continue
                    self._last_log_id = log.id
                    try:
                        extra = json.loads(log.extra) if log.extra else {}
                    except json.JSONDecodeError:
                        extra = {}
                    await self.broadcast(
                        {
                            "timestamp": log.created_at.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3],
                            "level": log.level,
                            "module": log.module,
                            "message": log.message,
                            "uid": log.uid,
                            "session_id": log.session_id,
                            "extra": extra,
                        },
                        log_id=log.id,
                    )
                if logs:
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._report_poll_error(exc)
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=LOG_POLL_INTERVAL_SECONDS)
            except TimeoutError:
                pass

    def _report_poll_error(self, exc: Exception) -> None:
        now = time.monotonic()
        if self._last_poll_error_report_at is not None and now - self._last_poll_error_report_at < LOG_POLL_ERROR_REPORT_INTERVAL_SECONDS:
            return
        self._last_poll_error_report_at = now
        sys.stderr.write(f"Log broadcaster database polling failed: {exc}\n")
        traceback.print_exception(type(exc), exc, exc.__traceback__, file=sys.stderr)

    async def _send_messages(self, websocket: WebSocket, queue: asyncio.Queue[tuple[str, int | None]]) -> None:
        try:
            while True:
                message, log_id = await queue.get()
                await websocket.send_text(message)
                if log_id is not None:
                    async with self.lock:
                        connection = self.active_connections.get(websocket)
                        if connection is not None:
                            connection.after_id = max(connection.after_id, log_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            await self._remove_connection(websocket)

    async def _remove_connection(self, websocket: WebSocket) -> LogConnection | None:
        async with self.lock:
            return self.active_connections.pop(websocket, None)


# 全局单例
log_broadcaster = LogBroadcaster()
