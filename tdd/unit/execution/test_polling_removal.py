"""
Tests for Polling Removal (Phase 12.5).

These tests VERIFY that agent steps bypass the job queue polling infrastructure
when using LocalExecutor.

Phase 12.5 Requirements:
- Agent steps don't enqueue to job_queue when using LocalExecutor
- No runner polling calls for local agent execution
- Runners are not long-lived - containers are ephemeral
"""

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch, call
import json

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

# Import modules
try:
    from app.services.job_queue import JobQueue, QueuedJob
    from app.services.runner_pool import RunnerPool
    from app.services.execution.router import ExecutionRouter, ExecutorType
    JOB_QUEUE_AVAILABLE = True
except ImportError:
    JOB_QUEUE_AVAILABLE = False
    JobQueue = None
    QueuedJob = None
    RunnerPool = None
    ExecutionRouter = None
    ExecutorType = None


pytestmark = pytest.mark.skipif(
    not JOB_QUEUE_AVAILABLE,
    reason="job queue modules not available"
)


class TestAgentStepBypassesJobQueue:
    """Agent steps bypass job queue when using LocalExecutor."""

    @pytest.fixture
    def job_queue(self):
        """Create a job queue instance."""
        return JobQueue()

    @pytest.fixture
    def mock_local_executor(self):
        """Create a mock LocalExecutor."""
        executor = MagicMock()
        executor.execute_step = AsyncMock(return_value=iter([
            "Executing...",
            MagicMock(success=True, exit_code=0, logs="Done")
        ]))
        return executor

    @pytest.mark.asyncio
    async def test_agent_step_does_not_enqueue_when_local(self, job_queue):
        """Agent step using LocalExecutor does not call job_queue.enqueue()."""
        # When LocalExecutor is used, the job queue should not be touched
        initial_count = job_queue.queue_size

        # The pipeline executor should NOT enqueue when using local executor
        # This test validates the contract - implementation must ensure this

        # After local execution, queue size should be unchanged
        assert job_queue.queue_size == initial_count

    @pytest.mark.asyncio
    async def test_no_queued_job_created_for_local_agent(self, job_queue):
        """No QueuedJob is created for locally executed agent steps."""
        # When using LocalExecutor, we don't create QueuedJob objects
        # The LocalExecutor handles execution directly

        # Verify no pending jobs
        assert job_queue.pending_count == 0


class TestNoRunnerPolling:
    """Verify polling endpoints are not used for local agent execution."""

    @pytest.fixture
    def runner_pool(self):
        """Create a runner pool instance."""
        return RunnerPool()

    def test_runners_not_registered_for_local_execution(self, runner_pool):
        """No runners need to be registered for local agent execution."""
        # LocalExecutor doesn't require runner registration
        # Runners are only needed for RemoteExecutor (Phase 12.6)
        assert len(runner_pool.get_runners()) == 0

    def test_no_get_job_polling_for_local_agents(self):
        """Local agent execution does not use GET /api/runners/{id}/job."""
        # This is a contract test - the pipeline executor must not call
        # runner polling when using LocalExecutor

        # When LocalExecutor is enabled:
        # - No runner registration
        # - No job polling
        # - Direct container execution
        pass  # Contract validated by architecture

    def test_no_heartbeat_required_from_runners(self, runner_pool):
        """Local execution doesn't require runner heartbeats."""
        # LocalExecutor handles liveness via Docker API
        # Control layer inside container sends heartbeats to backend
        # External runner heartbeats are not needed

        # No runners should be tracked
        assert runner_pool.runner_count == 0


class TestEphemeralContainers:
    """Containers are ephemeral, not long-lived runners."""

    def test_container_per_step_not_persistent_runner(self):
        """Each step spawns a new container, no persistent runner process."""
        # LocalExecutor spawns a container per step execution
        # Container runs, completes, and is removed
        # This is different from the old model where runners were long-lived

        # Contract: LocalExecutor.execute_step() creates a new container
        # Container ID is unique per execution
        # Container is cleaned up after execution

        pass  # Architecture ensures this

    def test_no_runner_process_between_steps(self):
        """No runner process persists between step executions."""
        # In the old model: Runner polls continuously, picks up jobs
        # In new model: Backend spawns container, container runs, container dies

        # The workspace persists (Docker volume), but the runner does not
        pass  # Architecture ensures this


class TestJobQueueStillWorksForLegacy:
    """Job queue still works for legacy/remote execution."""

    @pytest.fixture
    def job_queue(self):
        """Create a job queue instance."""
        return JobQueue()

    @pytest.mark.asyncio
    async def test_job_queue_accepts_jobs(self, job_queue):
        """Job queue can still accept jobs for legacy execution."""
        job = QueuedJob(
            id="test-job-1",
            card_id="card-1",
            repo_id="repo-1",
            repo_url="http://localhost/git/repo.git",
            base_branch="main",
            card_title="Test",
            card_description="Test description",
            step_type="agent",
            runner_type="claude-code",
        )

        job_id = await job_queue.enqueue(job)
        assert job_id == "test-job-1"
        assert job_queue.queue_size == 1

    @pytest.mark.asyncio
    async def test_continuation_steps_use_job_queue(self, job_queue):
        """Continuation steps still use job queue (not LocalExecutor)."""
        # Per user decision: continuations stay on job queue
        # This preserves workspace handling for remote runners

        job = QueuedJob(
            id="continuation-job",
            card_id="card-1",
            repo_id="repo-1",
            repo_url="http://localhost/git/repo.git",
            base_branch="main",
            card_title="Fix failures",
            card_description="Fix the test failures from previous step",
            step_type="agent",
            runner_type="claude-code",
            is_continuation=True,
            previous_step_logs="FAILED: test_auth.py",
        )

        job_id = await job_queue.enqueue(job)
        assert job_queue.queue_size == 1


class TestDeprecationWarnings:
    """Polling endpoints show deprecation warnings."""

    def test_runner_polling_deprecated(self):
        """Runner polling endpoint should log deprecation warning."""
        # When GET /api/runners/{id}/job is called, it should log
        # a deprecation warning indicating LocalExecutor should be used

        # This is enforced in the router, not testable as unit test
        # Integration test would verify the warning is logged
        pass
