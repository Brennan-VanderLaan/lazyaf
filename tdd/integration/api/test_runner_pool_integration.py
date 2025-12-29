"""
Integration tests for Phase 3 Runner Pool functionality.

These tests verify the integration of:
- Cards start endpoint with job queue
- Jobs callback endpoint with runner pool
- Runners status and scale endpoints with runner pool

All tests mock Docker to avoid actual container operations.
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, AsyncMock

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from app.models import Job, JobStatus, Card, CardStatus
from app.services.runner_pool import runner_pool
from app.services.job_queue import job_queue
from shared.factories import repo_create_payload, card_create_payload
from shared.assertions import (
    assert_status_code,
    assert_not_found,
    assert_json_contains,
    assert_error_response,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest_asyncio.fixture
async def repo(client):
    """Create a repo for tests."""
    response = await client.post(
        "/api/repos",
        json=repo_create_payload(name="RunnerTestRepo", path="/repos/test"),
    )
    return response.json()


@pytest_asyncio.fixture
async def card(client, repo):
    """Create a card for tests."""
    response = await client.post(
        f"/api/repos/{repo['id']}/cards",
        json=card_create_payload(title="Test Feature", description="Test description"),
    )
    return response.json()


@pytest_asyncio.fixture
async def clean_runner_pool():
    """Clean runner pool state before and after each test."""
    # Clear before
    runner_pool._runners = {}
    runner_pool._running = False

    yield runner_pool

    # Clear after
    runner_pool._runners = {}
    runner_pool._running = False


@pytest_asyncio.fixture
async def clean_job_queue():
    """Clean job queue state before and after each test."""
    # Create new queue instance for each test
    job_queue._pending = {}
    # Clear the asyncio queue by draining it
    while True:
        try:
            job_queue._queue.get_nowait()
        except Exception:
            break

    yield job_queue

    # Clear after
    job_queue._pending = {}
    while True:
        try:
            job_queue._queue.get_nowait()
        except Exception:
            break


# -----------------------------------------------------------------------------
# Start Card Tests
# -----------------------------------------------------------------------------

class TestStartCardWithJobQueue:
    """Tests for POST /api/cards/{id}/start with job queue integration."""

    async def test_start_card_creates_job(self, client, card, db_session, clean_job_queue):
        """Starting a card creates a job in the database."""
        response = await client.post(f"/api/cards/{card['id']}/start")
        assert_status_code(response, 200)

        result = response.json()
        assert result["status"] == "in_progress"
        assert result["job_id"] is not None

        # Verify job was created in DB
        from sqlalchemy import select
        job_result = await db_session.execute(
            select(Job).where(Job.id == result["job_id"])
        )
        job = job_result.scalar_one_or_none()
        assert job is not None
        assert job.status == "queued"

    async def test_start_card_sets_branch_name(self, client, card, clean_job_queue):
        """Starting a card sets the branch name."""
        response = await client.post(f"/api/cards/{card['id']}/start")
        result = response.json()

        assert result["branch_name"] is not None
        assert result["branch_name"].startswith("lazyaf/")

    async def test_start_card_adds_to_queue(self, client, card, clean_job_queue):
        """Starting a card adds a job to the queue."""
        initial_pending = clean_job_queue.pending_count

        await client.post(f"/api/cards/{card['id']}/start")

        assert clean_job_queue.pending_count == initial_pending + 1

    async def test_start_card_only_todo_status(self, client, card, clean_job_queue):
        """Can only start a card in 'todo' status."""
        # First start succeeds
        await client.post(f"/api/cards/{card['id']}/start")

        # Second start fails (card is now in_progress)
        response = await client.post(f"/api/cards/{card['id']}/start")
        assert_error_response(response, 400, "Card must be in 'todo' status to start")

    async def test_start_card_not_found(self, client):
        """Returns 404 for non-existent card."""
        response = await client.post("/api/cards/nonexistent-id/start")
        assert_not_found(response, "Card")

    async def test_start_card_queued_job_has_card_info(self, client, card, clean_job_queue):
        """Queued job contains card information."""
        response = await client.post(f"/api/cards/{card['id']}/start")
        result = response.json()

        queued_job = clean_job_queue.get_pending(result["job_id"])
        assert queued_job is not None
        assert queued_job.card_id == card["id"]
        assert queued_job.card_title == "Test Feature"
        assert queued_job.card_description == "Test description"


# -----------------------------------------------------------------------------
# Job Callback Tests
# -----------------------------------------------------------------------------

class TestJobCallback:
    """Tests for POST /api/jobs/{id}/callback endpoint."""

    async def test_callback_running_updates_job(self, client, db_session, card):
        """Callback with running status updates job."""
        # Create a job
        job = Job(card_id=card["id"], status=JobStatus.QUEUED.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.post(
            f"/api/jobs/{job.id}/callback",
            json={"status": "running"},
        )
        assert_status_code(response, 200)

        # Refresh and check
        await db_session.refresh(job)
        assert job.status == "running"
        assert job.started_at is not None

    async def test_callback_completed_updates_job(self, client, db_session, card, clean_runner_pool):
        """Callback with completed status updates job and card."""
        # Create a job
        job = Job(card_id=card["id"], status=JobStatus.RUNNING.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.post(
            f"/api/jobs/{job.id}/callback",
            json={"status": "completed", "pr_url": "https://github.com/test/repo/pull/123"},
        )
        assert_status_code(response, 200)

        # Check job updated
        await db_session.refresh(job)
        assert job.status == "completed"
        assert job.completed_at is not None

        # Check card updated
        from sqlalchemy import select
        card_result = await db_session.execute(
            select(Card).where(Card.id == card["id"])
        )
        db_card = card_result.scalar_one()
        assert db_card.status == "in_review"
        assert db_card.pr_url == "https://github.com/test/repo/pull/123"

    async def test_callback_failed_updates_job(self, client, db_session, card, clean_runner_pool):
        """Callback with failed status updates job and card."""
        # Create a job
        job = Job(card_id=card["id"], status=JobStatus.RUNNING.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.post(
            f"/api/jobs/{job.id}/callback",
            json={"status": "failed", "error": "Build failed"},
        )
        assert_status_code(response, 200)

        # Check job updated
        await db_session.refresh(job)
        assert job.status == "failed"
        assert job.error == "Build failed"
        assert job.completed_at is not None

        # Check card updated
        from sqlalchemy import select
        card_result = await db_session.execute(
            select(Card).where(Card.id == card["id"])
        )
        db_card = card_result.scalar_one()
        assert db_card.status == "failed"

    async def test_callback_marks_runner_idle(self, client, db_session, card, clean_runner_pool):
        """Callback marks the runner as idle on completion."""
        # Scale up to have a runner
        await clean_runner_pool.scale(1)
        runner = list(clean_runner_pool._runners.values())[0]

        # Create a job and mark runner as busy with this job
        job = Job(card_id=card["id"], status=JobStatus.RUNNING.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        runner.status = "busy"
        runner.current_job_id = job.id

        response = await client.post(
            f"/api/jobs/{job.id}/callback",
            json={"status": "completed"},
        )
        assert_status_code(response, 200)

        # Check runner is now idle
        assert runner.status == "idle"
        assert runner.current_job_id is None

    async def test_callback_not_found(self, client):
        """Returns 404 for non-existent job."""
        response = await client.post(
            "/api/jobs/nonexistent-id/callback",
            json={"status": "completed"},
        )
        assert_not_found(response, "Job")

    async def test_callback_returns_ok(self, client, db_session, card):
        """Callback returns ok status."""
        job = Job(card_id=card["id"], status=JobStatus.RUNNING.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.post(
            f"/api/jobs/{job.id}/callback",
            json={"status": "completed"},
        )
        result = response.json()
        assert result["status"] == "ok"


# -----------------------------------------------------------------------------
# Runners Status Tests
# -----------------------------------------------------------------------------

class TestRunnersStatus:
    """Tests for GET /api/runners/status endpoint."""

    async def test_status_empty_pool(self, client, clean_runner_pool, clean_job_queue):
        """Returns status for empty pool."""
        response = await client.get("/api/runners/status")
        assert_status_code(response, 200)

        result = response.json()
        assert result["total_runners"] == 0
        assert result["idle_runners"] == 0
        assert result["busy_runners"] == 0

    async def test_status_with_runners(self, client, clean_runner_pool, clean_job_queue):
        """Returns status with runners."""
        await clean_runner_pool.scale(3)

        response = await client.get("/api/runners/status")
        result = response.json()

        assert result["total_runners"] == 3
        assert result["idle_runners"] == 3
        assert result["busy_runners"] == 0

    async def test_status_with_busy_runners(self, client, clean_runner_pool, clean_job_queue):
        """Returns status with mixed runner states."""
        await clean_runner_pool.scale(4)

        # Mark some as busy
        runners = list(clean_runner_pool._runners.values())
        runners[0].status = "busy"
        runners[1].status = "busy"

        response = await client.get("/api/runners/status")
        result = response.json()

        assert result["total_runners"] == 4
        assert result["idle_runners"] == 2
        assert result["busy_runners"] == 2

    async def test_status_includes_queue_info(self, client, clean_runner_pool, clean_job_queue):
        """Status includes queue information."""
        response = await client.get("/api/runners/status")
        result = response.json()

        assert "queued_jobs" in result
        assert "pending_jobs" in result
        assert isinstance(result["queued_jobs"], int)
        assert isinstance(result["pending_jobs"], int)


# -----------------------------------------------------------------------------
# Runners Scale Tests
# -----------------------------------------------------------------------------

class TestRunnersScale:
    """Tests for POST /api/runners/scale endpoint."""

    async def test_scale_up(self, client, clean_runner_pool):
        """Scales runner pool up."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": 5},
        )
        assert_status_code(response, 200)

        result = response.json()
        assert result["current"] == 5
        assert result["target"] == 5

        # Verify pool state
        assert clean_runner_pool.runner_count == 5

    async def test_scale_down(self, client, clean_runner_pool):
        """Scales runner pool down."""
        await clean_runner_pool.scale(5)

        response = await client.post(
            "/api/runners/scale",
            json={"count": 2},
        )
        assert_status_code(response, 200)

        result = response.json()
        assert result["previous"] == 5
        assert result["current"] == 2

    async def test_scale_to_zero(self, client, clean_runner_pool):
        """Scales runner pool to zero."""
        await clean_runner_pool.scale(3)

        response = await client.post(
            "/api/runners/scale",
            json={"count": 0},
        )
        assert_status_code(response, 200)
        assert response.json()["current"] == 0

    async def test_scale_negative_rejected(self, client, clean_runner_pool):
        """Negative count is rejected."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": -1},
        )
        assert_status_code(response, 200)  # Returns 200 with error message
        assert "error" in response.json()

    async def test_scale_too_high_rejected(self, client, clean_runner_pool):
        """Count above maximum is rejected."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": 100},
        )
        assert_status_code(response, 200)  # Returns 200 with error message
        assert "error" in response.json()

    async def test_scale_missing_count(self, client, clean_runner_pool):
        """Missing count field returns validation error."""
        response = await client.post(
            "/api/runners/scale",
            json={},
        )
        assert_status_code(response, 422)


