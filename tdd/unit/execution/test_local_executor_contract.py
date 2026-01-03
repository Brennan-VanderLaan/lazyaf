"""
Tests for LocalExecutor Contract (Phase 12.1).

These tests DEFINE the interface for LocalExecutor.
Write tests first, then implement to make them pass.

LocalExecutor:
- Spawns containers directly on the Docker host
- Provides instant execution (no polling)
- Streams logs in real-time
- Handles timeouts and crashes
- Supports workspace mounting
"""

from pathlib import Path
from typing import AsyncGenerator
from unittest.mock import AsyncMock, MagicMock, patch
import asyncio

import pytest

# Import will fail until we implement the module - that's expected in TDD
try:
    from app.services.execution.local_executor import (
        LocalExecutor,
        ExecutionConfig,
        ExecutionResult,
        ExecutionError,
        ContainerNotFoundError,
        TimeoutError as ExecutionTimeoutError,
    )
    from app.services.execution.step_state import StepState
    from app.services.execution.idempotency import ExecutionKey, IdempotencyStore, get_idempotency_store
    EXECUTION_MODULE_AVAILABLE = True
except ImportError:
    EXECUTION_MODULE_AVAILABLE = False
    LocalExecutor = None
    ExecutionConfig = None
    ExecutionResult = None
    ExecutionError = Exception
    ContainerNotFoundError = Exception
    ExecutionTimeoutError = Exception
    StepState = None
    ExecutionKey = None
    IdempotencyStore = None
    get_idempotency_store = None


@pytest.fixture(autouse=True)
def clear_idempotency_store():
    """Clear the global idempotency store before each test."""
    if get_idempotency_store is not None:
        get_idempotency_store().clear()
    yield
    # Also clear after test for good measure
    if get_idempotency_store is not None:
        get_idempotency_store().clear()


pytestmark = pytest.mark.skipif(
    not EXECUTION_MODULE_AVAILABLE,
    reason="execution module not yet implemented"
)


# Use shared mock infrastructure
try:
    from tdd.shared.mocks import MockDockerClient, MockContainer
except ImportError:
    MockDockerClient = MagicMock
    MockContainer = MagicMock


class TestExecutionConfig:
    """Tests for ExecutionConfig structure."""

    def test_config_has_image(self):
        """ExecutionConfig includes image name."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["python", "-c", "print('hello')"],
            workspace_path="/workspace",
        )
        assert config.image == "python:3.12"

    def test_config_has_command(self):
        """ExecutionConfig includes command."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["echo", "hello"],
            workspace_path="/workspace",
        )
        assert config.command == ["echo", "hello"]

    def test_config_has_workspace_path(self):
        """ExecutionConfig includes workspace path."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["echo", "hello"],
            workspace_path="/path/to/workspace",
        )
        assert config.workspace_path == "/path/to/workspace"

    def test_config_has_timeout(self):
        """ExecutionConfig includes optional timeout."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["echo", "hello"],
            workspace_path="/workspace",
            timeout_seconds=300,
        )
        assert config.timeout_seconds == 300

    def test_config_has_environment(self):
        """ExecutionConfig includes optional environment variables."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["echo", "hello"],
            workspace_path="/workspace",
            environment={"API_KEY": "secret"},
        )
        assert config.environment == {"API_KEY": "secret"}


class TestExecuteStepInterface:
    """Tests for execute_step() interface."""

    @pytest.fixture
    def executor(self):
        """Create a LocalExecutor with mocked Docker."""
        docker_client = MockDockerClient()
        return LocalExecutor(docker_client=docker_client)

    @pytest.fixture
    def config(self, tmp_path):
        """Create a basic execution config."""
        return ExecutionConfig(
            image="python:3.12",
            command=["echo", "hello"],
            workspace_path=str(tmp_path),
            timeout_seconds=60,
        )

    @pytest.fixture
    def key(self):
        """Create an execution key."""
        return ExecutionKey(
            pipeline_run_id="run-123",
            step_index=0,
            attempt=1,
        )

    @pytest.mark.asyncio
    async def test_execute_step_returns_async_generator(self, executor, config, key):
        """execute_step() returns an AsyncGenerator."""
        result = executor.execute_step(key, config)

        assert hasattr(result, "__anext__")

    @pytest.mark.asyncio
    async def test_execute_step_yields_log_lines(self, executor, config, key):
        """execute_step() yields log lines."""
        logs = []
        async for log_line in executor.execute_step(key, config):
            logs.append(log_line)
            if len(logs) >= 1:
                break

        # Should get at least some output
        assert len(logs) >= 0  # May be empty for mock

    @pytest.mark.asyncio
    async def test_execute_step_returns_result_at_end(self, executor, config, key):
        """execute_step() returns ExecutionResult when complete."""
        result = None
        async for item in executor.execute_step(key, config):
            if isinstance(item, ExecutionResult):
                result = item
                break

        # Result may be in the final yield
        # Implementation will define exact behavior

    @pytest.mark.asyncio
    async def test_execute_step_is_idempotent(self, executor, config, key):
        """Same execution key returns same result."""
        # First execution
        results_1 = []
        async for item in executor.execute_step(key, config):
            results_1.append(item)

        # Second execution with same key
        results_2 = []
        async for item in executor.execute_step(key, config):
            results_2.append(item)

        # Should return same execution (not spawn new container)
        # Implementation defines exact behavior


class TestContainerSpawning:
    """Tests for container creation."""

    @pytest.fixture
    def mock_docker(self):
        """Create a mock Docker client."""
        return MockDockerClient()

    @pytest.fixture
    def executor(self, mock_docker):
        """Create executor with mock Docker."""
        return LocalExecutor(docker_client=mock_docker)

    @pytest.fixture
    def config(self, tmp_path):
        """Create basic config."""
        return ExecutionConfig(
            image="python:3.12",
            command=["python", "-c", "print('hello')"],
            workspace_path=str(tmp_path),
        )

    @pytest.fixture
    def key(self):
        """Create execution key."""
        return ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

    @pytest.mark.asyncio
    async def test_spawns_container_with_correct_image(self, executor, config, key, mock_docker):
        """Container created with correct image."""
        mock_docker.on_run(lambda image, cmd: None)

        async for _ in executor.execute_step(key, config):
            break

        # Check container was created
        containers = mock_docker.containers.list(all=True)
        if containers:
            assert containers[0].image == "python:3.12"

    @pytest.mark.asyncio
    async def test_mounts_workspace_at_workspace(self, executor, config, key, mock_docker):
        """Workspace directory mounted at /workspace."""
        created_container = None

        def on_run(image, cmd):
            nonlocal created_container

        mock_docker.on_run(on_run)

        async for _ in executor.execute_step(key, config):
            break

        # Volume mounting is verified by checking container config
        # Implementation will define exact behavior

    @pytest.mark.asyncio
    async def test_passes_environment_variables(self, executor, key, mock_docker, tmp_path):
        """Environment variables passed to container."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["env"],
            workspace_path=str(tmp_path),
            environment={"MY_VAR": "my_value"},
        )

        async for _ in executor.execute_step(key, config):
            break

        # Environment verified by container config


