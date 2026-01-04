"""
Tests for RemoteExecutor Contract (Phase 12.6).

These tests DEFINE the interface and behavior of the RemoteExecutor service.
Write tests first, then implement to make them pass.

RemoteExecutor is the backend service that:
- Manages WebSocket connections to remote runners
- Pushes jobs to idle runners immediately
- Handles ACK timeouts and reassignment
- Monitors heartbeats and detects dead runners
- Coordinates job recovery when runners disconnect

Key Differences from LocalExecutor:
- LocalExecutor spawns containers directly (instant)
- RemoteExecutor pushes to connected runners (milliseconds, WebSocket)
"""

from datetime import datetime, timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.remote_executor import (
        RemoteExecutor,
        get_remote_executor,
        NoRunnerAvailableError,
        RunnerNotConnectedError,
        AckTimeoutError,
    )
    from app.services.execution.runner_state import RunnerState
    from app.models.runner import Runner
    REMOTE_EXECUTOR_MODULE_AVAILABLE = True
except ImportError:
    REMOTE_EXECUTOR_MODULE_AVAILABLE = False
    # Define placeholders
    RemoteExecutor = None
    get_remote_executor = None
    NoRunnerAvailableError = Exception
    RunnerNotConnectedError = Exception
    AckTimeoutError = Exception
    RunnerState = None
    Runner = None


pytestmark = pytest.mark.skipif(
    not REMOTE_EXECUTOR_MODULE_AVAILABLE,
    reason="remote_executor module not yet implemented"
)


class TestRemoteExecutorSingleton:
    """Tests for RemoteExecutor singleton pattern."""

    def test_get_remote_executor_returns_instance(self):
        """get_remote_executor() returns a RemoteExecutor instance."""
        executor = get_remote_executor()
        assert isinstance(executor, RemoteExecutor)

    def test_get_remote_executor_returns_same_instance(self):
        """get_remote_executor() returns the same instance (singleton)."""
        executor1 = get_remote_executor()
        executor2 = get_remote_executor()
        assert executor1 is executor2


class TestRunnerRegistration:
    """Tests for runner registration."""

    @pytest.fixture
    def executor(self):
        """Create a fresh RemoteExecutor for testing."""
        return RemoteExecutor()

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket connection."""
        ws = AsyncMock()
        ws.send_json = AsyncMock()
        ws.receive_json = AsyncMock()
        ws.close = AsyncMock()
        return ws

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session that returns no existing runner."""
        db = AsyncMock()
        db.add = MagicMock()
        db.commit = AsyncMock()
        db.refresh = AsyncMock()
        # Mock execute to return empty result (no existing runner)
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = None
        db.execute = AsyncMock(return_value=mock_result)
        return db

    @pytest.mark.asyncio
    async def test_register_runner_creates_db_record(self, executor, mock_db, mock_websocket):
        """register_runner() creates a Runner record in the database."""
        runner = await executor.register_runner(
            db=mock_db,
            websocket=mock_websocket,
            runner_id="runner-1",
            name="Test Runner",
            runner_type="claude-code",
            labels={"arch": "amd64"}
        )

        # Should have called db.add with a Runner
        mock_db.add.assert_called_once()
        mock_db.commit.assert_called()

    @pytest.mark.asyncio
    async def test_register_runner_stores_websocket(self, executor, mock_db, mock_websocket):
        """register_runner() stores the WebSocket connection."""
        await executor.register_runner(
            db=mock_db,
            websocket=mock_websocket,
            runner_id="runner-1",
            name="Test Runner",
            runner_type="claude-code",
            labels={}
        )

        # Should be able to get the connection
        assert executor.get_connection("runner-1") is mock_websocket

    @pytest.mark.asyncio
    async def test_register_runner_returns_runner(self, executor, mock_db, mock_websocket):
        """register_runner() returns the Runner model."""
        runner = await executor.register_runner(
            db=mock_db,
            websocket=mock_websocket,
            runner_id="runner-1",
            name="Test Runner",
            runner_type="claude-code",
            labels={}
        )

        assert runner is not None
        assert runner.id == "runner-1"

    @pytest.mark.asyncio
    async def test_register_runner_sets_idle_status(self, executor, mock_db, mock_websocket):
        """register_runner() sets runner status to IDLE."""
        runner = await executor.register_runner(
            db=mock_db,
            websocket=mock_websocket,
            runner_id="runner-1",
            name="Test Runner",
            runner_type="claude-code",
            labels={}
        )

        assert runner.status == "idle"

    @pytest.mark.asyncio
    async def test_register_runner_stores_labels(self, executor, mock_db, mock_websocket):
        """register_runner() stores runner labels."""
        labels = {"arch": "arm64", "has": ["gpio", "camera"]}

        runner = await executor.register_runner(
            db=mock_db,
            websocket=mock_websocket,
            runner_id="runner-1",
            name="Pi Runner",
            runner_type="claude-code",
            labels=labels
        )

        # Labels should be stored (as JSON string or dict depending on impl)
        assert runner.labels is not None


