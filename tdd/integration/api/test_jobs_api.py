"""
Integration tests for Jobs API endpoints.

These tests verify job retrieval, log streaming, and cancellation.
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from app.models import Job, JobStatus
from shared.factories import repo_create_payload, card_create_payload
from shared.assertions import (
    assert_status_code,
    assert_not_found,
    assert_json_contains,
    assert_error_response,
)


@pytest_asyncio.fixture
async def repo(client):
    """Create a repo for job tests."""
    response = await client.post(
        "/api/repos",
        json=repo_create_payload(name="JobTestRepo"),
    )
    return response.json()


@pytest_asyncio.fixture
async def card(client, repo):
    """Create a card for job tests."""
    response = await client.post(
        f"/api/repos/{repo['id']}/cards",
        json=card_create_payload(title="Job Test Card"),
    )
    return response.json()


@pytest_asyncio.fixture
async def job(db_session, card):
    """Create a job directly in the database for testing."""
    job = Job(
        card_id=card["id"],
        status=JobStatus.QUEUED.value,
        logs="Test log entry\n",
    )
    db_session.add(job)
    await db_session.commit()
    await db_session.refresh(job)
    return job


class TestGetJob:
    """Tests for GET /api/jobs/{job_id} endpoint."""

    async def test_get_job_exists(self, client, job):
        """Returns job when it exists."""
        response = await client.get(f"/api/jobs/{job.id}")
        assert_status_code(response, 200)
        assert_json_contains(response, {"id": job.id, "status": "queued"})

    async def test_get_job_not_found(self, client):
        """Returns 404 for non-existent job."""
        response = await client.get("/api/jobs/nonexistent-job-id")
        assert_not_found(response, "Job")

    async def test_get_job_returns_all_fields(self, client, job):
        """Returns job with all expected fields."""
        response = await client.get(f"/api/jobs/{job.id}")
        result = response.json()

        assert "id" in result
        assert "card_id" in result
        assert "status" in result
        assert "logs" in result
        assert "created_at" in result
        # Optional fields may be None
        assert "runner_id" in result
        assert "error" in result
        assert "started_at" in result
        assert "completed_at" in result


class TestGetJobLogs:
    """Tests for GET /api/jobs/{job_id}/logs endpoint."""

    async def test_get_job_logs_exists(self, client, job):
        """Returns job logs when job exists."""
        response = await client.get(f"/api/jobs/{job.id}/logs")
        assert_status_code(response, 200)
        # Logs are returned as JSON with logs, job_id, status
        result = response.json()
        assert "Test log entry" in result["logs"]
        assert result["job_id"] == job.id
        assert result["status"] == "queued"

    async def test_get_job_logs_not_found(self, client):
        """Returns 404 for non-existent job."""
        response = await client.get("/api/jobs/nonexistent/logs")
        assert_not_found(response, "Job")

    async def test_get_job_logs_empty(self, client, db_session, card):
        """Returns empty logs for job with no output."""
        job = Job(card_id=card["id"], status=JobStatus.QUEUED.value, logs="")
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.get(f"/api/jobs/{job.id}/logs")
        assert_status_code(response, 200)
        result = response.json()
        assert result["logs"] == ""
        assert result["job_id"] == job.id


class TestCancelJob:
    """Tests for POST /api/jobs/{job_id}/cancel endpoint."""

    async def test_cancel_queued_job(self, client, db_session, card):
        """Cancels a queued job successfully."""
        job = Job(card_id=card["id"], status=JobStatus.QUEUED.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.post(f"/api/jobs/{job.id}/cancel")
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "failed"
        assert result["error"] == "Cancelled by user"

    async def test_cancel_running_job(self, client, db_session, card):
        """Cancels a running job successfully."""
        job = Job(card_id=card["id"], status=JobStatus.RUNNING.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.post(f"/api/jobs/{job.id}/cancel")
        assert_status_code(response, 200)
        assert response.json()["status"] == "failed"

    async def test_cancel_completed_job_fails(self, client, db_session, card):
        """Cannot cancel an already completed job."""
        job = Job(card_id=card["id"], status=JobStatus.COMPLETED.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.post(f"/api/jobs/{job.id}/cancel")
        assert_error_response(response, 400, "Job cannot be cancelled")

    async def test_cancel_failed_job_fails(self, client, db_session, card):
        """Cannot cancel an already failed job."""
        job = Job(card_id=card["id"], status=JobStatus.FAILED.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.post(f"/api/jobs/{job.id}/cancel")
        assert_error_response(response, 400, "Job cannot be cancelled")

    async def test_cancel_job_not_found(self, client):
        """Returns 404 for non-existent job."""
        response = await client.post("/api/jobs/nonexistent/cancel")
        assert_not_found(response, "Job")


class TestJobStatusTransitions:
    """Tests for job status field values."""

    async def test_job_queued_status(self, client, db_session, card):
        """Job can be retrieved with queued status."""
        job = Job(card_id=card["id"], status=JobStatus.QUEUED.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.get(f"/api/jobs/{job.id}")
        assert response.json()["status"] == "queued"

    async def test_job_running_status(self, client, db_session, card):
        """Job can be retrieved with running status."""
        job = Job(
            card_id=card["id"],
            status=JobStatus.RUNNING.value,
            runner_id="test-runner-id",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.get(f"/api/jobs/{job.id}")
        result = response.json()
        assert result["status"] == "running"
        assert result["runner_id"] == "test-runner-id"

    async def test_job_completed_status(self, client, db_session, card):
        """Job can be retrieved with completed status."""
        job = Job(card_id=card["id"], status=JobStatus.COMPLETED.value)
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.get(f"/api/jobs/{job.id}")
        assert response.json()["status"] == "completed"

    async def test_job_failed_status_with_error(self, client, db_session, card):
        """Job can be retrieved with failed status and error message."""
        job = Job(
            card_id=card["id"],
            status=JobStatus.FAILED.value,
            error="Something went wrong",
        )
        db_session.add(job)
        await db_session.commit()
        await db_session.refresh(job)

        response = await client.get(f"/api/jobs/{job.id}")
        result = response.json()
        assert result["status"] == "failed"
        assert result["error"] == "Something went wrong"
