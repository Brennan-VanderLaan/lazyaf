"""
Unit tests for job_queue.py - In-memory async job queue.

These tests verify:
- Queue enqueue/dequeue operations
- Pending job tracking
- Non-blocking dequeue behavior
- Timeout behavior for wait_for_job
- Queue size and pending count properties
"""
import sys
from pathlib import Path
from datetime import datetime

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.services.job_queue import JobQueue, QueuedJob


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def queue():
    """Create a fresh JobQueue for each test."""
    return JobQueue()


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


def make_job(job_id: str, card_id: str = "card-default") -> QueuedJob:
    """Helper to create QueuedJob instances with minimal required fields."""
    return QueuedJob(
        id=job_id,
        card_id=card_id,
        repo_id="repo-default",
        repo_url="https://github.com/test/repo.git",
        repo_path="/repos/test",
        base_branch="main",
        card_title="Test Job",
        card_description="Test description",
    )


# -----------------------------------------------------------------------------
# QueuedJob Tests
# -----------------------------------------------------------------------------

class TestQueuedJob:
    """Tests for QueuedJob dataclass."""

    def test_queued_job_creation_with_all_fields(self):
        """Creates QueuedJob with all required fields."""
        job = QueuedJob(
            id="test-id",
            card_id="card-id",
            repo_id="repo-id",
            repo_url="https://github.com/test/repo.git",
            repo_path="/path/to/repo",
            base_branch="main",
            card_title="My Feature",
            card_description="Feature description",
        )
        assert job.id == "test-id"
        assert job.card_id == "card-id"
        assert job.repo_id == "repo-id"
        assert job.repo_url == "https://github.com/test/repo.git"
        assert job.repo_path == "/path/to/repo"
        assert job.base_branch == "main"
        assert job.card_title == "My Feature"
        assert job.card_description == "Feature description"

    def test_queued_job_has_created_at_default(self):
        """QueuedJob gets created_at timestamp by default."""
        before = datetime.utcnow()
        job = make_job("test-id")
        after = datetime.utcnow()

        assert job.created_at >= before
        assert job.created_at <= after

    def test_queued_job_with_empty_repo_url(self):
        """QueuedJob can have empty repo_url for local repos."""
        job = QueuedJob(
            id="local-job",
            card_id="card-1",
            repo_id="repo-1",
            repo_url="",
            repo_path="/local/repo/path",
            base_branch="main",
            card_title="Local Feature",
            card_description="Work on local repo",
        )
        assert job.repo_url == ""
        assert job.repo_path == "/local/repo/path"


# -----------------------------------------------------------------------------
# Enqueue Tests
# -----------------------------------------------------------------------------

class TestEnqueue:
    """Tests for JobQueue.enqueue() method."""

    async def test_enqueue_returns_job_id(self, queue, sample_job):
        """Enqueue returns the job ID."""
        result = await queue.enqueue(sample_job)
        assert result == "job-123"

    async def test_enqueue_adds_to_pending(self, queue, sample_job):
        """Enqueue adds job to pending tracking."""
        await queue.enqueue(sample_job)
        assert queue.pending_count == 1
        assert queue.get_pending("job-123") is sample_job

    async def test_enqueue_adds_to_queue(self, queue, sample_job):
        """Enqueue adds job to the async queue."""
        await queue.enqueue(sample_job)
        assert queue.queue_size == 1

    async def test_enqueue_multiple_jobs(self, queue):
        """Can enqueue multiple jobs."""
        job1 = make_job("job-1")
        job2 = make_job("job-2")
        job3 = make_job("job-3")

        await queue.enqueue(job1)
        await queue.enqueue(job2)
        await queue.enqueue(job3)

        assert queue.queue_size == 3
        assert queue.pending_count == 3

    async def test_enqueue_preserves_order(self, queue):
        """Jobs are dequeued in FIFO order."""
        job1 = make_job("first")
        job2 = make_job("second")
        job3 = make_job("third")

        await queue.enqueue(job1)
        await queue.enqueue(job2)
        await queue.enqueue(job3)

        result1 = await queue.dequeue()
        result2 = await queue.dequeue()
        result3 = await queue.dequeue()

        assert result1.id == "first"
        assert result2.id == "second"
        assert result3.id == "third"