class TestPushStep:
    """Tests for pushing steps to runners."""

    @pytest.fixture
    def executor(self):
        """Create a RemoteExecutor with a registered runner."""
        executor = RemoteExecutor()
        return executor

    @pytest.fixture
    def mock_websocket(self):
        """Create a mock WebSocket that ACKs immediately."""
        ws = AsyncMock()
        ws.send_json = AsyncMock()
        return ws

    @pytest.fixture
    def mock_runner(self):
        """Create a mock Runner model."""
        runner = MagicMock()
        runner.id = "runner-1"
        runner.status = "idle"
        runner.runner_type = "claude-code"
        return runner

    @pytest.fixture
    def mock_step_execution(self):
        """Create a mock StepExecution."""
        step = MagicMock()
        step.id = "step-123"
        step.execution_key = "run-1:0:1"
        return step

    @pytest.fixture
    def mock_config(self):
        """Create a mock ExecutionConfig."""
        return {
            "command": "pytest -v",
            "image": "python:3.12",
            "timeout": 300
        }

    @pytest.mark.asyncio
    async def test_push_step_sends_execute_message(
        self, executor, mock_websocket, mock_runner, mock_step_execution, mock_config
    ):
        """push_step() sends execute_step message via WebSocket."""
        # Register the runner first
        executor._connections["runner-1"] = mock_websocket
        executor._runner_states["runner-1"] = MagicMock()

        # Mock _wait_for_ack to return immediately
        async def instant_ack(step_id):
            return True

        with patch.object(executor, '_wait_for_ack', side_effect=instant_ack):
            await executor.push_step(mock_runner, mock_step_execution, mock_config)

        # Should have sent the execute message
        mock_websocket.send_json.assert_called()
        call_args = mock_websocket.send_json.call_args[0][0]
        assert call_args["type"] == "execute_step"
        assert call_args["step_id"] == "step-123"

    @pytest.mark.asyncio
    async def test_push_step_waits_for_ack(
        self, executor, mock_websocket, mock_runner, mock_step_execution, mock_config
    ):
        """push_step() waits for ACK from runner."""
        executor._connections["runner-1"] = mock_websocket
        executor._runner_states["runner-1"] = MagicMock()

        # Create a future that will be resolved
        async def delayed_ack(step_id):
            await asyncio.sleep(0.01)
            return True

        with patch.object(executor, '_wait_for_ack', side_effect=delayed_ack):
            result = await executor.push_step(mock_runner, mock_step_execution, mock_config)

        assert result is True

    @pytest.mark.asyncio
    async def test_push_step_raises_on_ack_timeout(
        self, executor, mock_websocket, mock_runner, mock_step_execution, mock_config
    ):
        """push_step() raises AckTimeoutError if ACK not received."""
        executor._connections["runner-1"] = mock_websocket
        executor._runner_states["runner-1"] = MagicMock()

        # Simulate very long wait that will timeout
        async def slow_ack(step_id):
            await asyncio.sleep(100)  # Will be cancelled by timeout
            return True

        with patch.object(executor, '_wait_for_ack', side_effect=slow_ack):
            with patch('app.services.execution.remote_executor.ACK_TIMEOUT', 0.01):
                with pytest.raises(AckTimeoutError):
                    await executor.push_step(mock_runner, mock_step_execution, mock_config)

    @pytest.mark.asyncio
    async def test_push_step_raises_if_runner_not_connected(
        self, executor, mock_runner, mock_step_execution, mock_config
    ):
        """push_step() raises if runner is not connected."""
        # Don't register the runner

        with pytest.raises(RunnerNotConnectedError):
            await executor.push_step(mock_runner, mock_step_execution, mock_config)


