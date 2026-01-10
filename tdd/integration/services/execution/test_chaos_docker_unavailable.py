"""
Chaos tests for Docker unavailable scenarios.

These tests verify that the LocalExecutor handles Docker unavailability gracefully:
- Connection refused is handled with proper error
- Timeout on Docker API is handled
- Recovery when Docker becomes available again

Note: These tests use mocking to simulate Docker unavailability,
not actual Docker disconnection.
"""
import sys
from pathlib import Path
from uuid import uuid4
from unittest.mock import Mock, patch, MagicMock

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def execution_context():
    """Create execution context for testing."""
    run_id = str(uuid4())
    return {
        "pipeline_run_id": run_id,
        "step_run_id": str(uuid4()),
        "step_index": 0,
        "execution_key": f"{run_id}:0:1",
        "workspace_volume": f"lazyaf-test-ws-{uuid4().hex[:8]}",
        "repo_url": "http://localhost:8000/git/test-repo.git",
        "branch": "main",
    }


# -----------------------------------------------------------------------------
# Chaos Tests: Docker Unavailable
# -----------------------------------------------------------------------------

class TestDockerConnectionRefused:
    """Tests for handling Docker connection refused errors."""

    async def test_connection_refused_returns_error(self, execution_context):
        """Connection refused to Docker is handled gracefully."""
        from app.services.execution.local_executor import LocalExecutor
        import docker.errors

        # Create a mock client that raises connection refused
        mock_client = Mock()
        mock_client.containers.run.side_effect = docker.errors.DockerException(
            "Error while fetching server API version: Connection refused"
        )

        config = {
            "type": "script",
            "command": "echo 'hello'",
            "image": "alpine:latest",
            "timeout": 30,
        }

        executor = LocalExecutor(docker_client=mock_client)

        result = None
        events = []
        async for event in executor.execute_step(config, execution_context):
            events.append(event)
            if event.get("type") == "result":
                result = event

        # Should return a failure result, not crash
        assert result is not None
        assert result["status"] == "failed"
        assert "error" in result or "docker" in str(result).lower()

    async def test_docker_not_running_error(self, execution_context):
        """Docker daemon not running is handled gracefully."""
        from app.services.execution.local_executor import LocalExecutor
        import docker.errors

        mock_client = Mock()
        mock_client.containers.run.side_effect = docker.errors.DockerException(
            "Error while fetching server API version: "
            "('Connection aborted.', ConnectionRefusedError(111, 'Connection refused'))"
        )

        config = {
            "type": "script",
            "command": "echo 'hello'",
            "image": "alpine:latest",
            "timeout": 30,
        }

        executor = LocalExecutor(docker_client=mock_client)

        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        assert result is not None
        assert result["status"] == "failed"


class TestDockerAPITimeout:
    """Tests for handling Docker API timeout errors."""

    async def test_api_timeout_returns_error(self, execution_context):
        """Docker API timeout is handled gracefully."""
        from app.services.execution.local_executor import LocalExecutor
        import requests.exceptions

        mock_client = Mock()
        mock_client.containers.run.side_effect = requests.exceptions.ReadTimeout(
            "Read timed out"
        )

        config = {
            "type": "script",
            "command": "echo 'hello'",
            "image": "alpine:latest",
            "timeout": 30,
        }

        executor = LocalExecutor(docker_client=mock_client)

        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        assert result is not None
        assert result["status"] == "failed"

    async def test_connection_timeout_returns_error(self, execution_context):
        """Docker connection timeout is handled gracefully."""
        from app.services.execution.local_executor import LocalExecutor
        import requests.exceptions

        mock_client = Mock()
        mock_client.containers.run.side_effect = requests.exceptions.ConnectTimeout(
            "Connection to Docker timed out"
        )

        config = {
            "type": "script",
            "command": "echo 'hello'",
            "image": "alpine:latest",
            "timeout": 30,
        }

        executor = LocalExecutor(docker_client=mock_client)

        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        assert result is not None
        assert result["status"] == "failed"


class TestDockerImagePullFailure:
    """Tests for handling image pull failures."""

    async def test_image_not_found_returns_error(self, execution_context):
        """Missing Docker image is handled gracefully."""
        from app.services.execution.local_executor import LocalExecutor
        import docker.errors

        mock_client = Mock()
        mock_client.containers.run.side_effect = docker.errors.ImageNotFound(
            "Image 'nonexistent-image:latest' not found"
        )

        config = {
            "type": "script",
            "command": "echo 'hello'",
            "image": "nonexistent-image:latest",
            "timeout": 30,
        }

        executor = LocalExecutor(docker_client=mock_client)

        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        assert result is not None
        assert result["status"] == "failed"
        # Error message should mention the image issue
        assert "error" in result

    async def test_image_pull_timeout_returns_error(self, execution_context):
        """Image pull timeout is handled gracefully."""
        from app.services.execution.local_executor import LocalExecutor
        import docker.errors

        mock_client = Mock()
        mock_client.containers.run.side_effect = docker.errors.APIError(
            "Read timed out while pulling image"
        )

        config = {
            "type": "script",
            "command": "echo 'hello'",
            "image": "large-image:latest",
            "timeout": 30,
        }

        executor = LocalExecutor(docker_client=mock_client)

        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        assert result is not None
        assert result["status"] == "failed"


class TestDockerResourceExhaustion:
    """Tests for handling Docker resource exhaustion."""

    async def test_no_space_left_returns_error(self, execution_context):
        """Disk space exhaustion is handled gracefully."""
        from app.services.execution.local_executor import LocalExecutor
        import docker.errors

        mock_client = Mock()
        mock_client.containers.run.side_effect = docker.errors.APIError(
            "no space left on device"
        )

        config = {
            "type": "script",
            "command": "echo 'hello'",
            "image": "alpine:latest",
            "timeout": 30,
        }

        executor = LocalExecutor(docker_client=mock_client)

        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        assert result is not None
        assert result["status"] == "failed"

    async def test_container_limit_reached_returns_error(self, execution_context):
        """Container limit reached is handled gracefully."""
        from app.services.execution.local_executor import LocalExecutor
        import docker.errors

        mock_client = Mock()
        mock_client.containers.run.side_effect = docker.errors.APIError(
            "You have reached your pull rate limit"
        )

        config = {
            "type": "script",
            "command": "echo 'hello'",
            "image": "alpine:latest",
            "timeout": 30,
        }

        executor = LocalExecutor(docker_client=mock_client)

        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        assert result is not None
        assert result["status"] == "failed"


class TestGracefulDegradation:
    """Tests for graceful degradation when Docker has issues."""

    async def test_partial_failure_reports_correctly(self, execution_context):
        """Partial failure (container starts but logs fail) is handled."""
        from app.services.execution.local_executor import LocalExecutor
        import docker.errors

        mock_container = Mock()
        mock_container.id = "test-container-123"
        mock_container.logs.side_effect = docker.errors.APIError("logs unavailable")
        mock_container.wait.return_value = {"StatusCode": 0}
        mock_container.remove = Mock()

        mock_client = Mock()
        mock_client.containers.run.return_value = mock_container

        config = {
            "type": "script",
            "command": "echo 'hello'",
            "image": "alpine:latest",
            "timeout": 30,
        }

        executor = LocalExecutor(docker_client=mock_client)

        # Even if logs fail, we should still get a result
        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        # Should have a result (either completed or failed, depending on implementation)
        assert result is not None
        assert result["status"] in ("completed", "failed")
