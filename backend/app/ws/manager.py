import asyncio
import json
from collections import defaultdict

from fastapi import WebSocket


class LogConnectionManager:
    """
    管理 WebSocket 連線，以 task_id 分組廣播。
    Celery Worker 透過 Redis pub/sub 發布訊息，
    WebSocket endpoint 訂閱後轉發給前端終端機。
    """

    def __init__(self) -> None:
        self._connections: dict[str, list[WebSocket]] = defaultdict(list)

    async def connect(self, task_id: str, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections[task_id].append(websocket)

    def disconnect(self, task_id: str, websocket: WebSocket) -> None:
        conns = self._connections.get(task_id, [])
        if websocket in conns:
            conns.remove(websocket)
        if not conns:
            self._connections.pop(task_id, None)

    async def broadcast(self, task_id: str, payload: dict) -> None:
        dead: list[WebSocket] = []
        for ws in list(self._connections.get(task_id, [])):
            try:
                await ws.send_json(payload)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(task_id, ws)


# 全域單例（單程序內共用）
manager = LogConnectionManager()
