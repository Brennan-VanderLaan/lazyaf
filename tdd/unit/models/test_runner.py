"""
Unit tests for Runner model.

These tests verify the Runner model's structure, status handling,
and container management behavior without touching the database.
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))

from app.models import Runner, RunnerStatus

from tdd.shared.factories import RunnerFactory
from tdd.shared.assertions import assert_model_has_id


class TestRunnerStatus:
    """Tests for RunnerStatus enum."""

    def test_runner_status_values(self):
        """RunnerStatus enum should have all expected values."""
        # Phase 12.6: Extended status set for WebSocket-based runner management
        expected_statuses = {
            "disconnected", "connecting", "idle", "assigned", "busy", "dead", "offline"
        }
        actual_statuses = {status.value for status in RunnerStatus}
        assert actual_statuses == expected_statuses

    def test_runner_status_is_string_enum(self):
        """RunnerStatus should be a string enum for JSON serialization."""
        assert issubclass(RunnerStatus, str)
        assert RunnerStatus.IDLE == "idle"
        assert RunnerStatus.BUSY == "busy"
        assert RunnerStatus.OFFLINE == "offline"
        # Phase 12.6: New WebSocket states
        assert RunnerStatus.DISCONNECTED == "disconnected"
        assert RunnerStatus.CONNECTING == "connecting"
        assert RunnerStatus.ASSIGNED == "assigned"
        assert RunnerStatus.DEAD == "dead"


class TestRunnerModel:
    """Tests for Runner SQLAlchemy model."""

    def test_runner_creation(self):
        """Runner can be created with defaults."""
        runner = RunnerFactory.build()
        assert_model_has_id(runner)
        assert runner.container_id is not None

    def test_runner_default_status_is_idle(self):
        """Runner status should default to 'idle'."""
        runner = RunnerFactory.build()
        assert runner.status == RunnerStatus.IDLE.value

    def test_runner_no_job_by_default(self):
        """Runner should have no current job by default."""
        runner = RunnerFactory.build()
        assert runner.current_job_id is None

    def test_runner_has_heartbeat(self):
        """Runner should have last_heartbeat timestamp."""
        runner = RunnerFactory.build()
        assert runner.last_heartbeat is not None

    def test_runner_table_name(self):
        """Runner model maps to 'runners' table."""
        assert Runner.__tablename__ == "runners"


class TestRunnerStates:
    """Tests for Runner in different states."""

    def test_runner_idle_state(self):
        """Runner in IDLE state has no current job."""
        runner = RunnerFactory.build()
        assert runner.status == RunnerStatus.IDLE.value
        assert runner.current_job_id is None
        assert runner.container_id is not None

    def test_runner_busy_state(self):
        """Runner in BUSY state has a current job."""
        runner = RunnerFactory.build(busy=True)
        assert runner.status == RunnerStatus.BUSY.value
        assert runner.current_job_id is not None

    def test_runner_offline_state(self):
        """Runner in OFFLINE state has no container."""
        runner = RunnerFactory.build(offline=True)
        assert runner.status == RunnerStatus.OFFLINE.value
        assert runner.container_id is None

    def test_runner_without_container(self):
        """Runner can exist without a container ID (pre-spawn)."""
        runner = RunnerFactory.build(no_container=True)
        assert runner.container_id is None
        # But should still have valid status
        assert runner.status == RunnerStatus.IDLE.value


class TestRunnerContainerManagement:
    """Tests for Runner container-related behavior."""

    def test_runner_container_id_format(self):
        """Runner container_id should be a valid Docker ID format."""
        runner = RunnerFactory.build()
        # Docker short ID is 12 hex characters
        assert runner.container_id is not None
        assert len(runner.container_id) == 12
        assert all(c in "0123456789abcdef" for c in runner.container_id)

    def test_busy_runner_has_both_container_and_job(self):
        """Busy runner should have both container and job IDs."""
        runner = RunnerFactory.build(busy=True)
        assert runner.container_id is not None
        assert runner.current_job_id is not None