# -----------------------------------------------------------------------------
# Dequeue Tests
# -----------------------------------------------------------------------------

class TestDequeue:
    """Tests for JobQueue.dequeue() method (non-blocking)."""

    async def test_dequeue_returns_job(self, queue, sample_job):
        """Dequeue returns the next job from queue."""
        await queue.enqueue(sample_job)
        result = await queue.dequeue()
        assert result is sample_job

    async def test_dequeue_empty_queue_returns_none(self, queue):
        """Dequeue returns None when queue is empty."""
        result = await queue.dequeue()
        assert result is None

    async def test_dequeue_is_non_blocking(self, queue):
        """Dequeue does not block when queue is empty."""
        import asyncio

        # This should return immediately, not hang
        async def dequeue_with_timeout():
            return await asyncio.wait_for(queue.dequeue(), timeout=0.1)

        result = await dequeue_with_timeout()
        assert result is None

    async def test_dequeue_removes_from_queue(self, queue, sample_job):
        """Dequeue removes job from the async queue."""
        await queue.enqueue(sample_job)
        assert queue.queue_size == 1

        await queue.dequeue()
        assert queue.queue_size == 0

    async def test_dequeue_does_not_remove_from_pending(self, queue, sample_job):
        """Dequeue does NOT remove job from pending tracking.

        Jobs stay in pending until explicitly removed (after completion).
        """
        await queue.enqueue(sample_job)
        await queue.dequeue()

        # Job should still be in pending
        assert queue.pending_count == 1
        assert queue.get_pending("job-123") is sample_job


# -----------------------------------------------------------------------------
# Wait For Job Tests
# -----------------------------------------------------------------------------

class TestWaitForJob:
    """Tests for JobQueue.wait_for_job() method (blocking with timeout)."""

    async def test_wait_for_job_returns_immediately_if_available(self, queue, sample_job):
        """Returns job immediately if one is in queue."""
        await queue.enqueue(sample_job)
        result = await queue.wait_for_job(timeout=1.0)
        assert result is sample_job

    async def test_wait_for_job_returns_none_on_timeout(self, queue):
        """Returns None when timeout expires with empty queue."""
        result = await queue.wait_for_job(timeout=0.05)
        assert result is None

    async def test_wait_for_job_waits_for_enqueue(self, queue):
        """Waits for job to be enqueued within timeout."""
        import asyncio

        job = make_job("delayed-job")

        async def delayed_enqueue():
            await asyncio.sleep(0.05)
            await queue.enqueue(job)

        # Start enqueue in background
        asyncio.create_task(delayed_enqueue())

        # Wait should receive the job
        result = await queue.wait_for_job(timeout=1.0)
        assert result is not None
        assert result.id == "delayed-job"

    async def test_wait_for_job_respects_timeout(self, queue):
        """Does not wait longer than specified timeout."""
        import asyncio
        import time

        start = time.monotonic()
        result = await queue.wait_for_job(timeout=0.1)
        elapsed = time.monotonic() - start

        assert result is None
        # Allow small timing variance (10% tolerance)
        assert elapsed >= 0.09
        assert elapsed < 0.5  # Should not wait much longer than timeout


# -----------------------------------------------------------------------------
# Pending Tracking Tests
# -----------------------------------------------------------------------------