class TestFindIdleRunner:
    """Tests for finding idle runners."""

    @pytest.fixture
    def executor(self):
        """Create a RemoteExecutor."""
        return RemoteExecutor()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        return db

    @pytest.mark.asyncio
    async def test_find_idle_runner_returns_matching_runner(self, executor, mock_db):
        """find_idle_runner() returns a runner matching requirements."""
        # Set up mock runner
        mock_runner = MagicMock()
        mock_runner.id = "runner-1"
        mock_runner.status = "idle"
        mock_runner.runner_type = "claude-code"
        mock_runner.matches_requirements = MagicMock(return_value=True)

        # Register connection so is_runner_connected returns True
        executor._connections["runner-1"] = AsyncMock()

        async def mock_query(db):
            return [mock_runner]

        with patch.object(executor, '_query_idle_runners', side_effect=mock_query):
            runner = await executor.find_idle_runner(
                db=mock_db,
                runner_type="claude-code",
                requirements={}
            )

        assert runner is not None
        assert runner.id == "runner-1"

    @pytest.mark.asyncio
    async def test_find_idle_runner_returns_none_if_no_match(self, executor, mock_db):
        """find_idle_runner() returns None if no runner matches."""
        async def mock_query(db):
            return []

        with patch.object(executor, '_query_idle_runners', side_effect=mock_query):
            runner = await executor.find_idle_runner(
                db=mock_db,
                runner_type="claude-code",
                requirements={"arch": "arm64"}
            )

        assert runner is None

    @pytest.mark.asyncio
    async def test_find_idle_runner_filters_by_runner_type(self, executor, mock_db):
        """find_idle_runner() filters by runner_type."""
        mock_runner = MagicMock()
        mock_runner.id = "runner-1"
        mock_runner.runner_type = "gemini"
        mock_runner.matches_requirements = MagicMock(return_value=True)

        executor._connections["runner-1"] = AsyncMock()

        async def mock_query(db):
            return [mock_runner]

        with patch.object(executor, '_query_idle_runners', side_effect=mock_query):
            # Looking for claude-code, but only gemini available
            runner = await executor.find_idle_runner(
                db=mock_db,
                runner_type="claude-code",
                requirements={}
            )

        assert runner is None

    @pytest.mark.asyncio
    async def test_find_idle_runner_filters_by_labels(self, executor, mock_db):
        """find_idle_runner() filters by label requirements."""
        mock_runner = MagicMock()
        mock_runner.id = "runner-1"
        mock_runner.runner_type = "claude-code"
        mock_runner.labels = '{"arch": "amd64"}'
        mock_runner.matches_requirements = MagicMock(return_value=False)  # Doesn't match arm64

        executor._connections["runner-1"] = AsyncMock()

        async def mock_query(db):
            return [mock_runner]

        with patch.object(executor, '_query_idle_runners', side_effect=mock_query):
            # Looking for arm64, but only amd64 available
            runner = await executor.find_idle_runner(
                db=mock_db,
                runner_type="claude-code",
                requirements={"arch": "arm64"}
            )

        assert runner is None

    @pytest.mark.asyncio
    async def test_find_idle_runner_matches_has_labels(self, executor, mock_db):
        """find_idle_runner() matches 'has' label requirements."""
        mock_runner = MagicMock()
        mock_runner.id = "runner-1"
        mock_runner.runner_type = "claude-code"
        mock_runner.labels = '{"has": ["gpio", "camera"]}'
        mock_runner.matches_requirements = MagicMock(return_value=True)

        executor._connections["runner-1"] = AsyncMock()

        async def mock_query(db):
            return [mock_runner]

        with patch.object(executor, '_query_idle_runners', side_effect=mock_query):
            runner = await executor.find_idle_runner(
                db=mock_db,
                runner_type="claude-code",
                requirements={"has": ["gpio"]}
            )

        assert runner is not None


