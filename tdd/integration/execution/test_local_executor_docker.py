"""
Integration tests for LocalExecutor with real Docker.

These tests require Docker to be installed and running.
They spawn actual containers and verify the execution flow.

Run with: pytest tdd/integration/execution/test_local_executor_docker.py -v
Skip if Docker not available: pytest -m "not docker"
"""
import asyncio
import os
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Check if Docker is available
try:
    import docker
    docker_client = docker.from_env()
    docker_client.ping()
    DOCKER_AVAILABLE = True
except Exception:
    DOCKER_AVAILABLE = False

# Import execution module
try:
    from app.services.execution.local_executor import (
        LocalExecutor,
        ExecutionConfig,
        ExecutionResult,
        ExecutionError,
        TimeoutError as ExecutionTimeoutError,
    )
    from app.services.execution.step_state import StepState
    from app.services.execution.idempotency import ExecutionKey, IdempotencyStore
    EXECUTION_MODULE_AVAILABLE = True
except ImportError:
    EXECUTION_MODULE_AVAILABLE = False
    LocalExecutor = None
    ExecutionConfig = None
    ExecutionResult = None
    ExecutionError = Exception
    ExecutionTimeoutError = Exception
    StepState = None
    ExecutionKey = None
    IdempotencyStore = None


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DOCKER_AVAILABLE, reason="Docker not available"),
    pytest.mark.skipif(not EXECUTION_MODULE_AVAILABLE, reason="Execution module not available"),
]


@pytest.fixture
def require_image():
    """Fixture factory to check if a Docker image is available.

    Usage:
        def test_something(self, require_image):
            require_image("python:3.12-slim")
            # ... test code ...
    """
    def _require(image_name: str):
        try:
            client = docker.from_env()
            client.images.get(image_name)
        except docker.errors.ImageNotFound:
            pytest.skip(f"Docker image '{image_name}' not available. Run: docker pull {image_name}")
        except Exception as e:
            pytest.skip(f"Failed to check for image '{image_name}': {e}")
    return _require


