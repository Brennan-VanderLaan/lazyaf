"""
Integration tests for Runners API endpoints.

These tests verify runner registration, heartbeat, and job polling operations.
Updated for Phase 3.5 to use persistent runner registration model.
"""
import sys
from pathlib import Path

import pytest

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
        # Register runners
        clean_runner_pool.register(name="runner-1")
        clean_runner_pool.register(name="runner-2")

        response = await client.get("/api/runners")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_runners_returns_fields(self, client, clean_runner_pool):
        """Returns runners with all expected fields."""
        clean_runner_pool.register(name="test-runner")

        response = await client.get("/api/runners")
        runners = response.json()
        assert len(runners) == 1

        result = runners[0]
        assert "id" in result
        assert "name" in result
        assert "status" in result
        assert "current_job_id" in result
        assert "last_heartbeat" in result
        assert "registered_at" in result
        assert "log_count" in result


class TestRunnerStates:
    """Tests for runners in different states."""

    async def test_idle_runner(self, client, clean_runner_pool):
        """Returns idle runner correctly."""
        clean_runner_pool.register()

        response = await client.get("/api/runners")
        result = response.json()[0]
        assert result["status"] == "idle"
        assert result["current_job_id"] is None

    async def test_busy_runner(self, client, clean_runner_pool):
        """Returns busy runner with job ID."""
        from app.services.job_queue import QueuedJob

        runner = clean_runner_pool.register()
        runner.status = "busy"
        runner.current_job = QueuedJob(
            id="job-12345",
            card_id="card-1",
            repo_id="repo-1",
            repo_url="",
            repo_path="",
            base_branch="main",
            card_title="Test",
            card_description="",
        )

        response = await client.get("/api/runners")
        result = response.json()[0]
        assert result["status"] == "busy"
        assert result["current_job_id"] == "job-12345"


