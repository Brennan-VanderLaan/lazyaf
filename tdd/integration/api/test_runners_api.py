"""
Integration tests for Runners API endpoints.

These tests verify runner listing and pool scaling operations.
Updated for Phase 3 to use runner_pool instead of database.
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

from shared.assertions import (
    assert_status_code,
    assert_json_list_length,
)


class TestListRunners:
    """Tests for GET /api/runners endpoint."""

    async def test_list_runners_empty(self, client, clean_runner_pool):
        """Returns empty list when no runners exist."""
        response = await client.get("/api/runners")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_runners_with_data(self, client, clean_runner_pool):
        """Returns all runners when they exist."""
        # Scale up to create runners in the pool
        await clean_runner_pool.scale(2)

        response = await client.get("/api/runners")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_runners_returns_fields(self, client, clean_runner_pool):
        """Returns runners with all expected fields."""
        await clean_runner_pool.scale(1)

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

    async def test_idle_runner(self, client, clean_runner_pool):
        """Returns idle runner correctly."""
        await clean_runner_pool.scale(1)

        response = await client.get("/api/runners")
        result = response.json()[0]
        assert result["status"] == "idle"
        assert result["current_job_id"] is None

    async def test_busy_runner(self, client, clean_runner_pool):
        """Returns busy runner with job ID."""
        await clean_runner_pool.scale(1)

        # Mark runner as busy
        runner = list(clean_runner_pool._runners.values())[0]
        runner.status = "busy"
        runner.current_job_id = "job-id-12345"
        runner.container_id = "container-abc"

        response = await client.get("/api/runners")
        result = response.json()[0]
        assert result["status"] == "busy"
        assert result["current_job_id"] == "job-id-12345"


class TestScaleRunners:
    """Tests for POST /api/runners/scale endpoint."""

    async def test_scale_runners_request(self, client, clean_runner_pool):
        """Accepts scale request with count."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": 3},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["target"] == 3
        assert result["current"] == 3

    async def test_scale_runners_to_zero(self, client, clean_runner_pool):
        """Accepts scale to zero."""
        await clean_runner_pool.scale(3)

        response = await client.post(
            "/api/runners/scale",
            json={"count": 0},
        )
        assert_status_code(response, 200)
        assert response.json()["current"] == 0

    async def test_scale_runners_returns_previous_count(self, client, clean_runner_pool):
        """Scale response includes previous count."""
        await clean_runner_pool.scale(2)

        response = await client.post(
            "/api/runners/scale",
            json={"count": 5},
        )
        result = response.json()
        assert result["previous"] == 2
        assert result["current"] == 5

    async def test_scale_runners_missing_count_fails(self, client, clean_runner_pool):
        """Scale request fails without count field."""
        response = await client.post(
            "/api/runners/scale",
            json={},
        )
        assert_status_code(response, 422)

    async def test_scale_runners_invalid_count_fails(self, client, clean_runner_pool):
        """Scale request fails with non-integer count."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": "three"},
        )
        assert_status_code(response, 422)

    async def test_scale_negative_count_rejected(self, client, clean_runner_pool):
        """Negative count returns error."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": -1},
        )
        assert_status_code(response, 200)
        assert "error" in response.json()

    async def test_scale_above_maximum_rejected(self, client, clean_runner_pool):
        """Count above 10 returns error."""
        response = await client.post(
            "/api/runners/scale",
            json={"count": 15},
        )
        assert_status_code(response, 200)
        assert "error" in response.json()


class TestPoolStatus:
    """Tests for GET /api/runners/status endpoint."""

    async def test_pool_status_empty(self, client, clean_runner_pool, clean_job_queue):
        """Returns status for empty pool."""
        response = await client.get("/api/runners/status")
        assert_status_code(response, 200)

        result = response.json()
        assert result["total_runners"] == 0
        assert result["idle_runners"] == 0
        assert result["busy_runners"] == 0
        assert result["queued_jobs"] == 0
        assert result["pending_jobs"] == 0

    async def test_pool_status_with_runners(self, client, clean_runner_pool, clean_job_queue):
        """Returns status with runners."""
        await clean_runner_pool.scale(5)

        response = await client.get("/api/runners/status")
        result = response.json()

        assert result["total_runners"] == 5
        assert result["idle_runners"] == 5
        assert result["busy_runners"] == 0

    async def test_pool_status_with_mixed_states(self, client, clean_runner_pool, clean_job_queue):
        """Returns status with mixed runner states."""
        await clean_runner_pool.scale(4)

        runners = list(clean_runner_pool._runners.values())
        runners[0].status = "busy"
        runners[1].status = "busy"

        response = await client.get("/api/runners/status")
        result = response.json()

        assert result["total_runners"] == 4
        assert result["idle_runners"] == 2
        assert result["busy_runners"] == 2


class TestRunnerPoolManagement:
    """Tests for runner pool behavior."""

    async def test_multiple_runners_different_states(self, client, clean_runner_pool):
        """Can retrieve runners in mixed states."""
        await clean_runner_pool.scale(3)

        runners = list(clean_runner_pool._runners.values())
        runners[0].status = "idle"
        runners[1].status = "busy"
        runners[1].current_job_id = "job1"
        runners[2].status = "idle"

        response = await client.get("/api/runners")
        result = response.json()
        assert len(result) == 3

        statuses = [r["status"] for r in result]
        assert "idle" in statuses
        assert "busy" in statuses

    async def test_scale_preserves_busy_runners(self, client, clean_runner_pool):
        """Scaling down preserves busy runners."""
        await clean_runner_pool.scale(4)

        # Mark 2 runners as busy
        runners = list(clean_runner_pool._runners.values())
        runners[0].status = "busy"
        runners[0].current_job_id = "job-1"
        runners[1].status = "busy"
        runners[1].current_job_id = "job-2"

        # Try to scale down to 1 (but we have 2 busy)
        response = await client.post(
            "/api/runners/scale",
            json={"count": 1},
        )
        result = response.json()

        # Should still have 2 runners (the busy ones)
        assert result["current"] == 2
        assert clean_runner_pool.busy_count == 2