class TestPendingTracking:
    """Tests for pending job tracking methods."""

    async def test_get_pending_returns_job(self, queue, sample_job):
        """get_pending returns the job for valid ID."""
        await queue.enqueue(sample_job)
        result = queue.get_pending("job-123")
        assert result is sample_job

    async def test_get_pending_returns_none_for_unknown_id(self, queue):
        """get_pending returns None for unknown job ID."""
        result = queue.get_pending("unknown-id")
        assert result is None

    async def test_remove_pending_removes_job(self, queue, sample_job):
        """remove_pending removes job from pending tracking."""
        await queue.enqueue(sample_job)
        assert queue.pending_count == 1

        queue.remove_pending("job-123")

        assert queue.pending_count == 0
        assert queue.get_pending("job-123") is None

    async def test_remove_pending_unknown_id_no_error(self, queue):
        """remove_pending for unknown ID does not raise error."""
        # Should not raise
        queue.remove_pending("unknown-id")
        assert queue.pending_count == 0

    async def test_pending_count_tracks_all_enqueued(self, queue):
        """pending_count includes all enqueued jobs."""
        await queue.enqueue(make_job("job-1"))
        await queue.enqueue(make_job("job-2"))
        await queue.enqueue(make_job("job-3"))

        assert queue.pending_count == 3

    async def test_pending_count_after_dequeue(self, queue):
        """pending_count does not decrease on dequeue."""
        await queue.enqueue(make_job("job-1"))
        await queue.enqueue(make_job("job-2"))

        await queue.dequeue()

        # Still 2 pending (dequeue does not remove from pending)
        assert queue.pending_count == 2

    async def test_pending_count_after_remove_pending(self, queue):
        """pending_count decreases when remove_pending is called."""
        await queue.enqueue(make_job("job-1"))
        await queue.enqueue(make_job("job-2"))

        queue.remove_pending("job-1")

        assert queue.pending_count == 1


# -----------------------------------------------------------------------------
# Queue Size Tests
# -----------------------------------------------------------------------------

class TestQueueSize:
    """Tests for queue_size property."""

    async def test_queue_size_empty(self, queue):
        """queue_size is 0 for empty queue."""
        assert queue.queue_size == 0

    async def test_queue_size_after_enqueue(self, queue):
        """queue_size increases after enqueue."""
        await queue.enqueue(make_job("job-1"))
        assert queue.queue_size == 1

        await queue.enqueue(make_job("job-2"))
        assert queue.queue_size == 2

    async def test_queue_size_after_dequeue(self, queue):
        """queue_size decreases after dequeue."""
        await queue.enqueue(make_job("job-1"))
        await queue.enqueue(make_job("job-2"))

        await queue.dequeue()
        assert queue.queue_size == 1

        await queue.dequeue()
        assert queue.queue_size == 0

    async def test_queue_size_vs_pending_count(self, queue):
        """queue_size and pending_count track different things."""
        await queue.enqueue(make_job("job-1"))
        await queue.enqueue(make_job("job-2"))

        # Both should be 2 initially
        assert queue.queue_size == 2
        assert queue.pending_count == 2

        # Dequeue one
        await queue.dequeue()

        # queue_size decreases, pending_count stays same
        assert queue.queue_size == 1
        assert queue.pending_count == 2

        # Remove from pending
        queue.remove_pending("job-1")

        # queue_size stays same, pending_count decreases
        assert queue.queue_size == 1
        assert queue.pending_count == 1


# -----------------------------------------------------------------------------
# Edge Cases
# -----------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and error handling."""

    async def test_multiple_dequeue_on_empty_queue(self, queue):
        """Multiple dequeues on empty queue all return None."""
        assert await queue.dequeue() is None
        assert await queue.dequeue() is None
        assert await queue.dequeue() is None

    async def test_duplicate_job_ids(self, queue):
        """Enqueueing jobs with same ID overwrites pending entry."""
        job1 = make_job("same-id")
        job1.card_title = "First Job"

        job2 = make_job("same-id")
        job2.card_title = "Second Job"

        await queue.enqueue(job1)
        await queue.enqueue(job2)

        # pending should have the second job (overwrites)
        pending = queue.get_pending("same-id")
        assert pending.card_title == "Second Job"

        # But queue should have both
        assert queue.queue_size == 2

    async def test_independent_queue_instances(self):
        """Different JobQueue instances are independent."""
        queue1 = JobQueue()
        queue2 = JobQueue()

        await queue1.enqueue(make_job("queue1-job"))

        assert queue1.queue_size == 1
        assert queue2.queue_size == 0

        assert queue1.pending_count == 1
        assert queue2.pending_count == 0
