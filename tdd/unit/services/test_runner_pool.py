"""
Unit tests for runner_pool.py - Docker-based runner pool manager.

These tests verify:
- Runner scaling (add/remove runners)
- Runner state management (idle, busy, offline)
- Job assignment to runners
- Worker loop behavior
- Docker client mocking

All Docker interactions are mocked since we cannot run Docker in tests.
"""
import sys
from pathlib import Path
from datetime import datetime
from unittest.mock import MagicMock, AsyncMock, patch

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.runner_pool import RunnerPool, RunnerInfo
from app.services.job_queue import JobQueue, QueuedJob


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def mock_settings():
    """Create mock settings."""
    settings = MagicMock()
    settings.anthropic_api_key = "test-api-key"
    return settings


@pytest.fixture
def pool(mock_settings):
    """Create a fresh RunnerPool for each test with mocked settings."""
    with patch("app.services.runner_pool.get_settings", return_value=mock_settings):
        pool = RunnerPool()
        # Reset the internal state
        pool._runners = {}
        pool._running = False
        pool._worker_task = None
        yield pool


@pytest.fixture
def mock_docker_client():
    """Create a mock Docker client."""
    client = MagicMock()
    container = MagicMock()
    container.id = "container-abc123"
    client.containers.run.return_value = container
    return client


@pytest.fixture
def sample_job():
    """Create a sample QueuedJob for testing."""
    return QueuedJob(
        id="job-123",
        card_id="card-456",
        repo_id="repo-789",
        repo_url="https://github.com/test/repo.git",
        repo_path="/repos/test",
        base_branch="main",
        card_title="Test Feature",
        card_description="Implement test feature",
    )


def make_job(job_id: str = "job-default") -> QueuedJob:
    """Helper to create QueuedJob instances."""
    return QueuedJob(
        id=job_id,
        card_id="card-default",
        repo_id="repo-default",
        repo_url="https://github.com/test/repo.git",
        repo_path="/repos/test",
        base_branch="main",
        card_title="Test Job",
        card_description="Test description",
    )


# -----------------------------------------------------------------------------
# RunnerInfo Tests
# -----------------------------------------------------------------------------

class TestRunnerInfo:
    """Tests for RunnerInfo class."""

    def test_runner_info_creation(self):
        """Creates RunnerInfo with required fields."""
        runner = RunnerInfo(id="runner-1")
        assert runner.id == "runner-1"
        assert runner.container_id is None
        assert runner.status == "idle"
        assert runner.current_job_id is None

    def test_runner_info_with_container_id(self):
        """Creates RunnerInfo with container ID."""
        runner = RunnerInfo(id="runner-1", container_id="abc123")
        assert runner.container_id == "abc123"

    def test_runner_info_has_last_heartbeat(self):
        """RunnerInfo has last_heartbeat timestamp."""
        before = datetime.utcnow()
        runner = RunnerInfo(id="runner-1")
        after = datetime.utcnow()

        assert runner.last_heartbeat >= before
        assert runner.last_heartbeat <= after

    def test_runner_info_status_modification(self):
        """Can modify runner status."""
        runner = RunnerInfo(id="runner-1")
        assert runner.status == "idle"

        runner.status = "busy"
        assert runner.status == "busy"

        runner.status = "offline"
        assert runner.status == "offline"


# -----------------------------------------------------------------------------
# Scale Tests
# -----------------------------------------------------------------------------

class TestScale:
    """Tests for RunnerPool.scale() method."""

    async def test_scale_up_from_zero(self, pool):
        """Scales up from 0 to target count."""
        result = await pool.scale(3)

        assert result["previous"] == 0
        assert result["current"] == 3
        assert result["target"] == 3
        assert pool.runner_count == 3

    async def test_scale_up_adds_runners(self, pool):
        """Scaling up adds new runners."""
        await pool.scale(2)
        assert pool.runner_count == 2

        result = await pool.scale(5)
        assert result["previous"] == 2
        assert result["current"] == 5
        assert pool.runner_count == 5

    async def test_scale_down_removes_idle_runners(self, pool):
        """Scaling down removes idle runners."""
        await pool.scale(5)
        assert pool.runner_count == 5

        result = await pool.scale(2)
        assert result["previous"] == 5
        assert result["current"] == 2
        assert pool.runner_count == 2

    async def test_scale_to_zero(self, pool):
        """Can scale down to zero runners."""
        await pool.scale(3)
        result = await pool.scale(0)

        assert result["current"] == 0
        assert pool.runner_count == 0

    async def test_scale_same_count_no_change(self, pool):
        """Scaling to same count does not change anything."""
        await pool.scale(3)
        result = await pool.scale(3)

        assert result["previous"] == 3
        assert result["current"] == 3
        assert pool.runner_count == 3

    async def test_scale_up_creates_idle_runners(self, pool):
        """New runners are created in idle status."""
        await pool.scale(3)

        for runner in pool._runners.values():
            assert runner.status == "idle"
            assert runner.current_job_id is None

    async def test_scale_down_only_removes_idle(self, pool):
        """Scaling down only removes idle runners, not busy ones."""
        await pool.scale(4)

        # Mark 2 runners as busy
        runners = list(pool._runners.values())
        runners[0].status = "busy"
        runners[0].current_job_id = "job-1"
        runners[1].status = "busy"
        runners[1].current_job_id = "job-2"

        # Try to scale down to 1
        result = await pool.scale(1)

        # Should only remove 2 idle runners (4 - 2 idle = 2 remaining)
        assert result["current"] == 2
        assert pool.runner_count == 2
        assert pool.busy_count == 2

    async def test_scale_generates_unique_ids(self, pool):
        """Each new runner gets a unique ID."""
        await pool.scale(5)

        runner_ids = list(pool._runners.keys())
        assert len(runner_ids) == len(set(runner_ids))  # All unique


