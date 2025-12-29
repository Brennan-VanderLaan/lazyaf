"""
Unit tests for RunnerPool service - persistent runner registration model.

These tests verify runner registration, heartbeat, job assignment, and cleanup.
"""
import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.runner_pool import RunnerInfo, RunnerPool
from app.services.job_queue import QueuedJob


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
        pool._runners = {}
        pool._running = False
        pool._cleanup_task = None
        yield pool


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
        assert runner.name == "runner-runner-1"  # auto-generated from id[:8]
        assert runner.status == "idle"
        assert runner.current_job is None
        assert runner.logs == []

    def test_runner_info_with_custom_name(self):
        """Creates RunnerInfo with custom name."""
        runner = RunnerInfo(id="runner-1", name="my-runner")
        assert runner.name == "my-runner"

    def test_runner_info_has_timestamps(self):
        """RunnerInfo has last_heartbeat and registered_at timestamps."""
        before = datetime.utcnow()
        runner = RunnerInfo(id="runner-1")
        after = datetime.utcnow()

        assert runner.last_heartbeat >= before
        assert runner.last_heartbeat <= after
        assert runner.registered_at >= before
        assert runner.registered_at <= after

    def test_runner_info_status_modification(self):
        """Can modify runner status."""
        runner = RunnerInfo(id="runner-1")
        assert runner.status == "idle"

        runner.status = "busy"
        assert runner.status == "busy"

        runner.status = "offline"
        assert runner.status == "offline"

    def test_runner_info_is_alive_fresh(self):
        """Fresh runner is alive."""
        runner = RunnerInfo(id="runner-1")
        assert runner.is_alive(timeout_seconds=30) is True

    def test_runner_info_is_alive_expired(self):
        """Runner with old heartbeat is not alive."""
        runner = RunnerInfo(id="runner-1")
        runner.last_heartbeat = datetime.utcnow() - timedelta(seconds=60)
        assert runner.is_alive(timeout_seconds=30) is False


# -----------------------------------------------------------------------------
# Register Tests
# -----------------------------------------------------------------------------

class TestRegister:
    """Tests for RunnerPool.register() method."""

    def test_register_creates_runner(self, pool):
        """Register creates a new runner."""
        runner = pool.register()
        assert runner is not None
        assert pool.runner_count == 1

    def test_register_with_name(self, pool):
        """Register accepts custom name."""
        runner = pool.register(name="my-runner")
        assert runner.name == "my-runner"

    def test_register_generates_unique_ids(self, pool):
        """Each registration gets unique id."""
        r1 = pool.register()
        r2 = pool.register()
        r3 = pool.register()
        assert r1.id != r2.id != r3.id

    def test_register_increases_count(self, pool):
        """Registration increases runner count."""
        assert pool.runner_count == 0
        pool.register()
        assert pool.runner_count == 1
        pool.register()
        assert pool.runner_count == 2

    def test_register_creates_idle_runner(self, pool):
        """New runners start in idle status."""
        runner = pool.register()
        assert runner.status == "idle"


# -----------------------------------------------------------------------------
# Unregister Tests
# -----------------------------------------------------------------------------

class TestUnregister:
    """Tests for RunnerPool.unregister() method."""

    def test_unregister_removes_runner(self, pool):
        """Unregister removes the runner."""
        runner = pool.register()
        assert pool.runner_count == 1
        result = pool.unregister(runner.id)
        assert result is True
        assert pool.runner_count == 0

    def test_unregister_returns_false_for_unknown(self, pool):
        """Unregister returns False for unknown id."""
        result = pool.unregister("unknown-id")
        assert result is False

    def test_unregister_with_job_clears_it(self, pool):
        """Unregistering runner clears its job."""
        runner = pool.register()
        job = make_job("test-job")
        runner.status = "busy"
        runner.current_job = job

        with patch("asyncio.create_task"):
            pool.unregister(runner.id)

        assert pool.runner_count == 0


# -----------------------------------------------------------------------------
# Heartbeat Tests
# -----------------------------------------------------------------------------