class TestRegisterRunner:
    """Tests for POST /api/runners/register endpoint."""

    async def test_register_runner(self, client, clean_runner_pool):
        """Accepts register request."""
        response = await client.post(
            "/api/runners/register",
            json={"name": "my-runner"},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert "runner_id" in result
        assert result["name"] == "my-runner"

    async def test_register_runner_no_name(self, client, clean_runner_pool):
        """Accepts register without name."""
        response = await client.post(
            "/api/runners/register",
            json={},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert "runner_id" in result
        assert "name" in result

    async def test_register_creates_in_pool(self, client, clean_runner_pool):
        """Registration creates runner in pool."""
        assert clean_runner_pool.runner_count == 0

        response = await client.post(
            "/api/runners/register",
            json={"name": "test-runner"},
        )
        assert_status_code(response, 200)
        assert clean_runner_pool.runner_count == 1

    async def test_register_with_runner_id(self, client, clean_runner_pool):
        """Registration with client-provided runner_id uses that ID."""
        response = await client.post(
            "/api/runners/register",
            json={"runner_id": "my-custom-uuid", "name": "custom-runner"},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["runner_id"] == "my-custom-uuid"
        assert result["name"] == "custom-runner"

    async def test_register_reconnect_same_id(self, client, clean_runner_pool):
        """Re-registering with same runner_id reuses existing runner."""
        runner_id = "persistent-runner-id"

        # First registration
        response1 = await client.post(
            "/api/runners/register",
            json={"runner_id": runner_id, "name": "runner-v1"},
        )
        assert_status_code(response1, 200)
        assert clean_runner_pool.runner_count == 1

        # Second registration with same ID (simulating reconnect)
        response2 = await client.post(
            "/api/runners/register",
            json={"runner_id": runner_id, "name": "runner-v2"},
        )
        assert_status_code(response2, 200)
        result = response2.json()
        assert result["runner_id"] == runner_id
        assert result["name"] == "runner-v2"  # Name updated

        # Should still be just one runner in pool
        assert clean_runner_pool.runner_count == 1


class TestHeartbeat:
    """Tests for POST /api/runners/{id}/heartbeat endpoint."""

    async def test_heartbeat_success(self, client, clean_runner_pool):
        """Heartbeat returns OK for known runner."""
        runner = clean_runner_pool.register()

        response = await client.post(f"/api/runners/{runner.id}/heartbeat")
        assert_status_code(response, 200)
        assert response.json()["status"] == "ok"

    async def test_heartbeat_unknown_runner(self, client, clean_runner_pool):
        """Heartbeat returns 404 for unknown runner."""
        response = await client.post("/api/runners/unknown-id/heartbeat")
        assert_status_code(response, 404)


class TestGetJob:
    """Tests for GET /api/runners/{id}/job endpoint."""

    async def test_get_job_no_jobs(self, client, clean_runner_pool, clean_job_queue):
        """Returns null when no jobs available."""
        runner = clean_runner_pool.register()

        response = await client.get(f"/api/runners/{runner.id}/job")
        assert_status_code(response, 200)
        assert response.json()["job"] is None

    async def test_get_job_unknown_runner(self, client, clean_runner_pool):
        """Returns 404 for unknown runner."""
        response = await client.get("/api/runners/unknown-id/job")
        assert_status_code(response, 404)


class TestCompleteJob:
    """Tests for POST /api/runners/{id}/complete endpoint."""

    async def test_complete_job_success(self, client, clean_runner_pool):
        """Complete job returns OK."""
        from app.services.job_queue import QueuedJob

        runner = clean_runner_pool.register()
        runner.status = "busy"
        runner.current_job = QueuedJob(
            id="job-1",
            card_id="card-1",
            repo_id="repo-1",
            repo_url="",
            repo_path="",
            base_branch="main",
            card_title="Test",
            card_description="",
        )

        response = await client.post(
            f"/api/runners/{runner.id}/complete",
            json={"success": True},
        )
        assert_status_code(response, 200)

    async def test_complete_job_unknown_runner(self, client, clean_runner_pool):
        """Complete returns 404 for unknown runner."""
        response = await client.post(
            "/api/runners/unknown-id/complete",
            json={"success": True},
        )
        assert_status_code(response, 404)


class TestRunnerLogs:
    """Tests for runner log endpoints."""

    async def test_append_logs(self, client, clean_runner_pool):
        """Can append logs to runner."""
        runner = clean_runner_pool.register()

        response = await client.post(
            f"/api/runners/{runner.id}/logs",
            json={"lines": ["log line 1", "log line 2"]},
        )
        assert_status_code(response, 200)
        assert response.json()["total_lines"] == 2

    async def test_get_logs(self, client, clean_runner_pool):
        """Can get logs from runner."""
        runner = clean_runner_pool.register()
        clean_runner_pool.append_log(runner.id, "test log")

        response = await client.get(f"/api/runners/{runner.id}/logs")
        assert_status_code(response, 200)
        result = response.json()
        assert "logs" in result
        assert "test log" in result["logs"]

    async def test_get_logs_with_offset(self, client, clean_runner_pool):
        """Can get logs with offset."""
        runner = clean_runner_pool.register()
        clean_runner_pool.append_log(runner.id, "line 0")
        clean_runner_pool.append_log(runner.id, "line 1")
        clean_runner_pool.append_log(runner.id, "line 2")

        response = await client.get(f"/api/runners/{runner.id}/logs?offset=1")
        assert_status_code(response, 200)
        result = response.json()
        assert len(result["logs"]) == 2
        assert result["total"] == 3


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
        assert result["offline_runners"] == 0
        assert result["queued_jobs"] == 0
        assert result["pending_jobs"] == 0

    async def test_pool_status_with_runners(self, client, clean_runner_pool, clean_job_queue):
        """Returns status with runners."""
        clean_runner_pool.register()
        clean_runner_pool.register()
        clean_runner_pool.register()

        response = await client.get("/api/runners/status")
        result = response.json()

        assert result["total_runners"] == 3
        assert result["idle_runners"] == 3
        assert result["busy_runners"] == 0

    async def test_pool_status_with_mixed_states(self, client, clean_runner_pool, clean_job_queue):
        """Returns status with mixed runner states."""
        r1 = clean_runner_pool.register()
        r2 = clean_runner_pool.register()
        r3 = clean_runner_pool.register()
        r4 = clean_runner_pool.register()

        r1.status = "busy"
        r2.status = "busy"
        r3.status = "offline"

        response = await client.get("/api/runners/status")
        result = response.json()

        assert result["total_runners"] == 4
        assert result["idle_runners"] == 1
        assert result["busy_runners"] == 2
        assert result["offline_runners"] == 1


class TestDockerCommand:
    """Tests for GET /api/runners/docker-command endpoint."""

    async def test_get_docker_command(self, client):
        """Returns docker command."""
        response = await client.get("/api/runners/docker-command")
        assert_status_code(response, 200)

        result = response.json()
        assert "command" in result
        assert "image" in result
        assert "env_vars" in result
        assert "lazyaf-runner" in result["image"]

    async def test_get_docker_command_with_secrets(self, client):
        """Returns docker command with secrets flag."""
        response = await client.get("/api/runners/docker-command?with_secrets=true")
        assert_status_code(response, 200)

        result = response.json()
        assert "command_with_secrets" in result


class TestUnregisterRunner:
    """Tests for DELETE /api/runners/{id} endpoint."""

    async def test_unregister_runner(self, client, clean_runner_pool):
        """Can unregister a runner."""
        runner = clean_runner_pool.register()
        assert clean_runner_pool.runner_count == 1

        response = await client.delete(f"/api/runners/{runner.id}")
        assert_status_code(response, 200)
        assert clean_runner_pool.runner_count == 0

    async def test_unregister_unknown_runner(self, client, clean_runner_pool):
        """Returns 404 for unknown runner."""
        response = await client.delete("/api/runners/unknown-id")
        assert_status_code(response, 404)