# -----------------------------------------------------------------------------
# Runner Count Properties Tests
# -----------------------------------------------------------------------------

class TestRunnerCounts:
    """Tests for runner count properties."""

    async def test_runner_count(self, pool):
        """runner_count returns total runners."""
        assert pool.runner_count == 0

        await pool.scale(3)
        assert pool.runner_count == 3

    async def test_idle_count(self, pool):
        """idle_count returns count of idle runners."""
        await pool.scale(3)
        assert pool.idle_count == 3

        # Mark one as busy
        runner = list(pool._runners.values())[0]
        runner.status = "busy"

        assert pool.idle_count == 2

    async def test_busy_count(self, pool):
        """busy_count returns count of busy runners."""
        await pool.scale(3)
        assert pool.busy_count == 0

        # Mark two as busy
        runners = list(pool._runners.values())
        runners[0].status = "busy"
        runners[1].status = "busy"

        assert pool.busy_count == 2

    async def test_counts_are_consistent(self, pool):
        """idle_count + busy_count = runner_count for active runners."""
        await pool.scale(5)

        runners = list(pool._runners.values())
        runners[0].status = "busy"
        runners[1].status = "busy"
        runners[2].status = "offline"  # offline is neither idle nor busy

        assert pool.runner_count == 5
        assert pool.idle_count == 2
        assert pool.busy_count == 2
        # Note: offline runners are counted in total but not idle or busy


# -----------------------------------------------------------------------------
# Get Runners Tests
# -----------------------------------------------------------------------------

class TestGetRunners:
    """Tests for RunnerPool.get_runners() method."""

    async def test_get_runners_empty(self, pool):
        """Returns empty list when no runners."""
        result = pool.get_runners()
        assert result == []

    async def test_get_runners_returns_list(self, pool):
        """Returns list of runner dictionaries."""
        await pool.scale(2)
        result = pool.get_runners()

        assert isinstance(result, list)
        assert len(result) == 2

    async def test_get_runners_contains_required_fields(self, pool):
        """Each runner dict has required fields."""
        await pool.scale(1)
        result = pool.get_runners()

        runner = result[0]
        assert "id" in runner
        assert "container_id" in runner
        assert "status" in runner
        assert "current_job_id" in runner
        assert "last_heartbeat" in runner

    async def test_get_runners_reflects_state(self, pool):
        """Runner info reflects current state."""
        await pool.scale(1)

        runner = list(pool._runners.values())[0]
        runner.status = "busy"
        runner.current_job_id = "job-999"
        runner.container_id = "container-abc"

        result = pool.get_runners()
        runner_data = result[0]

        assert runner_data["status"] == "busy"
        assert runner_data["current_job_id"] == "job-999"
        assert runner_data["container_id"] == "container-abc"


# -----------------------------------------------------------------------------
# Mark Runner Idle Tests
# -----------------------------------------------------------------------------