class TestHandleAck:
    """Tests for handling ACK messages from runners."""

    @pytest.fixture
    def executor(self):
        """Create a RemoteExecutor."""
        return RemoteExecutor()

    @pytest.mark.asyncio
    async def test_handle_ack_resolves_pending_future(self, executor):
        """handle_ack() resolves the pending ACK future."""
        # Create a pending ACK future
        future = asyncio.get_event_loop().create_future()
        executor._pending_acks["step-123"] = future

        await executor.handle_ack("runner-1", "step-123")

        assert future.done()
        assert future.result() is True

    @pytest.mark.asyncio
    async def test_handle_ack_ignores_unknown_step(self, executor):
        """handle_ack() ignores ACK for unknown step (no error)."""
        # No pending ACK for this step
        await executor.handle_ack("runner-1", "unknown-step")
        # Should not raise


class TestHandleHeartbeat:
    """Tests for handling heartbeat messages."""

    @pytest.fixture
    def executor(self):
        """Create a RemoteExecutor."""
        return RemoteExecutor()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        # Mock execute to return a runner
        mock_runner = MagicMock()
        mock_runner.id = "runner-1"
        mock_runner.last_heartbeat = datetime.utcnow()
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_runner
        db.execute = AsyncMock(return_value=mock_result)
        return db

    @pytest.mark.asyncio
    async def test_handle_heartbeat_updates_timestamp(self, executor, mock_db):
        """handle_heartbeat() updates runner's last_heartbeat."""
        executor._runner_states["runner-1"] = MagicMock()

        before = datetime.utcnow()
        await executor.handle_heartbeat(mock_db, "runner-1")
        after = datetime.utcnow()

        # State machine's heartbeat should be updated
        executor._runner_states["runner-1"].update_heartbeat.assert_called()


class TestHandleStepComplete:
    """Tests for handling step completion."""

    @pytest.fixture
    def executor(self):
        """Create a RemoteExecutor."""
        return RemoteExecutor()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        # Mock execute to return a runner
        mock_runner = MagicMock()
        mock_runner.id = "runner-1"
        mock_runner.status = "busy"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_runner
        db.execute = AsyncMock(return_value=mock_result)
        return db

    @pytest.mark.asyncio
    async def test_handle_step_complete_updates_runner_state(self, executor, mock_db):
        """handle_step_complete() transitions runner back to IDLE."""
        executor._runner_states["runner-1"] = MagicMock()
        executor._runner_states["runner-1"].state = RunnerState.BUSY

        await executor.handle_step_complete(
            db=mock_db,
            runner_id="runner-1",
            step_id="step-123",
            exit_code=0,
            error=None
        )

        # Should transition to idle
        executor._runner_states["runner-1"].complete_step.assert_called()


