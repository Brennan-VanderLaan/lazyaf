"""
Integration tests for Runners API endpoints.

These tests verify runner listing and pool scaling operations.
"""
import sys
from pathlib import Path

import pytest

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from app.models import Runner, RunnerStatus
from shared.assertions import (
    assert_status_code,
    assert_json_list_length,
    assert_json_contains,
)


class TestListRunners:
    """Tests for GET /api/runners endpoint."""

    async def test_list_runners_empty(self, client):
        """Returns empty list when no runners exist."""
        response = await client.get("/api/runners")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_runners_with_data(self, client, db_session):
        """Returns all runners when they exist."""
        # Create runners directly in database
        runner1 = Runner(status=RunnerStatus.IDLE.value)
        runner2 = Runner(status=RunnerStatus.BUSY.value)
        db_session.add(runner1)
        db_session.add(runner2)
        await db_session.commit()

        response = await client.get("/api/runners")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_runners_returns_fields(self, client, db_session):
        """Returns runners with all expected fields."""
        runner = Runner(
            status=RunnerStatus.IDLE.value,
            container_id="abc123def456",
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        response = await client.get("/api/runners")
        runners = response.json()
        assert len(runners) == 1

        result = runners[0]
        assert "id" in result
        assert "status" in result
        assert "container_id" in result
        assert "current_job_id" in result
        assert "last_heartbeat" in result


class TestRunnerStates:
    """Tests for runners in different states."""

    async def test_idle_runner(self, client, db_session):
        """Returns idle runner correctly."""
        runner = Runner(
            status=RunnerStatus.IDLE.value,
            container_id="idle123",
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        response = await client.get("/api/runners")
        result = response.json()[0]
        assert result["status"] == "idle"
        assert result["current_job_id"] is None

    async def test_busy_runner(self, client, db_session):
        """Returns busy runner with job ID."""
        runner = Runner(
            status=RunnerStatus.BUSY.value,
            container_id="busy123",
            current_job_id="job-id-12345",
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        response = await client.get("/api/runners")
        result = response.json()[0]
        assert result["status"] == "busy"
        assert result["current_job_id"] == "job-id-12345"

    async def test_offline_runner(self, client, db_session):
        """Returns offline runner correctly."""
        runner = Runner(
            status=RunnerStatus.OFFLINE.value,
            container_id=None,
        )
        db_session.add(runner)
        await db_session.commit()
        await db_session.refresh(runner)

        response = await client.get("/api/runners")
        result = response.json()[0]
        assert result["status"] == "offline"
        assert result["container_id"] is None


class TestScaleRunners:
    """Tests for POST /api/runners/scale endpoint."""

    async def test_scale_runners_request(self, client):
        """Accepts scale request with count."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": 3},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["target"] == 3
        assert "message" in result

    async def test_scale_runners_to_zero(self, client):
        """Accepts scale to zero."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": 0},
        )
        assert_status_code(response, 200)
        assert response.json()["target"] == 0

    async def test_scale_runners_returns_current_count(self, client):
        """Scale response includes current count."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": 5},
        )
        result = response.json()
        assert "current" in result
        # Currently returns 0 as scaling is not implemented
        assert isinstance(result["current"], int)

    async def test_scale_runners_missing_count_fails(self, client):
        """Scale request fails without count field."""
        response = await client.post(
            "/api/runners/scale",
            json={},
        )
        assert_status_code(response, 422)

    async def test_scale_runners_invalid_count_fails(self, client):
        """Scale request fails with non-integer count."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": "three"},
        )
        assert_status_code(response, 422)


class TestRunnerPoolManagement:
    """Tests for runner pool behavior (placeholder for future implementation)."""

    async def test_multiple_runners_different_states(self, client, db_session):
        """Can retrieve runners in mixed states."""
        runners = [
            Runner(status=RunnerStatus.IDLE.value, container_id="idle1"),
            Runner(status=RunnerStatus.BUSY.value, container_id="busy1", current_job_id="job1"),
            Runner(status=RunnerStatus.OFFLINE.value, container_id=None),
        ]
        for runner in runners:
            db_session.add(runner)
        await db_session.commit()

        response = await client.get("/api/runners")
        result = response.json()
        assert len(result) == 3

        statuses = {r["status"] for r in result}
        assert statuses == {"idle", "busy", "offline"}