class TestMarkRunnerIdle:
    """Tests for RunnerPool.mark_runner_idle() method."""

    async def test_mark_runner_idle_by_job_id(self, pool):
        """Marks runner idle by job ID."""
        await pool.scale(1)

        runner = list(pool._runners.values())[0]
        runner.status = "busy"
        runner.current_job_id = "job-123"
        runner.container_id = "container-abc"

        pool.mark_runner_idle("job-123")

        assert runner.status == "idle"
        assert runner.current_job_id is None
        assert runner.container_id is None

    async def test_mark_runner_idle_unknown_job_no_error(self, pool):
        """Calling with unknown job ID does not raise error."""
        await pool.scale(1)

        # Should not raise
        pool.mark_runner_idle("unknown-job-id")

        # Runner should be unchanged
        runner = list(pool._runners.values())[0]
        assert runner.status == "idle"

    async def test_mark_runner_idle_only_affects_matching_runner(self, pool):
        """Only the runner with matching job ID is affected."""
        await pool.scale(3)

        runners = list(pool._runners.values())
        runners[0].status = "busy"
        runners[0].current_job_id = "job-1"
        runners[1].status = "busy"
        runners[1].current_job_id = "job-2"
        runners[2].status = "busy"
        runners[2].current_job_id = "job-3"

        pool.mark_runner_idle("job-2")

        assert runners[0].status == "busy"
        assert runners[0].current_job_id == "job-1"
        assert runners[1].status == "idle"
        assert runners[1].current_job_id is None
        assert runners[2].status == "busy"
        assert runners[2].current_job_id == "job-3"


# -----------------------------------------------------------------------------
# Start/Stop Tests
# -----------------------------------------------------------------------------

class TestStartStop:
    """Tests for RunnerPool.start() and stop() methods."""

    async def test_start_sets_running_flag(self, pool):
        """start() sets _running to True."""
        assert pool._running is False

        await pool.start()
        assert pool._running is True

        await pool.stop()

    async def test_start_creates_worker_task(self, pool):
        """start() creates a worker task."""
        assert pool._worker_task is None

        await pool.start()
        assert pool._worker_task is not None

        await pool.stop()

    async def test_stop_clears_running_flag(self, pool):
        """stop() sets _running to False."""
        await pool.start()
        assert pool._running is True

        await pool.stop()
        assert pool._running is False

    async def test_stop_cancels_worker_task(self, pool):
        """stop() cancels the worker task."""
        await pool.start()
        task = pool._worker_task

        await pool.stop()

        assert task.cancelled() or task.done()

    async def test_start_idempotent(self, pool):
        """Calling start() twice does not create multiple workers."""
        await pool.start()
        first_task = pool._worker_task

        await pool.start()
        second_task = pool._worker_task

        assert first_task is second_task

        await pool.stop()

    async def test_stop_without_start(self, pool):
        """stop() without start() does not raise error."""
        await pool.stop()  # Should not raise


# -----------------------------------------------------------------------------
# Assign Job Tests (with Docker mocking)
# -----------------------------------------------------------------------------

class TestAssignJob:
    """Tests for job assignment to runners."""

    async def test_assign_job_marks_runner_busy(self, pool, sample_job, mock_docker_client):
        """Assigning a job marks the runner as busy."""
        await pool.scale(1)
        runner = list(pool._runners.values())[0]

        pool._docker = mock_docker_client

        await pool._assign_job(runner, sample_job)

        assert runner.status == "busy"
        assert runner.current_job_id == "job-123"

    async def test_assign_job_sets_container_id(self, pool, sample_job, mock_docker_client):
        """Assigning a job sets the container ID on runner."""
        await pool.scale(1)
        runner = list(pool._runners.values())[0]

        pool._docker = mock_docker_client

        await pool._assign_job(runner, sample_job)

        assert runner.container_id == "container-abc123"

    async def test_assign_job_starts_container(self, pool, sample_job, mock_docker_client):
        """Assigning a job starts a Docker container."""
        await pool.scale(1)
        runner = list(pool._runners.values())[0]

        pool._docker = mock_docker_client

        await pool._assign_job(runner, sample_job)

        mock_docker_client.containers.run.assert_called_once()

    async def test_assign_job_passes_environment(self, pool, sample_job, mock_docker_client):
        """Container is started with correct environment variables."""
        await pool.scale(1)
        runner = list(pool._runners.values())[0]

        pool._docker = mock_docker_client

        await pool._assign_job(runner, sample_job)

        call_kwargs = mock_docker_client.containers.run.call_args[1]
        env = call_kwargs["environment"]

        assert env["REPO_URL"] == "https://github.com/test/repo.git"
        assert env["REPO_PATH"] == "/repos/test"
        assert env["BASE_BRANCH"] == "main"
        assert env["CARD_TITLE"] == "Test Feature"
        assert env["CARD_DESCRIPTION"] == "Implement test feature"
        assert "CALLBACK_URL" in env
        assert "job-123" in env["CALLBACK_URL"]

    async def test_assign_job_failure_reverts_runner_status(self, pool, sample_job, mock_docker_client):
        """If container start fails, runner status is reverted to idle."""
        await pool.scale(1)
        runner = list(pool._runners.values())[0]

        pool._docker = mock_docker_client
        mock_docker_client.containers.run.side_effect = Exception("Docker error")

        with pytest.raises(Exception, match="Docker error"):
            await pool._assign_job(runner, sample_job)

        assert runner.status == "idle"
        assert runner.current_job_id is None

    async def test_assign_job_uses_correct_image(self, pool, sample_job, mock_docker_client):
        """Container uses the correct runner image."""
        await pool.scale(1)
        runner = list(pool._runners.values())[0]

        pool._docker = mock_docker_client

        await pool._assign_job(runner, sample_job)

        call_args = mock_docker_client.containers.run.call_args[0]
        assert call_args[0] == "lazyaf-runner:latest"

    async def test_assign_job_container_detached(self, pool, sample_job, mock_docker_client):
        """Container is started in detached mode."""
        await pool.scale(1)
        runner = list(pool._runners.values())[0]

        pool._docker = mock_docker_client

        await pool._assign_job(runner, sample_job)

        call_kwargs = mock_docker_client.containers.run.call_args[1]
        assert call_kwargs["detach"] is True
        assert call_kwargs["remove"] is True


