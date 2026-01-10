"""
Unit tests for LocalExecutor Contract.

These tests define the interface for LocalExecutor - the service that spawns
Docker containers directly from the backend for step execution.

LocalExecutor provides:
- Docker container spawning via Docker SDK
- Workspace volume mounting
- Real-time log streaming
- Timeout handling (container killed after deadline)
- Crash detection (container dies unexpectedly)
- Idempotent execution (same key = same result)

Tests are written FIRST to define the contract, then implementation makes them pass.
"""
import sys
from pathlib import Path
from uuid import uuid4
from unittest.mock import AsyncMock, MagicMock, patch
from typing import AsyncGenerator

import pytest
import pytest_asyncio

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client for testing without real Docker."""
    client = MagicMock()
    client.containers = MagicMock()
    client.volumes = MagicMock()
    return client


@pytest.fixture
def step_config():
    """Sample step configuration for testing."""
    return {
        "type": "script",
        "command": "echo 'hello world'",
        "image": "python:3.12-slim",
        "timeout": 300,  # 5 minutes
    }


@pytest.fixture
def execution_context():
    """Sample execution context for testing."""
    return {
        "pipeline_run_id": str(uuid4()),
        "step_run_id": str(uuid4()),
        "step_index": 0,
        "execution_key": f"{uuid4()}:0:1",
        "workspace_volume": f"lazyaf-ws-{uuid4()}",
        "repo_url": "http://localhost:8000/git/test-repo.git",
        "branch": "feature/test",
    }


# -----------------------------------------------------------------------------
# Contract: Executor Interface
# -----------------------------------------------------------------------------

class TestExecutorInterface:
    """Tests that define the LocalExecutor interface."""

    def test_executor_has_execute_step_method(self):
        """LocalExecutor must have execute_step method."""
        from app.services.execution.local_executor import LocalExecutor
        assert hasattr(LocalExecutor, "execute_step")
        assert callable(getattr(LocalExecutor, "execute_step"))

    def test_execute_step_returns_async_generator(self, mock_docker_client, step_config, execution_context):
        """execute_step returns an AsyncGenerator for streaming results."""
        from app.services.execution.local_executor import LocalExecutor

        executor = LocalExecutor(docker_client=mock_docker_client)
        result = executor.execute_step(step_config, execution_context)

        # Should return an async generator
        assert hasattr(result, "__anext__")

    def test_executor_has_cancel_step_method(self):
        """LocalExecutor must have cancel_step method."""
        from app.services.execution.local_executor import LocalExecutor
        assert hasattr(LocalExecutor, "cancel_step")
        assert callable(getattr(LocalExecutor, "cancel_step"))


# -----------------------------------------------------------------------------
# Contract: Container Spawning
# -----------------------------------------------------------------------------

class TestContainerSpawning:
    """Tests that define container spawning behavior."""

    async def test_execute_step_spawns_container(self, mock_docker_client, step_config, execution_context):
        """execute_step spawns a Docker container."""
        from app.services.execution.local_executor import LocalExecutor

        # Setup mock
        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.status = "running"
        mock_container.logs = MagicMock(return_value=iter([b"output line\n"]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        # Consume the generator
        async for _ in executor.execute_step(step_config, execution_context):
            pass

        # Verify container was created
        mock_docker_client.containers.run.assert_called_once()

    async def test_execute_step_uses_correct_image(self, mock_docker_client, step_config, execution_context):
        """execute_step uses the image specified in step_config."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        async for _ in executor.execute_step(step_config, execution_context):
            pass

        # Check image argument
        call_kwargs = mock_docker_client.containers.run.call_args
        assert call_kwargs[0][0] == "python:3.12-slim"  # First positional arg is image

    async def test_execute_step_uses_default_image_when_not_specified(self, mock_docker_client, execution_context):
        """execute_step uses default image when not specified in config."""
        from app.services.execution.local_executor import LocalExecutor, DEFAULT_STEP_IMAGE

        config = {"type": "script", "command": "echo test"}

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        async for _ in executor.execute_step(config, execution_context):
            pass

        call_kwargs = mock_docker_client.containers.run.call_args
        assert call_kwargs[0][0] == DEFAULT_STEP_IMAGE


# -----------------------------------------------------------------------------
# Contract: Workspace Volume Mounting
# -----------------------------------------------------------------------------

class TestWorkspaceVolume:
    """Tests that define workspace volume mounting behavior."""

    async def test_execute_step_mounts_workspace_volume(self, mock_docker_client, step_config, execution_context):
        """execute_step mounts the workspace volume at /workspace."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        async for _ in executor.execute_step(step_config, execution_context):
            pass

        call_kwargs = mock_docker_client.containers.run.call_args[1]
        volumes = call_kwargs.get("volumes", {})

        # Workspace volume should be mounted at /workspace
        workspace_vol = execution_context["workspace_volume"]
        assert workspace_vol in volumes
        assert volumes[workspace_vol]["bind"] == "/workspace"

    async def test_execute_step_sets_working_directory(self, mock_docker_client, step_config, execution_context):
        """execute_step sets working directory to /workspace/repo."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        async for _ in executor.execute_step(step_config, execution_context):
            pass

        call_kwargs = mock_docker_client.containers.run.call_args[1]
        assert call_kwargs.get("working_dir") == "/workspace/repo"