class TestHandleDisconnect:
    """Tests for handling runner disconnection."""

    @pytest.fixture
    def executor(self):
        """Create a RemoteExecutor."""
        return RemoteExecutor()

    @pytest.fixture
    def mock_db(self):
        """Create a mock database session."""
        db = AsyncMock()
        db.commit = AsyncMock()
        # Mock execute to return a runner
        mock_runner = MagicMock()
        mock_runner.id = "runner-1"
        mock_runner.status = "busy"
        mock_result = MagicMock()
        mock_result.scalar_one_or_none.return_value = mock_runner
        db.execute = AsyncMock(return_value=mock_result)
        return db

    @pytest.mark.asyncio
    async def test_handle_disconnect_removes_connection(self, executor, mock_db):
        """handle_disconnect() removes the WebSocket connection."""
        mock_ws = AsyncMock()
        executor._connections["runner-1"] = mock_ws
        executor._runner_states["runner-1"] = MagicMock()
        executor._runner_states["runner-1"].current_step_id = None

        await executor.handle_disconnect(mock_db, "runner-1")

        assert "runner-1" not in executor._connections

    @pytest.mark.asyncio
    async def test_handle_disconnect_updates_runner_status(self, executor, mock_db):
        """handle_disconnect() updates runner status in DB."""
        mock_ws = AsyncMock()
        executor._connections["runner-1"] = mock_ws
        executor._runner_states["runner-1"] = MagicMock()
        executor._runner_states["runner-1"].current_step_id = None

        await executor.handle_disconnect(mock_db, "runner-1")

        # Should transition to disconnected
        executor._runner_states["runner-1"].transition.assert_called()

    @pytest.mark.asyncio
    async def test_handle_disconnect_requeues_step_if_busy(self, executor, mock_db):
        """handle_disconnect() requeues step if runner was busy."""
        mock_ws = AsyncMock()
        executor._connections["runner-1"] = mock_ws

        state = MagicMock()
        state.state = RunnerState.BUSY
        state.current_step_id = "step-123"
        executor._runner_states["runner-1"] = state

        with patch.object(executor, '_requeue_step') as mock_requeue:
            await executor.handle_disconnect(mock_db, "runner-1")
            mock_requeue.assert_called_with(mock_db, "step-123")


class TestMonitorTimeouts:
    """Tests for timeout monitoring background task."""

    @pytest.fixture
    def executor(self):
        """Create a RemoteExecutor."""
        return RemoteExecutor()

    @pytest.mark.asyncio
    async def test_monitor_detects_ack_timeout(self, executor):
        """monitor_timeouts() detects ACK timeouts (via is_alive check)."""
        # Create an assigned runner with old assignment time
        # Note: The actual implementation checks ASSIGNED or BUSY states
        # and calls _handle_death for both when is_alive returns False
        state = MagicMock()
        state.state = RunnerState.ASSIGNED
        state.is_alive.return_value = False  # Timed out
        executor._runner_states["runner-1"] = state

        with patch.object(executor, '_handle_death') as mock_death:
            # Run one iteration
            await executor._check_timeouts()
            mock_death.assert_called_with("runner-1")

    @pytest.mark.asyncio
    async def test_monitor_detects_heartbeat_timeout(self, executor):
        """monitor_timeouts() detects heartbeat timeouts."""
        # Create a busy runner with stale heartbeat
        state = MagicMock()
        state.state = RunnerState.BUSY
        state.is_alive.return_value = False  # Heartbeat timeout
        state.current_step_id = "step-123"
        executor._runner_states["runner-1"] = state

        with patch.object(executor, '_handle_death') as mock_death:
            await executor._check_timeouts()
            mock_death.assert_called_with("runner-1")


class TestConnectionManagement:
    """Tests for WebSocket connection management."""

    @pytest.fixture
    def executor(self):
        """Create a RemoteExecutor."""
        return RemoteExecutor()

    def test_get_connection_returns_websocket(self, executor):
        """get_connection() returns stored WebSocket."""
        mock_ws = AsyncMock()
        executor._connections["runner-1"] = mock_ws

        assert executor.get_connection("runner-1") is mock_ws

    def test_get_connection_returns_none_if_not_found(self, executor):
        """get_connection() returns None if runner not connected."""
        assert executor.get_connection("unknown-runner") is None

    def test_get_connected_runner_ids_returns_list(self, executor):
        """get_connected_runner_ids() returns list of connected runners."""
        executor._connections["runner-1"] = AsyncMock()
        executor._connections["runner-2"] = AsyncMock()

        ids = executor.get_connected_runner_ids()

        assert "runner-1" in ids
        assert "runner-2" in ids

    def test_is_runner_connected_true_when_connected(self, executor):
        """is_runner_connected() returns True when runner is connected."""
        executor._connections["runner-1"] = AsyncMock()

        assert executor.is_runner_connected("runner-1") is True

    def test_is_runner_connected_false_when_not_connected(self, executor):
        """is_runner_connected() returns False when runner is not connected."""
        assert executor.is_runner_connected("unknown-runner") is False
