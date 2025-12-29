"""
Unit tests for Job model.

These tests verify the Job model's structure, status handling,
and behavior without touching the database.
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import Job, JobStatus

from tdd.shared.factories import JobFactory
from tdd.shared.assertions import (
    assert_model_has_id,
    assert_model_has_timestamps,
)


class TestJobStatus:
    """Tests for JobStatus enum."""

    def test_job_status_values(self):
        """JobStatus enum should have all expected values."""
        expected_statuses = {"queued", "running", "completed", "failed"}
        actual_statuses = {status.value for status in JobStatus}
        assert actual_statuses == expected_statuses

    def test_job_status_is_string_enum(self):
        """JobStatus should be a string enum for JSON serialization."""
        assert issubclass(JobStatus, str)
        assert JobStatus.QUEUED == "queued"
        assert JobStatus.RUNNING == "running"
        assert JobStatus.COMPLETED == "completed"
        assert JobStatus.FAILED == "failed"


class TestJobModel:
    """Tests for Job SQLAlchemy model."""

    def test_job_creation_with_required_fields(self):
        """Job can be created with card_id only."""
        card_id = "test-card-id-1234"
        job = JobFactory.build(card_id=card_id)

        assert job.card_id == card_id
        assert_model_has_id(job)

    def test_job_default_status_is_queued(self):
        """Job status should default to 'queued'."""
        job = JobFactory.build()
        assert job.status == JobStatus.QUEUED.value

    def test_job_optional_fields_default_to_none(self):
        """Job optional fields should default to None."""
        job = JobFactory.build()
        assert job.runner_id is None
        assert job.error is None
        assert job.started_at is None
        assert job.completed_at is None

    def test_job_logs_default_to_empty_string(self):
        """Job logs should default to empty string."""
        job = JobFactory.build()
        assert job.logs == ""

    def test_job_table_name(self):
        """Job model maps to 'jobs' table."""
        assert Job.__tablename__ == "jobs"


class TestJobLifecycle:
    """Tests for Job in different lifecycle states."""

    def test_job_queued_state(self):
        """Job in QUEUED state has no runner or timestamps."""
        job = JobFactory.build()
        assert job.status == JobStatus.QUEUED.value
        assert job.runner_id is None
        assert job.started_at is None
        assert job.completed_at is None

    def test_job_running_state(self):
        """Job in RUNNING state has runner and start time."""
        job = JobFactory.build(running=True)
        assert job.status == JobStatus.RUNNING.value
        assert job.runner_id is not None
        assert job.started_at is not None
        assert job.completed_at is None
        assert job.logs != ""

    def test_job_completed_state(self):
        """Job in COMPLETED state has all timestamps."""
        job = JobFactory.build(completed=True)
        assert job.status == JobStatus.COMPLETED.value
        assert job.runner_id is not None
        assert job.started_at is not None
        assert job.completed_at is not None
        assert job.error is None

    def test_job_failed_state(self):
        """Job in FAILED state has error message."""
        job = JobFactory.build(failed=True)
        assert job.status == JobStatus.FAILED.value
        assert job.error is not None
        assert job.completed_at is not None


class TestJobTimestamps:
    """Tests for Job timestamp handling."""

    def test_job_has_created_at(self):
        """Job should have created_at timestamp."""
        job = JobFactory.build()
        assert job.created_at is not None

    def test_completed_job_has_duration(self):
        """Completed job should have both start and end times."""
        job = JobFactory.build(completed=True)
        assert job.started_at is not None
        assert job.completed_at is not None
        # Duration can be calculated
        assert job.completed_at >= job.started_at