class TestHeartbeat:
    """Tests for RunnerPool.heartbeat() method."""

    def test_heartbeat_updates_timestamp(self, pool):
        """Heartbeat updates last_heartbeat."""
        runner = pool.register()
        old_heartbeat = runner.last_heartbeat

        import time
        time.sleep(0.01)
        pool.heartbeat(runner.id)

        assert runner.last_heartbeat > old_heartbeat

    def test_heartbeat_returns_true_for_known(self, pool):
        """Heartbeat returns True for known runner."""
        runner = pool.register()
        result = pool.heartbeat(runner.id)
        assert result is True

    def test_heartbeat_returns_false_for_unknown(self, pool):
        """Heartbeat returns False for unknown runner."""
        result = pool.heartbeat("unknown-id")
        assert result is False

    def test_heartbeat_revives_offline_runner(self, pool):
        """Heartbeat changes offline runner to idle."""
        runner = pool.register()
        runner.status = "offline"
        pool.heartbeat(runner.id)
        assert runner.status == "idle"

    def test_heartbeat_keeps_busy_runner_busy(self, pool):
        """Heartbeat does not change busy runner status."""
        runner = pool.register()
        runner.status = "busy"
        pool.heartbeat(runner.id)
        assert runner.status == "busy"


# -----------------------------------------------------------------------------
# Get Job Tests
# -----------------------------------------------------------------------------

class TestGetJob:
    """Tests for RunnerPool.get_job() method."""

    @pytest.mark.asyncio
    async def test_get_job_returns_none_for_unknown_runner(self, pool):
        """get_job returns None for unknown runner."""
        job = await pool.get_job("unknown-id")
        assert job is None

    @pytest.mark.asyncio
    async def test_get_job_returns_none_for_busy_runner(self, pool):
        """get_job returns None if runner is busy."""
        runner = pool.register()
        runner.status = "busy"
        with patch("app.services.runner_pool.job_queue"):
            job = await pool.get_job(runner.id)
        assert job is None

    @pytest.mark.asyncio
    async def test_get_job_returns_none_if_queue_empty(self, pool):
        """get_job returns None if no jobs queued."""
        runner = pool.register()
        with patch("app.services.runner_pool.job_queue") as mock_queue:
            mock_queue.dequeue = AsyncMock(return_value=None)
            job = await pool.get_job(runner.id)
        assert job is None
        assert runner.status == "idle"

    @pytest.mark.asyncio
    async def test_get_job_assigns_and_marks_busy(self, pool):
        """get_job assigns job and marks runner busy."""
        runner = pool.register()
        mock_job = make_job("test-job")

        with patch("app.services.runner_pool.job_queue") as mock_queue:
            mock_queue.dequeue = AsyncMock(return_value=mock_job)
            job = await pool.get_job(runner.id)

        assert job == mock_job
        assert runner.status == "busy"
        assert runner.current_job == mock_job

    @pytest.mark.asyncio
    async def test_get_job_clears_logs(self, pool):
        """get_job clears runner logs for new job."""
        runner = pool.register()
        runner.logs = ["old log 1", "old log 2"]
        mock_job = make_job("test-job")

        with patch("app.services.runner_pool.job_queue") as mock_queue:
            mock_queue.dequeue = AsyncMock(return_value=mock_job)
            await pool.get_job(runner.id)

        assert runner.logs == []


# -----------------------------------------------------------------------------
# Complete Job Tests
# -----------------------------------------------------------------------------

class TestCompleteJob:
    """Tests for RunnerPool.complete_job() method."""

    def test_complete_job_returns_none_for_unknown(self, pool):
        """complete_job returns None for unknown runner."""
        result = pool.complete_job("unknown-id", success=True)
        assert result is None

    def test_complete_job_marks_runner_idle(self, pool):
        """complete_job marks runner as idle."""
        runner = pool.register()
        runner.status = "busy"
        runner.current_job = make_job()

        with patch("app.services.runner_pool.job_queue"):
            pool.complete_job(runner.id, success=True)

        assert runner.status == "idle"
        assert runner.current_job is None

    def test_complete_job_returns_job(self, pool):
        """complete_job returns the completed job."""
        runner = pool.register()
        job = make_job("completed-job")
        runner.status = "busy"
        runner.current_job = job

        with patch("app.services.runner_pool.job_queue"):
            result = pool.complete_job(runner.id, success=True)

        assert result == job

    def test_complete_job_returns_none_if_no_job(self, pool):
        """complete_job returns None if runner has no job."""
        runner = pool.register()
        runner.status = "idle"
        runner.current_job = None

        with patch("app.services.runner_pool.job_queue"):
            result = pool.complete_job(runner.id, success=True)

        assert result is None