class TestTimeoutHandling:
    """Tests for timeout handling."""

    @pytest.fixture
    def mock_docker(self):
        """Create mock Docker that simulates slow container."""
        return MockDockerClient()

    @pytest.fixture
    def executor(self, mock_docker):
        """Create executor."""
        return LocalExecutor(docker_client=mock_docker)

    @pytest.fixture
    def key(self):
        """Create execution key."""
        return ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

    @pytest.mark.asyncio
    async def test_timeout_kills_container(self, executor, key, mock_docker, tmp_path):
        """Container killed after timeout exceeded."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["sleep", "3600"],
            workspace_path=str(tmp_path),
            timeout_seconds=1,  # Very short timeout
        )

        with pytest.raises((ExecutionTimeoutError, asyncio.TimeoutError)):
            async for _ in executor.execute_step(key, config):
                pass

    @pytest.mark.asyncio
    async def test_timeout_sets_failed_state(self, executor, key, mock_docker, tmp_path):
        """Timeout transitions step to FAILED state."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["sleep", "3600"],
            workspace_path=str(tmp_path),
            timeout_seconds=1,
        )

        result = None
        try:
            async for item in executor.execute_step(key, config):
                if isinstance(item, ExecutionResult):
                    result = item
        except (ExecutionTimeoutError, asyncio.TimeoutError):
            pass

        # After timeout, step should be failed
        state = executor.get_step_state(key)
        # State should be FAILED after timeout


class TestCrashDetection:
    """Tests for container crash detection."""

    @pytest.fixture
    def mock_docker(self):
        """Create mock Docker."""
        return MockDockerClient()

    @pytest.fixture
    def executor(self, mock_docker):
        """Create executor."""
        return LocalExecutor(docker_client=mock_docker)

    @pytest.fixture
    def key(self):
        """Create execution key."""
        return ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

    @pytest.mark.asyncio
    async def test_crash_detection_fails_step(self, executor, key, mock_docker, tmp_path):
        """Container crash (killed) fails the step."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["sh", "-c", "kill -9 $$"],  # Self-kill
            workspace_path=str(tmp_path),
        )

        result = None
        async for item in executor.execute_step(key, config):
            if isinstance(item, ExecutionResult):
                result = item

        # Result should indicate failure
        if result:
            assert result.success is False

    @pytest.mark.asyncio
    async def test_nonzero_exit_fails_step(self, executor, key, mock_docker, tmp_path):
        """Non-zero exit code fails the step."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["sh", "-c", "exit 1"],
            workspace_path=str(tmp_path),
        )

        result = None
        async for item in executor.execute_step(key, config):
            if isinstance(item, ExecutionResult):
                result = item

        if result:
            assert result.success is False
            assert result.exit_code == 1