# -----------------------------------------------------------------------------
# Contract: Log Streaming
# -----------------------------------------------------------------------------

class TestLogStreaming:
    """Tests that define log streaming behavior."""

    async def test_execute_step_streams_logs(self, mock_docker_client, step_config, execution_context):
        """execute_step yields log lines as they arrive."""
        from app.services.execution.local_executor import LocalExecutor

        log_lines = [b"line 1\n", b"line 2\n", b"line 3\n"]
        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter(log_lines))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        collected_logs = []
        async for event in executor.execute_step(step_config, execution_context):
            if event.get("type") == "log":
                collected_logs.append(event["line"])

        assert len(collected_logs) == 3
        assert "line 1" in collected_logs[0]

    async def test_execute_step_yields_status_events(self, mock_docker_client, step_config, execution_context):
        """execute_step yields status change events."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        status_events = []
        async for event in executor.execute_step(step_config, execution_context):
            if event.get("type") == "status":
                status_events.append(event["status"])

        # Should have status events for state transitions
        assert "preparing" in status_events
        assert "running" in status_events
        assert "completed" in status_events or "failed" in status_events


# -----------------------------------------------------------------------------
# Contract: Exit Code Handling
# -----------------------------------------------------------------------------

class TestExitCodeHandling:
    """Tests that define exit code handling behavior."""

    async def test_exit_code_zero_yields_completed(self, mock_docker_client, step_config, execution_context):
        """Exit code 0 yields completed status."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        final_event = None
        async for event in executor.execute_step(step_config, execution_context):
            if event.get("type") == "result":
                final_event = event

        assert final_event is not None
        assert final_event["status"] == "completed"
        assert final_event["exit_code"] == 0

    async def test_exit_code_nonzero_yields_failed(self, mock_docker_client, step_config, execution_context):
        """Non-zero exit code yields failed status."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 1})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        final_event = None
        async for event in executor.execute_step(step_config, execution_context):
            if event.get("type") == "result":
                final_event = event

        assert final_event is not None
        assert final_event["status"] == "failed"
        assert final_event["exit_code"] == 1


# -----------------------------------------------------------------------------
# Contract: Timeout Handling
# -----------------------------------------------------------------------------

class TestTimeoutHandling:
    """Tests that define timeout handling behavior."""

    async def test_timeout_kills_container(self, mock_docker_client, execution_context):
        """Container is killed when timeout is exceeded."""
        from app.services.execution.local_executor import LocalExecutor

        config = {
            "type": "script",
            "command": "sleep 1000",
            "timeout": 1,  # 1 second timeout
        }

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        # Simulate timeout by raising TimeoutError (what Docker SDK does on timeout)
        mock_container.wait = MagicMock(side_effect=TimeoutError("timed out"))
        mock_container.kill = MagicMock()
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        final_event = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                final_event = event

        # Container should have been killed
        mock_container.kill.assert_called_once()

        # Result should indicate timeout
        assert final_event is not None
        assert final_event["status"] == "timeout"

    async def test_timeout_event_includes_timeout_value(self, mock_docker_client, execution_context):
        """Timeout event includes the configured timeout value."""
        from app.services.execution.local_executor import LocalExecutor

        config = {
            "type": "script",
            "command": "sleep 1000",
            "timeout": 60,
        }

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(side_effect=TimeoutError("timeout"))
        mock_container.kill = MagicMock()
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        final_event = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                final_event = event

        assert final_event["timeout_seconds"] == 60


# -----------------------------------------------------------------------------
# Contract: Crash Detection
# -----------------------------------------------------------------------------

class TestCrashDetection:
    """Tests that define crash detection behavior."""

    async def test_container_crash_yields_failed(self, mock_docker_client, step_config, execution_context):
        """Container crash yields failed status with error message."""
        from app.services.execution.local_executor import LocalExecutor
        from docker.errors import ContainerError

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_docker_client.containers.run = MagicMock(
            side_effect=ContainerError(
                container=mock_container,
                exit_status=137,
                command="echo test",
                image="python:3.12-slim",
                stderr=b"Killed"
            )
        )

        executor = LocalExecutor(docker_client=mock_docker_client)

        final_event = None
        async for event in executor.execute_step(step_config, execution_context):
            if event.get("type") == "result":
                final_event = event

        assert final_event is not None
        assert final_event["status"] == "failed"
        assert "error" in final_event

    async def test_image_not_found_yields_failed(self, mock_docker_client, execution_context):
        """Image not found yields failed status with clear error."""
        from app.services.execution.local_executor import LocalExecutor
        from docker.errors import ImageNotFound

        config = {
            "type": "script",
            "command": "echo test",
            "image": "nonexistent:image",
        }

        mock_docker_client.containers.run = MagicMock(
            side_effect=ImageNotFound("Image not found")
        )

        executor = LocalExecutor(docker_client=mock_docker_client)

        final_event = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                final_event = event

        assert final_event is not None
        assert final_event["status"] == "failed"
        assert "image" in final_event["error"].lower()


# -----------------------------------------------------------------------------
# Contract: Idempotent Execution
# -----------------------------------------------------------------------------

class TestIdempotentExecution:
    """Tests that define idempotent execution behavior."""

    async def test_execute_step_is_idempotent(self, mock_docker_client, step_config, execution_context):
        """Same execution_key returns same result without re-running."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([b"output\n"]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        # First execution
        results1 = []
        async for event in executor.execute_step(step_config, execution_context):
            results1.append(event)

        # Second execution with same key
        results2 = []
        async for event in executor.execute_step(step_config, execution_context):
            results2.append(event)

        # Container.run should only be called once
        assert mock_docker_client.containers.run.call_count == 1

        # Results should indicate cached/idempotent return
        assert any(e.get("cached") for e in results2 if e.get("type") == "result")