# -----------------------------------------------------------------------------
# Logs Tests
# -----------------------------------------------------------------------------

class TestLogs:
    """Tests for log functionality."""

    def test_append_log_adds_line(self, pool):
        """append_log adds a log line."""
        runner = pool.register()
        pool.append_log(runner.id, "test log line")
        assert "test log line" in runner.logs

    def test_append_log_ignores_unknown_runner(self, pool):
        """append_log ignores unknown runner."""
        pool.append_log("unknown-id", "test log line")  # Should not raise

    def test_get_logs_returns_logs(self, pool):
        """get_logs returns runner logs."""
        runner = pool.register()
        pool.append_log(runner.id, "line 1")
        pool.append_log(runner.id, "line 2")
        logs = pool.get_logs(runner.id)
        assert logs == ["line 1", "line 2"]

    def test_get_logs_returns_empty_for_unknown(self, pool):
        """get_logs returns empty list for unknown runner."""
        logs = pool.get_logs("unknown-id")
        assert logs == []

    def test_append_log_truncates_at_1000(self, pool):
        """Logs are truncated to 1000 lines."""
        runner = pool.register()
        for i in range(1100):
            pool.append_log(runner.id, f"line {i}")
        assert len(runner.logs) == 1000
        assert runner.logs[0] == "line 100"  # First 100 were dropped


# -----------------------------------------------------------------------------
# Runner Count Properties Tests
# -----------------------------------------------------------------------------

class TestRunnerCounts:
    """Tests for runner count properties."""

    def test_runner_count(self, pool):
        """runner_count returns total runners."""
        assert pool.runner_count == 0
        pool.register()
        pool.register()
        assert pool.runner_count == 2

    def test_idle_count(self, pool):
        """idle_count returns count of idle runners."""
        r1 = pool.register()
        r2 = pool.register()
        assert pool.idle_count == 2

        r1.status = "busy"
        assert pool.idle_count == 1

    def test_busy_count(self, pool):
        """busy_count returns count of busy runners."""
        r1 = pool.register()
        r2 = pool.register()
        assert pool.busy_count == 0

        r1.status = "busy"
        r2.status = "busy"
        assert pool.busy_count == 2

    def test_offline_count(self, pool):
        """offline_count returns count of offline runners."""
        r1 = pool.register()
        r2 = pool.register()
        assert pool.offline_count == 0

        r1.status = "offline"
        assert pool.offline_count == 1

    def test_counts_are_consistent(self, pool):
        """idle + busy + offline = runner_count."""
        r1 = pool.register()
        r2 = pool.register()
        r3 = pool.register()
        r4 = pool.register()

        r1.status = "idle"
        r2.status = "busy"
        r3.status = "offline"
        r4.status = "busy"

        assert pool.runner_count == 4
        assert pool.idle_count == 1
        assert pool.busy_count == 2
        assert pool.offline_count == 1


# -----------------------------------------------------------------------------
# Get Runners Tests
# -----------------------------------------------------------------------------

class TestGetRunners:
    """Tests for RunnerPool.get_runners() method."""

    def test_get_runners_empty(self, pool):
        """Returns empty list when no runners."""
        result = pool.get_runners()
        assert result == []

    def test_get_runners_returns_list(self, pool):
        """Returns list of runner dictionaries."""
        pool.register()
        pool.register()
        result = pool.get_runners()

        assert isinstance(result, list)
        assert len(result) == 2

    def test_get_runners_contains_required_fields(self, pool):
        """Each runner dict has required fields."""
        pool.register()
        result = pool.get_runners()

        runner = result[0]
        assert "id" in runner
        assert "name" in runner
        assert "status" in runner
        assert "current_job_id" in runner
        assert "last_heartbeat" in runner
        assert "registered_at" in runner
        assert "log_count" in runner

    def test_get_runners_reflects_state(self, pool):
        """Runner info reflects current state."""
        runner = pool.register()
        runner.status = "busy"
        runner.current_job = make_job("job-999")

        result = pool.get_runners()
        runner_data = result[0]

        assert runner_data["status"] == "busy"
        assert runner_data["current_job_id"] == "job-999"