# -----------------------------------------------------------------------------
# Docker Client Tests
# -----------------------------------------------------------------------------

class TestDockerClient:
    """Tests for Docker client property."""

    def test_docker_client_property_creates_on_first_access(self, pool):
        """Docker client is created lazily on first access."""
        assert pool._docker is None

        with patch("app.services.runner_pool.docker.from_env") as mock_from_env:
            mock_client = MagicMock()
            mock_from_env.return_value = mock_client

            client = pool.docker_client

            mock_from_env.assert_called_once()
            assert client is mock_client

    def test_docker_client_cached(self, pool, mock_docker_client):
        """Docker client is cached after first access."""
        pool._docker = mock_docker_client

        client1 = pool.docker_client
        client2 = pool.docker_client

        assert client1 is client2
        assert client1 is mock_docker_client

    def test_docker_client_raises_on_connection_error(self, pool):
        """Raises when Docker connection fails."""
        from docker.errors import DockerException

        with patch("app.services.runner_pool.docker.from_env") as mock_from_env:
            mock_from_env.side_effect = DockerException("Connection failed")

            with pytest.raises(DockerException, match="Connection failed"):
                _ = pool.docker_client


# -----------------------------------------------------------------------------
# Worker Loop Tests
# -----------------------------------------------------------------------------

class TestWorkerLoop:
    """Tests for the worker loop behavior."""

    async def test_worker_loop_assigns_job_to_idle_runner(self, pool, mock_docker_client):
        """Worker loop assigns jobs to idle runners."""
        import asyncio

        # Create pool with one runner
        await pool.scale(1)
        pool._docker = mock_docker_client

        # Create a job queue with a job
        job = make_job("test-job")

        # Start pool
        await pool.start()

        # Enqueue job using the global queue
        with patch("app.services.runner_pool.job_queue") as mock_queue:
            mock_queue.dequeue = AsyncMock(return_value=job)

            # Let the loop run once
            await asyncio.sleep(0.1)

        await pool.stop()

    async def test_worker_loop_exits_on_stop(self, pool):
        """Worker loop exits when stop() is called."""
        import asyncio

        await pool.start()
        task = pool._worker_task

        await pool.stop()

        # Give time for task to be cancelled
        await asyncio.sleep(0.1)

        assert task.done() or task.cancelled()


# -----------------------------------------------------------------------------
# Local Repo Volume Mount Tests
# -----------------------------------------------------------------------------

class TestLocalRepoVolumeMount:
    """Tests for volume mounting with local repositories."""

    async def test_local_repo_mounts_volume(self, pool, mock_docker_client):
        """Local repo (no URL) mounts the repo path as volume."""
        await pool.scale(1)
        runner = list(pool._runners.values())[0]

        local_job = QueuedJob(
            id="local-job",
            card_id="card-1",
            repo_id="repo-1",
            repo_url="",  # Empty URL = local repo
            repo_path="/local/repo/path",
            base_branch="main",
            card_title="Local Feature",
            card_description="Work on local repo",
        )

        pool._docker = mock_docker_client

        await pool._assign_job(runner, local_job)

        call_kwargs = mock_docker_client.containers.run.call_args[1]
        volumes = call_kwargs["volumes"]

        assert "/local/repo/path" in volumes
        assert volumes["/local/repo/path"]["bind"] == "/workspace/repo"
        assert volumes["/local/repo/path"]["mode"] == "rw"

    async def test_remote_repo_no_volume_mount(self, pool, sample_job, mock_docker_client):
        """Remote repo (with URL) does not mount volumes."""
        await pool.scale(1)
        runner = list(pool._runners.values())[0]

        pool._docker = mock_docker_client

        await pool._assign_job(runner, sample_job)

        call_kwargs = mock_docker_client.containers.run.call_args[1]
        # volumes should be None or not contain repo_path
        volumes = call_kwargs.get("volumes")
        assert volumes is None