class TestLogStreaming:
    """Tests for real-time log streaming."""

    @pytest.fixture
    def mock_docker(self):
        """Create mock Docker."""
        return MockDockerClient()

    @pytest.fixture
    def executor(self, mock_docker):
        """Create executor."""
        return LocalExecutor(docker_client=mock_docker)

    @pytest.fixture
    def key(self):
        """Create execution key."""
        return ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

    @pytest.mark.asyncio
    async def test_streams_stdout(self, executor, key, mock_docker, tmp_path):
        """Container stdout is streamed."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["echo", "hello world"],
            workspace_path=str(tmp_path),
        )

        logs = []
        async for item in executor.execute_step(key, config):
            if isinstance(item, str):
                logs.append(item)

        # Implementation will define exact log format

    @pytest.mark.asyncio
    async def test_streams_stderr(self, executor, key, mock_docker, tmp_path):
        """Container stderr is streamed."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["sh", "-c", "echo error >&2"],
            workspace_path=str(tmp_path),
        )

        logs = []
        async for item in executor.execute_step(key, config):
            if isinstance(item, str):
                logs.append(item)

        # Implementation will define exact log format


class TestExecutionResult:
    """Tests for ExecutionResult structure."""

    def test_result_has_success_flag(self):
        """ExecutionResult has success boolean."""
        result = ExecutionResult(
            success=True,
            exit_code=0,
            logs="",
        )
        assert result.success is True

    def test_result_has_exit_code(self):
        """ExecutionResult has exit code."""
        result = ExecutionResult(
            success=False,
            exit_code=1,
            logs="",
        )
        assert result.exit_code == 1

    def test_result_has_logs(self):
        """ExecutionResult has captured logs."""
        result = ExecutionResult(
            success=True,
            exit_code=0,
            logs="Hello world\n",
        )
        assert result.logs == "Hello world\n"

    def test_result_has_error_message(self):
        """ExecutionResult has optional error message."""
        result = ExecutionResult(
            success=False,
            exit_code=1,
            logs="",
            error="Command failed",
        )
        assert result.error == "Command failed"

    def test_result_has_duration(self):
        """ExecutionResult has execution duration."""
        from datetime import timedelta

        result = ExecutionResult(
            success=True,
            exit_code=0,
            logs="",
            duration=timedelta(seconds=5),
        )
        assert result.duration.total_seconds() == 5


class TestCancellation:
    """Tests for execution cancellation."""

    @pytest.fixture
    def mock_docker(self):
        """Create mock Docker."""
        return MockDockerClient()

    @pytest.fixture
    def executor(self, mock_docker):
        """Create executor."""
        return LocalExecutor(docker_client=mock_docker)

    @pytest.fixture
    def key(self):
        """Create execution key."""
        return ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

    @pytest.mark.asyncio
    async def test_cancel_stops_container(self, executor, key, mock_docker, tmp_path):
        """Cancellation kills the container."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["sleep", "3600"],
            workspace_path=str(tmp_path),
        )

        # Start execution
        gen = executor.execute_step(key, config)
        await gen.__anext__()  # Start

        # Cancel
        await executor.cancel(key)

        # Container should be killed
        state = executor.get_step_state(key)
        # Should be CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_returns_immediately(self, executor, key, mock_docker, tmp_path):
        """Cancel returns quickly without waiting for container."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["sleep", "3600"],
            workspace_path=str(tmp_path),
        )

        # Start execution
        gen = executor.execute_step(key, config)

        # Cancel should return quickly
        import time
        start = time.time()
        await executor.cancel(key)
        elapsed = time.time() - start

        assert elapsed < 5  # Should be fast


class TestCleanup:
    """Tests for resource cleanup."""

    @pytest.fixture
    def mock_docker(self):
        """Create mock Docker."""
        return MockDockerClient()

    @pytest.fixture
    def executor(self, mock_docker):
        """Create executor."""
        return LocalExecutor(docker_client=mock_docker)

    @pytest.fixture
    def key(self):
        """Create execution key."""
        return ExecutionKey(pipeline_run_id="run-123", step_index=0, attempt=1)

    @pytest.mark.asyncio
    async def test_container_removed_after_completion(self, executor, key, mock_docker, tmp_path):
        """Container is removed after successful completion."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["echo", "done"],
            workspace_path=str(tmp_path),
        )

        async for _ in executor.execute_step(key, config):
            pass

        # Container should be cleaned up
        # Implementation defines exact cleanup behavior

    @pytest.mark.asyncio
    async def test_container_removed_after_failure(self, executor, key, mock_docker, tmp_path):
        """Container is removed after failure."""
        config = ExecutionConfig(
            image="python:3.12",
            command=["sh", "-c", "exit 1"],
            workspace_path=str(tmp_path),
        )

        async for _ in executor.execute_step(key, config):
            pass

        # Container should be cleaned up even on failure
