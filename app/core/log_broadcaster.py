import asyncio
import json

from fastapi import WebSocket


class LogBroadcaster:
    """
    日志广播管理器，负责管理 WebSocket 连接并将日志实时推送给订阅者。
    """

    def __init__(self):
        self.active_connections: set[WebSocket] = set()
        self.lock = asyncio.Lock()

    async def connect(self, websocket: WebSocket):
        """
        注册新连接
        """
        await websocket.accept()
        async with self.lock:
            self.active_connections.add(websocket)

    async def disconnect(self, websocket: WebSocket):
        """
        移除连接
        """
        async with self.lock:
            if websocket in self.active_connections:
                self.active_connections.remove(websocket)

    async def broadcast(self, log_entry: dict):
        """
        异步推送日志给所有订阅者
        """
        if not self.active_connections:
            return

        message = json.dumps(log_entry, ensure_ascii=False)

        async with self.lock:
            # 复制一份连接列表以避免在遍历时修改集合（虽然使用了锁，但为了健壮性）
            connections = list(self.active_connections)

        # 批量并行发送
        tasks = []
        for connection in connections:
            tasks.append(self._send_to_one(connection, message))

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    async def _send_to_one(self, websocket: WebSocket, message: str):
        try:
            await websocket.send_text(message)
        except Exception:
            # 发送失败则认为连接已失效，交给路由层的异常捕获来清理
            pass


# 全局单例
log_broadcaster = LogBroadcaster()
