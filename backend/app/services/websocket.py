from fastapi import WebSocket, WebSocketDisconnect
from typing import Any
import json


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast(self, message_type: str, payload: Any):
        message = json.dumps({"type": message_type, "payload": payload})
        disconnected = []
        for connection in self.active_connections:
            try:
                await connection.send_text(message)
            except Exception:
                disconnected.append(connection)

        for conn in disconnected:
            self.disconnect(conn)

    async def send_card_updated(self, card_data: dict):
        await self.broadcast("card_updated", card_data)

    async def send_job_status(self, job_data: dict):
        await self.broadcast("job_status", job_data)

    async def send_runner_status(self, runner_data: dict):
        await self.broadcast("runner_status", runner_data)


manager = ConnectionManager()
