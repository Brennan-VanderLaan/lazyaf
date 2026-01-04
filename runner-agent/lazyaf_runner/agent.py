"""
Runner Agent - connects to LazyAF backend and executes steps.

Usage:
    agent = RunnerAgent(RunnerConfig(
        backend_url="http://localhost:8000",
        runner_name="My Runner",
        labels={"arch": "arm64", "has": ["gpio"]},
    ))
    await agent.run()  # Blocks, reconnects on disconnect
"""

import asyncio
import json
import logging
from typing import Optional
from uuid import uuid4

import websockets
from websockets.client import WebSocketClientProtocol

from .config import RunnerConfig
from .docker_orch import DockerOrchestrator

logger = logging.getLogger(__name__)


class RunnerAgent:
    """
    Runner agent that connects to LazyAF backend and executes steps.

    Handles:
    - WebSocket connection with automatic reconnection
    - Registration with backend
    - Heartbeat sending
    - Step execution via orchestrator
    - Log streaming
    """

    def __init__(
        self,
        config: Optional[RunnerConfig] = None,
        orchestrator: Optional[DockerOrchestrator] = None,
    ):
        """
        Initialize runner agent.

        Args:
            config: Runner configuration
            orchestrator: Orchestrator for executing steps
        """
        self.config = config or RunnerConfig()
        self.orchestrator = orchestrator or DockerOrchestrator()

        # State
        self._runner_id: Optional[str] = self.config.runner_id or str(uuid4())
        self._websocket: Optional[WebSocketClientProtocol] = None
        self._running = False
        self._current_step_id: Optional[str] = None

    @property
    def runner_id(self) -> str:
        """Get runner ID."""
        return self._runner_id

    async def run(self) -> None:
        """
        Run the agent forever.

        Connects to backend and automatically reconnects on disconnect.
        """
        self._running = True
        logger.info(f"Starting runner agent: {self._runner_id}")

        while self._running:
            try:
                await self._connect_and_serve()
            except asyncio.CancelledError:
                logger.info("Runner agent cancelled")
                break
            except Exception as e:
                logger.error(f"Connection error: {e}")
                if self._running:
                    logger.info(
                        f"Reconnecting in {self.config.reconnect_delay} seconds..."
                    )
                    await asyncio.sleep(self.config.reconnect_delay)

        logger.info("Runner agent stopped")

    async def stop(self) -> None:
        """Stop the agent."""
        self._running = False
        if self._websocket:
            await self._websocket.close()

    async def _connect_and_serve(self) -> None:
        """Connect to backend and handle messages."""
        logger.info(f"Connecting to {self.config.websocket_url}")

        async with websockets.connect(self.config.websocket_url) as ws:
            self._websocket = ws

            # Register
            await self._send_register()

            # Wait for registered response
            response = await ws.recv()
            data = json.loads(response)

            if data.get("type") == "error":
                raise Exception(f"Registration failed: {data.get('message')}")

            if data.get("type") != "registered":
                raise Exception(f"Expected 'registered', got: {data.get('type')}")

            # Update runner ID from server
            self._runner_id = data.get("runner_id", self._runner_id)
            logger.info(f"Registered as {self._runner_id}")

            # Start heartbeat task
            heartbeat_task = asyncio.create_task(self._heartbeat_loop())

            try:
                # Main receive loop
                async for message in ws:
                    await self._handle_message(json.loads(message))
            finally:
                heartbeat_task.cancel()
                try:
                    await heartbeat_task
                except asyncio.CancelledError:
                    pass

    async def _send_register(self) -> None:
        """Send registration message."""
        await self._websocket.send(json.dumps({
            "type": "register",
            "runner_id": self._runner_id,
            "name": self.config.runner_name,
            "runner_type": self.config.runner_type,
            "labels": self.config.labels,
        }))

    async def _heartbeat_loop(self) -> None:
        """Send periodic heartbeats."""
        while True:
            try:
                await asyncio.sleep(self.config.heartbeat_interval)
                await self._websocket.send(json.dumps({"type": "heartbeat"}))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning(f"Heartbeat error: {e}")
                break

    async def _handle_message(self, data: dict) -> None:
        """Handle a message from the backend."""
        msg_type = data.get("type")

        if msg_type == "execute_step":
            await self._execute_step(data)
        elif msg_type == "ping":
            await self._websocket.send(json.dumps({"type": "heartbeat"}))
        elif msg_type == "pong":
            pass  # Heartbeat response, ignore
        elif msg_type == "error":
            logger.error(f"Server error: {data.get('message')}")
        else:
            logger.warning(f"Unknown message type: {msg_type}")

    async def _execute_step(self, data: dict) -> None:
        """Execute a step from the backend."""
        step_id = data.get("step_id")
        execution_key = data.get("execution_key")
        config = data.get("config", {})

        logger.info(f"Executing step {step_id}")
        self._current_step_id = step_id

        try:
            # Send ACK
            await self._websocket.send(json.dumps({
                "type": "ack",
                "step_id": step_id,
            }))

            # Execute step via orchestrator
            exit_code = 0
            error = None

            try:
                async for log_lines in self.orchestrator.execute(config):
                    # Stream logs
                    await self._websocket.send(json.dumps({
                        "type": "log",
                        "step_id": step_id,
                        "lines": log_lines,
                    }))

                exit_code = self.orchestrator.last_exit_code

            except Exception as e:
                logger.error(f"Step execution failed: {e}")
                exit_code = 1
                error = str(e)

            # Send completion
            await self._websocket.send(json.dumps({
                "type": "step_complete",
                "step_id": step_id,
                "exit_code": exit_code,
                "error": error,
            }))

            logger.info(f"Step {step_id} completed: exit_code={exit_code}")

        except Exception as e:
            logger.error(f"Error executing step {step_id}: {e}")
            # Try to send failure
            try:
                await self._websocket.send(json.dumps({
                    "type": "step_complete",
                    "step_id": step_id,
                    "exit_code": 1,
                    "error": str(e),
                }))
            except Exception:
                pass

        finally:
            self._current_step_id = None