# -----------------------------------------------------------------------------
# List Runners Tests (Updated for runner_pool)
# -----------------------------------------------------------------------------

class TestListRunnersWithPool:
    """Tests for GET /api/runners with runner_pool integration."""

    async def test_list_runners_empty(self, client, clean_runner_pool):
        """Returns empty list when pool is empty."""
        response = await client.get("/api/runners")
        assert_status_code(response, 200)
        assert response.json() == []

    async def test_list_runners_shows_pool_runners(self, client, clean_runner_pool):
        """Returns runners from the pool."""
        await clean_runner_pool.scale(3)

        response = await client.get("/api/runners")
        assert_status_code(response, 200)

        runners = response.json()
        assert len(runners) == 3

    async def test_list_runners_shows_runner_state(self, client, clean_runner_pool):
        """Runner list shows current state."""
        await clean_runner_pool.scale(2)

        runners = list(clean_runner_pool._runners.values())
        runners[0].status = "busy"
        runners[0].current_job_id = "job-123"

        response = await client.get("/api/runners")
        result = response.json()

        statuses = {r["status"] for r in result}
        assert "idle" in statuses
        assert "busy" in statuses


# -----------------------------------------------------------------------------
# End-to-End Flow Tests
# -----------------------------------------------------------------------------

class TestCardJobRunnerFlow:
    """End-to-end tests for card -> job -> runner flow."""

    async def test_full_card_start_to_complete_flow(
        self, client, repo, db_session, clean_runner_pool, clean_job_queue
    ):
        """Tests the full flow from starting a card to completion."""
        # 1. Create a card
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="E2E Test Feature"),
        )
        card_id = create_response.json()["id"]

        # 2. Start the card
        start_response = await client.post(f"/api/cards/{card_id}/start")
        assert_status_code(start_response, 200)
        job_id = start_response.json()["job_id"]

        # 3. Verify job is queued
        job_response = await client.get(f"/api/jobs/{job_id}")
        assert job_response.json()["status"] == "queued"

        # 4. Simulate runner picking up job (callback: running)
        await client.post(
            f"/api/jobs/{job_id}/callback",
            json={"status": "running"},
        )

        job_response = await client.get(f"/api/jobs/{job_id}")
        assert job_response.json()["status"] == "running"

        # 5. Simulate job completion with PR
        await client.post(
            f"/api/jobs/{job_id}/callback",
            json={"status": "completed", "pr_url": "https://github.com/test/pr/1"},
        )

        # 6. Verify card is in review
        card_response = await client.get(f"/api/cards/{card_id}")
        result = card_response.json()
        assert result["status"] == "in_review"
        assert result["pr_url"] == "https://github.com/test/pr/1"

    async def test_card_start_to_failure_flow(
        self, client, repo, db_session, clean_runner_pool, clean_job_queue
    ):
        """Tests the flow when a job fails."""
        # 1. Create and start card
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Failing Feature"),
        )
        card_id = create_response.json()["id"]

        start_response = await client.post(f"/api/cards/{card_id}/start")
        job_id = start_response.json()["job_id"]

        # 2. Callback running
        await client.post(
            f"/api/jobs/{job_id}/callback",
            json={"status": "running"},
        )

        # 3. Callback failed
        await client.post(
            f"/api/jobs/{job_id}/callback",
            json={"status": "failed", "error": "Tests failed"},
        )

        # 4. Verify card is failed
        card_response = await client.get(f"/api/cards/{card_id}")
        result = card_response.json()
        assert result["status"] == "failed"

        # 5. Verify job has error
        job_response = await client.get(f"/api/jobs/{job_id}")
        assert job_response.json()["error"] == "Tests failed"