# -----------------------------------------------------------------------------
# Get Runner Tests
# -----------------------------------------------------------------------------

class TestGetRunner:
    """Tests for RunnerPool.get_runner() method."""

    def test_get_runner_returns_runner(self, pool):
        """get_runner returns RunnerInfo for known id."""
        registered = pool.register()
        runner = pool.get_runner(registered.id)
        assert runner == registered

    def test_get_runner_returns_none_for_unknown(self, pool):
        """get_runner returns None for unknown id."""
        runner = pool.get_runner("unknown-id")
        assert runner is None


# -----------------------------------------------------------------------------
# Start/Stop Tests
# -----------------------------------------------------------------------------

class TestStartStop:
    """Tests for RunnerPool.start() and stop() methods."""

    @pytest.mark.asyncio
    async def test_start_sets_running_flag(self, pool):
        """start() sets _running to True."""
        assert pool._running is False
        await pool.start()
        assert pool._running is True
        await pool.stop()

    @pytest.mark.asyncio
    async def test_start_creates_cleanup_task(self, pool):
        """start() creates a cleanup task."""
        assert pool._cleanup_task is None
        await pool.start()
        assert pool._cleanup_task is not None
        await pool.stop()

    @pytest.mark.asyncio
    async def test_stop_clears_running_flag(self, pool):
        """stop() sets _running to False."""
        await pool.start()
        assert pool._running is True
        await pool.stop()
        assert pool._running is False

    @pytest.mark.asyncio
    async def test_stop_cancels_cleanup_task(self, pool):
        """stop() cancels the cleanup task."""
        await pool.start()
        task = pool._cleanup_task
        await pool.stop()
        assert task.cancelled() or task.done()

    @pytest.mark.asyncio
    async def test_start_idempotent(self, pool):
        """Calling start() twice does not create multiple tasks."""
        await pool.start()
        first_task = pool._cleanup_task
        await pool.start()
        second_task = pool._cleanup_task
        assert first_task is second_task
        await pool.stop()

    @pytest.mark.asyncio
    async def test_stop_without_start(self, pool):
        """stop() without start() does not raise error."""
        await pool.stop()  # Should not raise


# -----------------------------------------------------------------------------
# Cleanup Tests
# -----------------------------------------------------------------------------

class TestCleanup:
    """Tests for dead runner cleanup."""

    def test_cleanup_marks_dead_runners_offline(self, pool):
        """Dead runners are marked offline."""
        runner = pool.register()
        runner.last_heartbeat = datetime.utcnow() - timedelta(seconds=60)
        pool._cleanup_dead_runners()
        assert runner.status == "offline"

    def test_cleanup_ignores_already_offline(self, pool):
        """Already offline runners are not affected."""
        runner = pool.register()
        runner.status = "offline"
        runner.last_heartbeat = datetime.utcnow() - timedelta(seconds=60)
        pool._cleanup_dead_runners()
        assert runner.status == "offline"

    def test_cleanup_requeues_jobs_from_dead_runners(self, pool):
        """Jobs from dead runners are requeued."""
        runner = pool.register()
        runner.status = "busy"
        job = make_job("orphan-job")
        runner.current_job = job
        runner.last_heartbeat = datetime.utcnow() - timedelta(seconds=60)

        with patch("asyncio.create_task") as mock_create_task:
            pool._cleanup_dead_runners()

        assert runner.status == "offline"
        assert runner.current_job is None
        # Verify job_queue.enqueue was passed to create_task
        mock_create_task.assert_called_once()

    def test_cleanup_ignores_alive_runners(self, pool):
        """Alive runners are not affected by cleanup."""
        runner = pool.register()
        runner.status = "busy"
        pool._cleanup_dead_runners()
        assert runner.status == "busy"
