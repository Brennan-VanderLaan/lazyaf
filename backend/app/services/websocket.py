from fastapi import WebSocket, WebSocketDisconnect
from typing import Any
import asyncio
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
        """Fan a message out to every connection concurrently.

        asyncio.gather(return_exceptions=True) so one slow/broken socket
        neither serializes nor aborts delivery to the rest (12.2-INT fix:
        log fan-out must not become O(connections) latency per frame).
        Connections that error are dropped.
        """
        message = json.dumps({"type": message_type, "payload": payload})
        connections = list(self.active_connections)
        if not connections:
            return
        results = await asyncio.gather(
            *(connection.send_text(message) for connection in connections),
            return_exceptions=True,
        )
        for connection, result in zip(connections, results):
            if isinstance(result, Exception):
                self.disconnect(connection)

    async def send_card_updated(self, card_data: dict):
        await self.broadcast("card_updated", card_data)

    async def send_card_deleted(self, card_id: str):
        await self.broadcast("card_deleted", {"id": card_id})

    async def send_job_status(self, job_data: dict):
        await self.broadcast("job_status", job_data)

    async def send_runner_status(self, runner_data: dict):
        await self.broadcast("runner_status", runner_data)

    # Pipeline-related broadcasts (Phase 9)
    async def send_pipeline_updated(self, pipeline_data: dict):
        await self.broadcast("pipeline_updated", pipeline_data)

    async def send_pipeline_deleted(self, pipeline_id: str):
        await self.broadcast("pipeline_deleted", {"id": pipeline_id})

    async def send_pipeline_run_status(self, run_data: dict):
        await self.broadcast("pipeline_run_status", run_data)

    async def send_step_run_status(self, step_data: dict):
        await self.broadcast("step_run_status", step_data)

    # Typed publish API (Phase 12.2-INT). The local execution path calls these
    # instead of hand-rolling payload dicts: the explicit signatures make an
    # arity/field mistake a loud call-site error instead of a silently
    # misshapen broadcast (failure_01 landmine 7). Legacy send_* methods above
    # stay untouched for existing callers.
    async def publish_step_update(self, run_id: str, step_index: int, status: str) -> None:
        """Broadcast a step status transition for a pipeline run."""
        await self.broadcast(
            "step_update",
            {"pipeline_run_id": run_id, "step_index": step_index, "status": status},
        )

    async def publish_step_log(self, run_id: str, step_index: int, line: str) -> None:
        """Broadcast a single log line from a running step."""
        await self.broadcast(
            "step_log",
            {"pipeline_run_id": run_id, "step_index": step_index, "line": line},
        )

    async def publish_step_logs(
        self, run_id: str, step_index: int, lines: list[str]
    ) -> None:
        """Broadcast a batch of log lines flushed together (12.2-INT fix 7).

        The batching lives in the flush cadence (the executor buffers ~200
        lines / 500ms before calling this); each line still goes out as its
        own step_log frame so the wire contract with existing UI consumers
        is unchanged.
        """
        for line in lines:
            await self.publish_step_log(run_id, step_index, line)

    async def reset(self):
        """Close and forget all connections. Test-mode reset hook; clients
        (the e2e frontend) are expected to reconnect."""
        connections = self.active_connections
        self.active_connections = []
        for connection in connections:
            try:
                await connection.close()
            except Exception:
                pass

    # Repo-related broadcasts
    async def send_repo_created(self, repo_data: dict):
        await self.broadcast("repo_created", repo_data)

    async def send_repo_updated(self, repo_data: dict):
        await self.broadcast("repo_updated", repo_data)

    async def send_repo_deleted(self, repo_id: str):
        await self.broadcast("repo_deleted", {"id": repo_id})


manager = ConnectionManager()
