"""
RemoteExecutor for Phase 12.6.

Manages WebSocket connections to remote runners and pushes jobs to them.
Unlike LocalExecutor which spawns containers directly, RemoteExecutor
coordinates with runner agents via WebSocket for distributed execution.
"""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from fastapi import WebSocket
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.runner import Runner, RunnerStatus
from app.services.execution.runner_state import (
    RunnerState,
    RunnerStateMachine,
)
from app.services.execution.runner_protocol import (
    ACK_TIMEOUT,
    DEATH_TIMEOUT,
)

logger = logging.getLogger(__name__)


class NoRunnerAvailableError(Exception):
    """Raised when no runner is available for the requested requirements."""
    pass


class RunnerNotConnectedError(Exception):
    """Raised when trying to push to a disconnected runner."""
    pass


class AckTimeoutError(Exception):
    """Raised when runner doesn't ACK job assignment in time."""
    pass


# Singleton instance
_remote_executor: Optional["RemoteExecutor"] = None


def get_remote_executor() -> "RemoteExecutor":
    """Get the RemoteExecutor singleton instance."""
    global _remote_executor
    if _remote_executor is None:
        _remote_executor = RemoteExecutor()
    return _remote_executor


class RemoteExecutor:
    """
    Manages WebSocket connections to remote runners and pushes jobs.

    Unlike polling-based runner pool, RemoteExecutor pushes jobs immediately
    when runners are idle, achieving millisecond latency.
    """

    def __init__(self):
        """Initialize RemoteExecutor."""
        # WebSocket connections: runner_id -> WebSocket
        self._connections: dict[str, WebSocket] = {}

        # State machines: runner_id -> RunnerStateMachine
        self._runner_states: dict[str, RunnerStateMachine] = {}

        # Pending ACKs: step_id -> Future
        self._pending_acks: dict[str, asyncio.Future] = {}

        # Background task for timeout monitoring
        self._monitor_task: Optional[asyncio.Task] = None

    # ========================================================================
    # Connection Management
    # ========================================================================

    def get_connection(self, runner_id: str) -> Optional[WebSocket]:
        """Get WebSocket connection for a runner."""
        return self._connections.get(runner_id)

    def get_connected_runner_ids(self) -> list[str]:
        """Get list of connected runner IDs."""
        return list(self._connections.keys())

    def is_runner_connected(self, runner_id: str) -> bool:
        """Check if a runner is connected."""
        return runner_id in self._connections

    # ========================================================================
    # Runner Registration
    # ========================================================================

    async def register_runner(
        self,
        db: AsyncSession,
        websocket: WebSocket,
        runner_id: str,
        name: str,
        runner_type: str,
        labels: dict,
    ) -> Runner:
        """
        Register a runner and store its WebSocket connection.

        Args:
            db: Database session
            websocket: WebSocket connection
            runner_id: Runner ID (client-provided or generated)
            name: Human-readable runner name
            runner_type: Runner type (claude-code, gemini, etc.)
            labels: Runner labels for requirement matching

        Returns:
            Runner model instance
        """
        # Check if runner already exists
        result = await db.execute(
            select(Runner).where(Runner.id == runner_id)
        )
        runner = result.scalar_one_or_none()

        websocket_id = str(uuid4())

        if runner:
            # Update existing runner
            runner.name = name
            runner.runner_type = runner_type
            runner.set_labels(labels)
            runner.status = RunnerStatus.IDLE.value
            runner.websocket_id = websocket_id
            runner.connected_at = datetime.utcnow()
            runner.last_heartbeat = datetime.utcnow()
        else:
            # Create new runner
            runner = Runner(
                id=runner_id,
                name=name,
                runner_type=runner_type,
                status=RunnerStatus.IDLE.value,
                websocket_id=websocket_id,
                connected_at=datetime.utcnow(),
                last_heartbeat=datetime.utcnow(),
            )
            runner.set_labels(labels)
            db.add(runner)

        await db.commit()
        await db.refresh(runner)

        # Store connection and create state machine
        self._connections[runner_id] = websocket
        self._runner_states[runner_id] = RunnerStateMachine(
            runner_id=runner_id,
            initial_state=RunnerState.IDLE,
        )

        logger.info(f"Runner registered: {runner_id} ({name})")
        return runner

    # ========================================================================
    # Job Push
    # ========================================================================

    async def push_step(
        self,
        runner: Runner,
        step_execution: Any,
        config: dict,
    ) -> bool:
        """
        Push a step to a runner via WebSocket.

        Args:
            runner: Runner model
            step_execution: StepExecution model
            config: Execution configuration

        Returns:
            True if step was ACKed, False otherwise

        Raises:
            RunnerNotConnectedError: If runner is not connected
            AckTimeoutError: If runner doesn't ACK in time
        """
        runner_id = runner.id
        step_id = step_execution.id
        execution_key = getattr(step_execution, 'execution_key', f"exec-{step_id}")

        # Check connection
        websocket = self._connections.get(runner_id)
        if not websocket:
            raise RunnerNotConnectedError(f"Runner {runner_id} is not connected")

        # Update state machine
        state = self._runner_states.get(runner_id)
        if state:
            state.assign_step(step_id)

        # Create ACK future
        ack_future = asyncio.get_event_loop().create_future()
        self._pending_acks[step_id] = ack_future

        # Send execute_step message
        await websocket.send_json({
            "type": "execute_step",
            "step_id": step_id,
            "execution_key": execution_key,
            "config": config,
        })

        logger.info(f"Pushed step {step_id} to runner {runner_id}")

        # Wait for ACK with timeout
        try:
            await asyncio.wait_for(
                self._wait_for_ack(step_id),
                timeout=ACK_TIMEOUT,
            )
            return True
        except asyncio.TimeoutError:
            # Clean up pending ACK
            self._pending_acks.pop(step_id, None)

            # Transition to dead
            if state:
                state.transition(RunnerState.DEAD, reason="ACK timeout")

            raise AckTimeoutError(f"Runner {runner_id} didn't ACK step {step_id}")

    async def _wait_for_ack(self, step_id: str) -> bool:
        """Wait for ACK for a specific step."""
        future = self._pending_acks.get(step_id)
        if future:
            return await future
        return False

    # ========================================================================
    # Message Handlers
    # ========================================================================

    async def handle_ack(self, runner_id: str, step_id: str) -> None:
        """
        Handle ACK message from runner.

        Args:
            runner_id: Runner ID
            step_id: Step ID being ACKed
        """
        # Resolve pending ACK future
        future = self._pending_acks.pop(step_id, None)
        if future and not future.done():
            future.set_result(True)

        # Transition state machine to BUSY
        state = self._runner_states.get(runner_id)
        if state and state.state == RunnerState.ASSIGNED:
            state.transition(RunnerState.BUSY)

        logger.debug(f"ACK received from {runner_id} for step {step_id}")

    async def handle_heartbeat(self, db: AsyncSession, runner_id: str) -> None:
        """
        Handle heartbeat message from runner.

        Args:
            db: Database session
            runner_id: Runner ID
        """
        state = self._runner_states.get(runner_id)
        if state:
            state.update_heartbeat()

        # Update database
        result = await db.execute(
            select(Runner).where(Runner.id == runner_id)
        )
        runner = result.scalar_one_or_none()
        if runner:
            runner.last_heartbeat = datetime.utcnow()
            await db.commit()

    async def handle_step_complete(
        self,
        db: AsyncSession,
        runner_id: str,
        step_id: str,
        exit_code: int,
        error: Optional[str],
    ) -> None:
        """
        Handle step completion from runner.

        Args:
            db: Database session
            runner_id: Runner ID
            step_id: Completed step ID
            exit_code: Exit code (0 = success)
            error: Error message if failed
        """
        state = self._runner_states.get(runner_id)
        if state:
            state.complete_step()

        # Update runner in database
        result = await db.execute(
            select(Runner).where(Runner.id == runner_id)
        )
        runner = result.scalar_one_or_none()
        if runner:
            runner.status = RunnerStatus.IDLE.value
            runner.current_step_execution_id = None
            await db.commit()

        logger.info(
            f"Step {step_id} completed on runner {runner_id}: "
            f"exit_code={exit_code}, error={error}"
        )

    async def handle_disconnect(self, db: AsyncSession, runner_id: str) -> None:
        """
        Handle runner disconnection.

        Args:
            db: Database session
            runner_id: Disconnected runner ID
        """
        # Remove connection
        self._connections.pop(runner_id, None)

        # Get state and check for in-progress work
        state = self._runner_states.get(runner_id)
        step_id = None
        if state:
            step_id = state.current_step_id
            state.transition(RunnerState.DISCONNECTED)

        # Update database
        result = await db.execute(
            select(Runner).where(Runner.id == runner_id)
        )
        runner = result.scalar_one_or_none()
        if runner:
            runner.status = RunnerStatus.DISCONNECTED.value
            runner.websocket_id = None
            await db.commit()

        # Requeue step if runner was busy
        if step_id:
            await self._requeue_step(db, step_id)

        logger.info(f"Runner {runner_id} disconnected")

    async def _requeue_step(self, db: AsyncSession, step_id: str) -> None:
        """Requeue a step after runner failure."""
        from app.models.step_execution import StepExecution, ExecutionStatus

        result = await db.execute(
            select(StepExecution).where(StepExecution.id == step_id)
        )
        step = result.scalar_one_or_none()
        if step and step.status in (
            ExecutionStatus.PREPARING.value,
            ExecutionStatus.RUNNING.value,
        ):
            step.status = ExecutionStatus.PENDING.value
            step.runner_id = None
            await db.commit()
            logger.info(f"Requeued step {step_id} after runner disconnect")

    # ========================================================================
    # Find Idle Runner
    # ========================================================================

    async def find_idle_runner(
        self,
        db: AsyncSession,
        runner_type: str = "any",
        requirements: Optional[dict] = None,
    ) -> Optional[Runner]:
        """
        Find an idle runner matching requirements.

        Args:
            db: Database session
            runner_type: Required runner type (or "any")
            requirements: Label requirements dict

        Returns:
            Matching Runner or None
        """
        requirements = requirements or {}

        # Query idle runners
        runners = await self._query_idle_runners(db)

        for runner in runners:
            # Check if connected
            if not self.is_runner_connected(runner.id):
                continue

            # Check runner type
            if runner_type != "any" and runner.runner_type != runner_type:
                continue

            # Check label requirements
            if not runner.matches_requirements(requirements):
                continue

            return runner

        return None

    async def _query_idle_runners(self, db: AsyncSession) -> list[Runner]:
        """Query idle runners from database."""
        result = await db.execute(
            select(Runner).where(Runner.status == RunnerStatus.IDLE.value)
        )
        return list(result.scalars().all())

    # ========================================================================
    # Timeout Monitoring
    # ========================================================================

    async def start_monitor(self) -> None:
        """Start the background timeout monitor."""
        if self._monitor_task is None:
            self._monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("RemoteExecutor timeout monitor started")

    async def stop_monitor(self) -> None:
        """Stop the background timeout monitor."""
        if self._monitor_task:
            self._monitor_task.cancel()
            try:
                await self._monitor_task
            except asyncio.CancelledError:
                pass
            self._monitor_task = None
            logger.info("RemoteExecutor timeout monitor stopped")

    async def _monitor_loop(self) -> None:
        """Background loop for timeout monitoring."""
        while True:
            try:
                await asyncio.sleep(5)  # Check every 5 seconds
                await self._check_timeouts()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in timeout monitor: {e}")

    async def _check_timeouts(self) -> None:
        """Check for ACK and heartbeat timeouts."""
        for runner_id, state in list(self._runner_states.items()):
            # Check for dead runners (heartbeat timeout)
            if state.state in {RunnerState.ASSIGNED, RunnerState.BUSY}:
                if not state.is_alive(timeout_seconds=DEATH_TIMEOUT):
                    await self._handle_death(runner_id)
            # Check ACK timeout handled in push_step via asyncio.wait_for

    async def _handle_ack_timeout(self, runner_id: str) -> None:
        """Handle ACK timeout for a runner."""
        state = self._runner_states.get(runner_id)
        if state and state.state == RunnerState.ASSIGNED:
            step_id = state.current_step_id
            state.transition(RunnerState.DEAD, reason="ACK timeout")
            logger.warning(f"Runner {runner_id} ACK timeout for step {step_id}")

    async def _handle_death(self, runner_id: str) -> None:
        """Handle runner death (heartbeat timeout)."""
        state = self._runner_states.get(runner_id)
        if state:
            step_id = state.current_step_id
            if state.state != RunnerState.DEAD:
                state.transition(RunnerState.DEAD, reason="Heartbeat timeout")
            logger.warning(f"Runner {runner_id} marked dead, step={step_id}")

            # Close WebSocket if still open
            ws = self._connections.pop(runner_id, None)
            if ws:
                try:
                    await ws.close()
                except Exception:
                    pass