# -----------------------------------------------------------------------------
# Contract: Cancel Step
# -----------------------------------------------------------------------------

class TestCancelStep:
    """Tests that define cancel step behavior."""

    async def test_cancel_step_kills_container(self, mock_docker_client, step_config, execution_context):
        """cancel_step kills the running container."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.kill = MagicMock()

        # Track container by execution key
        executor = LocalExecutor(docker_client=mock_docker_client)
        executor._running_containers = {
            execution_context["execution_key"]: mock_container
        }

        await executor.cancel_step(execution_context["execution_key"])

        mock_container.kill.assert_called_once()

    async def test_cancel_step_returns_false_for_unknown_key(self, mock_docker_client):
        """cancel_step returns False for unknown execution key."""
        from app.services.execution.local_executor import LocalExecutor

        executor = LocalExecutor(docker_client=mock_docker_client)

        result = await executor.cancel_step("unknown:0:1")

        assert result is False


# -----------------------------------------------------------------------------
# Contract: Container Cleanup
# -----------------------------------------------------------------------------

class TestContainerCleanup:
    """Tests that define container cleanup behavior."""

    async def test_container_removed_after_completion(self, mock_docker_client, step_config, execution_context):
        """Container is removed after successful completion."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_container.remove = MagicMock()
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        async for _ in executor.execute_step(step_config, execution_context):
            pass

        mock_container.remove.assert_called_once()

    async def test_container_removed_after_failure(self, mock_docker_client, step_config, execution_context):
        """Container is removed after failed execution."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 1})
        mock_container.remove = MagicMock()
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        async for _ in executor.execute_step(step_config, execution_context):
            pass

        mock_container.remove.assert_called_once()


# -----------------------------------------------------------------------------
# Contract: Environment Variables
# -----------------------------------------------------------------------------

class TestEnvironmentVariables:
    """Tests that define environment variable handling."""

    async def test_execute_step_passes_environment_vars(self, mock_docker_client, execution_context):
        """execute_step passes environment variables from config."""
        from app.services.execution.local_executor import LocalExecutor

        config = {
            "type": "script",
            "command": "echo $MY_VAR",
            "environment": {
                "MY_VAR": "test_value",
                "ANOTHER_VAR": "another_value",
            },
        }

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        async for _ in executor.execute_step(config, execution_context):
            pass

        call_kwargs = mock_docker_client.containers.run.call_args[1]
        env = call_kwargs.get("environment", {})
        assert env.get("MY_VAR") == "test_value"
        assert env.get("ANOTHER_VAR") == "another_value"

    async def test_execute_step_injects_lazyaf_vars(self, mock_docker_client, step_config, execution_context):
        """execute_step injects LAZYAF_* environment variables."""
        from app.services.execution.local_executor import LocalExecutor

        mock_container = MagicMock()
        mock_container.id = "container-123"
        mock_container.logs = MagicMock(return_value=iter([]))
        mock_container.wait = MagicMock(return_value={"StatusCode": 0})
        mock_docker_client.containers.run = MagicMock(return_value=mock_container)

        executor = LocalExecutor(docker_client=mock_docker_client)

        async for _ in executor.execute_step(step_config, execution_context):
            pass

        call_kwargs = mock_docker_client.containers.run.call_args[1]
        env = call_kwargs.get("environment", {})

        # Should inject LazyAF-specific variables
        assert "LAZYAF_PIPELINE_RUN_ID" in env
        assert "LAZYAF_STEP_INDEX" in env
        assert env["LAZYAF_PIPELINE_RUN_ID"] == execution_context["pipeline_run_id"]
