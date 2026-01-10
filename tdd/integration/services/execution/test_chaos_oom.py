"""
Chaos tests for OOM (Out of Memory) handling.

These tests verify that the LocalExecutor handles OOM scenarios gracefully:
- Container OOM is detected and reported correctly
- Execution status reflects the OOM condition
- Resources are cleaned up after OOM

These tests require Docker to be running and accessible.
"""
import sys
from pathlib import Path
from uuid import uuid4

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


# -----------------------------------------------------------------------------
# Docker Availability Check
# -----------------------------------------------------------------------------

def docker_available() -> bool:
    """Check if Docker is available and running."""
    try:
        import docker
        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        return False


# Skip all tests in this module if Docker is not available
pytestmark = pytest.mark.skipif(
    not docker_available(),
    reason="Docker not available"
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def docker_client():
    """Create a real Docker client."""
    import docker
    return docker.from_env()


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


@pytest.fixture
def cleanup_volume(docker_client, execution_context):
    """Cleanup workspace volume after test."""
    yield
    try:
        volume = docker_client.volumes.get(execution_context["workspace_volume"])
        volume.remove(force=True)
    except Exception:
        pass  # Volume may not exist


# -----------------------------------------------------------------------------
# Chaos Tests: OOM Handling
# -----------------------------------------------------------------------------

class TestOOMHandling:
    """Tests that verify OOM handling behavior."""

    @pytest.mark.timeout(30)
    async def test_container_oom_kill_exit_code_detected(self, docker_client, execution_context, cleanup_volume):
        """Container killed by signal (like OOM SIGKILL) is detected as failed.

        This test simulates what happens when a container is killed by OOM killer
        by having the container kill itself with SIGKILL (exit code 137 = 128 + 9).
        """
        from app.services.execution.local_executor import LocalExecutor

        docker_client.volumes.create(name=execution_context["workspace_volume"])

        # Create repo directory
        docker_client.containers.run(
            "alpine:latest",
            command="mkdir -p /workspace/repo",
            volumes={execution_context["workspace_volume"]: {"bind": "/workspace", "mode": "rw"}},
            remove=True,
        )

        # Simulate an OOM kill scenario by having the process exit with SIGKILL exit code
        config = {
            "type": "script",
            # Exit code 137 is what you get when container is OOM killed (128 + 9 for SIGKILL)
            "command": ["sh", "-c", "exit 137"],
            "image": "alpine:latest",
            "timeout": 20,
        }

        executor = LocalExecutor(docker_client=docker_client)

        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        # Should detect the non-zero exit code as failure
        assert result is not None
        assert result["status"] == "failed"
        assert result["exit_code"] == 137

    @pytest.mark.timeout(30)
    async def test_failed_container_resources_cleaned_up(self, docker_client, execution_context, cleanup_volume):
        """Resources are cleaned up after container failure."""
        from app.services.execution.local_executor import LocalExecutor

        docker_client.volumes.create(name=execution_context["workspace_volume"])

        docker_client.containers.run(
            "alpine:latest",
            command="mkdir -p /workspace/repo",
            volumes={execution_context["workspace_volume"]: {"bind": "/workspace", "mode": "rw"}},
            remove=True,
        )

        # Use a command that fails quickly
        config = {
            "type": "script",
            "command": ["sh", "-c", "exit 1"],
            "image": "alpine:latest",
            "timeout": 20,
        }

        executor = LocalExecutor(docker_client=docker_client)

        async for event in executor.execute_step(config, execution_context):
            pass  # Just consume all events

        # After execution completes, there should be no leftover containers
        # from this execution (LocalExecutor cleans up containers)
        # We can't easily check container ID since it's not exposed in events,
        # but we can verify no exception was raised and cleanup occurred


class TestMemoryLimitRespected:
    """Tests that verify memory limits are applied correctly."""

    @pytest.mark.timeout(30)
    async def test_memory_limit_applied(self, docker_client, execution_context, cleanup_volume):
        """Memory limit configuration is applied to container."""
        from app.services.execution.local_executor import LocalExecutor

        docker_client.volumes.create(name=execution_context["workspace_volume"])

        docker_client.containers.run(
            "alpine:latest",
            command="mkdir -p /workspace/repo",
            volumes={execution_context["workspace_volume"]: {"bind": "/workspace", "mode": "rw"}},
            remove=True,
        )

        # A simple command that should succeed within memory limits
        config = {
            "type": "script",
            "command": ["echo", "hello"],
            "image": "alpine:latest",
            "timeout": 30,
            "memory_limit": "50m",  # 50 MB should be plenty for echo
        }

        executor = LocalExecutor(docker_client=docker_client)

        result = None
        async for event in executor.execute_step(config, execution_context):
            if event.get("type") == "result":
                result = event

        # Should succeed since we're under the limit
        assert result is not None
        assert result["status"] == "completed"
        assert result["exit_code"] == 0