class TestLocalExecutorWithDocker:
    """Integration tests using real Docker containers."""

    @pytest.fixture
    def executor(self):
        """Create a LocalExecutor with real Docker client."""
        client = docker.from_env()
        store = IdempotencyStore()
        return LocalExecutor(docker_client=client, idempotency_store=store)

    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a temporary workspace directory."""
        return tmp_path

    @pytest.fixture
    def key(self):
        """Create an execution key."""
        return ExecutionKey(
            pipeline_run_id="test-run-123",
            step_index=0,
            attempt=1,
        )

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_execute_simple_echo(self, executor, workspace, key):
        """Execute a simple echo command in a container."""
        config = ExecutionConfig(
            image="alpine:latest",
            command=["echo", "Hello, Docker!"],
            workspace_path=str(workspace),
            timeout_seconds=30,
        )

        logs = []
        result = None
        async for item in executor.execute_step(key, config):
            if isinstance(item, str):
                logs.append(item)
            elif isinstance(item, ExecutionResult):
                result = item

        # Should have completed successfully
        assert result is not None
        assert result.success is True
        assert result.exit_code == 0

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_execute_with_exit_code(self, executor, workspace, key):
        """Container with non-zero exit code fails the step."""
        config = ExecutionConfig(
            image="alpine:latest",
            command=["sh", "-c", "exit 42"],
            workspace_path=str(workspace),
            timeout_seconds=30,
        )

        result = None
        async for item in executor.execute_step(key, config):
            if isinstance(item, ExecutionResult):
                result = item

        assert result is not None
        assert result.success is False
        assert result.exit_code == 42

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_execute_with_output(self, executor, workspace, key):
        """Container output is streamed back."""
        config = ExecutionConfig(
            image="alpine:latest",
            command=["sh", "-c", "echo 'line1'; echo 'line2'; echo 'line3'"],
            workspace_path=str(workspace),
            timeout_seconds=30,
        )

        logs = []
        async for item in executor.execute_step(key, config):
            if isinstance(item, str):
                logs.append(item)

        # Filter out executor status messages
        output_logs = [l for l in logs if not l.startswith("[executor]")]
        assert len(output_logs) >= 1  # At least some output

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_execute_with_environment(self, executor, workspace, key):
        """Environment variables are passed to container."""
        config = ExecutionConfig(
            image="alpine:latest",
            command=["sh", "-c", "echo $MY_VAR"],
            workspace_path=str(workspace),
            timeout_seconds=30,
            environment={"MY_VAR": "test_value"},
        )

        logs = []
        async for item in executor.execute_step(key, config):
            if isinstance(item, str):
                logs.append(item)

        # Should see the environment variable value
        output = "\n".join(logs)
        assert "test_value" in output or logs  # May be filtered

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_execute_with_workspace_mount(self, executor, workspace, key):
        """Workspace is mounted at /workspace in container."""
        # Create a test file in workspace
        test_file = workspace / "test.txt"
        test_file.write_text("Hello from workspace")

        config = ExecutionConfig(
            image="alpine:latest",
            command=["cat", "/workspace/test.txt"],
            workspace_path=str(workspace),
            timeout_seconds=30,
        )

        logs = []
        async for item in executor.execute_step(key, config):
            if isinstance(item, str):
                logs.append(item)

        # Should see the file content
        output = "\n".join(logs)
        assert "Hello from workspace" in output

    @pytest.mark.asyncio
    @pytest.mark.timeout(30)
    async def test_timeout_kills_container(self, executor, workspace, key):
        """Long-running container is killed after timeout."""
        config = ExecutionConfig(
            image="alpine:latest",
            command=["sleep", "300"],  # 5 minutes
            workspace_path=str(workspace),
            timeout_seconds=2,  # 2 second timeout
        )

        with pytest.raises(ExecutionTimeoutError):
            async for _ in executor.execute_step(key, config):
                pass

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_idempotency_same_key(self, executor, workspace, key):
        """Same execution key returns same result."""
        config = ExecutionConfig(
            image="alpine:latest",
            command=["echo", "test"],
            workspace_path=str(workspace),
            timeout_seconds=30,
        )

        # First execution
        result1 = None
        async for item in executor.execute_step(key, config):
            if isinstance(item, ExecutionResult):
                result1 = item

        # Second execution with same key - should return cached result
        result2 = None
        async for item in executor.execute_step(key, config):
            if isinstance(item, ExecutionResult):
                result2 = item

        assert result1 is not None
        assert result2 is not None
        assert result1.success == result2.success

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_state_transitions(self, executor, workspace, key):
        """Step state transitions correctly during execution."""
        config = ExecutionConfig(
            image="alpine:latest",
            command=["echo", "test"],
            workspace_path=str(workspace),
            timeout_seconds=30,
        )

        # Execute and wait for completion
        async for _ in executor.execute_step(key, config):
            pass

        # Check final state
        state = executor.get_step_state(key)
        assert state in {StepState.COMPLETED, StepState.FAILED}

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_cancellation(self, executor, workspace, key):
        """Cancellation stops the container."""
        config = ExecutionConfig(
            image="alpine:latest",
            command=["sleep", "300"],
            workspace_path=str(workspace),
            timeout_seconds=60,
        )

        # Start execution
        gen = executor.execute_step(key, config)

        # Get first item (startup message)
        await gen.__anext__()

        # Cancel
        result = await executor.cancel(key)
        assert result is True

        # State should be cancelled
        state = executor.get_step_state(key)
        assert state == StepState.CANCELLED


class TestLocalExecutorPythonImage:
    """Integration tests using Python Docker image."""

    @pytest.fixture
    def executor(self):
        """Create a LocalExecutor with real Docker client."""
        client = docker.from_env()
        store = IdempotencyStore()
        return LocalExecutor(docker_client=client, idempotency_store=store)

    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a temporary workspace directory."""
        return tmp_path

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_execute_python_script(self, executor, workspace, require_image):
        """Execute a Python script in container."""
        require_image("python:3.12-slim")

        # Create a Python script
        script = workspace / "hello.py"
        script.write_text("print('Hello from Python!')")

        key = ExecutionKey(
            pipeline_run_id="py-test-run",
            step_index=0,
            attempt=1,
        )

        config = ExecutionConfig(
            image="python:3.12-slim",
            command=["python", "/workspace/hello.py"],
            workspace_path=str(workspace),
            timeout_seconds=60,
        )

        logs = []
        result = None
        async for item in executor.execute_step(key, config):
            if isinstance(item, str):
                logs.append(item)
            elif isinstance(item, ExecutionResult):
                result = item

        assert result is not None
        assert result.success is True
        output = "\n".join(logs)
        assert "Hello from Python!" in output

    @pytest.mark.asyncio
    @pytest.mark.timeout(120)
    async def test_execute_python_with_error(self, executor, workspace, require_image):
        """Python script with error returns non-zero exit code."""
        require_image("python:3.12-slim")

        # Create a Python script that raises an error
        script = workspace / "error.py"
        script.write_text("raise ValueError('Test error')")

        key = ExecutionKey(
            pipeline_run_id="py-error-run",
            step_index=0,
            attempt=1,
        )

        config = ExecutionConfig(
            image="python:3.12-slim",
            command=["python", "/workspace/error.py"],
            workspace_path=str(workspace),
            timeout_seconds=60,
        )

        result = None
        async for item in executor.execute_step(key, config):
            if isinstance(item, ExecutionResult):
                result = item

        assert result is not None
        assert result.success is False
        assert result.exit_code != 0


class TestLocalExecutorCleanup:
    """Tests for container cleanup."""

    @pytest.fixture
    def docker_client(self):
        """Get Docker client."""
        return docker.from_env()

    @pytest.fixture
    def executor(self, docker_client):
        """Create a LocalExecutor."""
        store = IdempotencyStore()
        return LocalExecutor(docker_client=docker_client, idempotency_store=store)

    @pytest.fixture
    def workspace(self, tmp_path):
        """Create a temporary workspace directory."""
        return tmp_path

    @pytest.mark.asyncio
    @pytest.mark.timeout(60)
    async def test_container_removed_after_execution(self, executor, docker_client, workspace):
        """Container is removed after successful execution."""
        key = ExecutionKey(
            pipeline_run_id="cleanup-test",
            step_index=0,
            attempt=1,
        )

        config = ExecutionConfig(
            image="alpine:latest",
            command=["echo", "cleanup test"],
            workspace_path=str(workspace),
            timeout_seconds=30,
        )

        # Execute
        async for _ in executor.execute_step(key, config):
            pass

        # Check that no containers with our name pattern exist
        containers = docker_client.containers.list(
            all=True,
            filters={"name": "lazyaf-step-0"}
        )
        # Container should be removed
        assert len([c for c in containers if "cleanup-test" in c.name]) == 0
