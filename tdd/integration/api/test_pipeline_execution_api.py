"""
Integration tests for Pipeline Execution API endpoints.

These tests verify the pipeline execution lifecycle including:
- Running pipelines
- Listing and viewing pipeline runs
- Cancelling pipeline runs
- Step logs retrieval
- WebSocket event broadcasting
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

from shared.factories import (
    repo_ingest_payload,
    pipeline_create_payload,
    pipeline_step_payload,
    pipeline_run_create_payload,
)
from shared.assertions import (
    assert_status_code,
    assert_json_list_length,
    assert_json_contains,
    assert_not_found,
)


@pytest_asyncio.fixture
async def ingested_repo(client, clean_git_repos):
    """Create an ingested repo for pipeline execution tests."""
    response = await client.post(
        "/api/repos/ingest",
        json=repo_ingest_payload(name="PipelineExecTestRepo"),
    )
    return response.json()


@pytest_asyncio.fixture
async def pipeline_with_steps(client, ingested_repo):
    """Create a pipeline with steps for execution tests."""
    steps = [
        pipeline_step_payload(
            name="Lint",
            step_type="script",
            config={"command": "echo linting..."},
        ),
        pipeline_step_payload(
            name="Test",
            step_type="script",
            config={"command": "echo testing..."},
        ),
    ]
    response = await client.post(
        f"/api/repos/{ingested_repo['id']}/pipelines",
        json=pipeline_create_payload(name="CI Pipeline", steps=steps),
    )
    return response.json()


@pytest_asyncio.fixture
async def empty_pipeline(client, ingested_repo):
    """Create a pipeline with no steps."""
    response = await client.post(
        f"/api/repos/{ingested_repo['id']}/pipelines",
        json=pipeline_create_payload(name="Empty Pipeline", steps=[]),
    )
    return response.json()


class TestRunPipeline:
    """Tests for POST /api/pipelines/{pipeline_id}/run endpoint."""

    async def test_run_pipeline_creates_run(self, client, pipeline_with_steps, clean_job_queue):
        """Running a pipeline creates a pipeline run."""
        response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        assert_status_code(response, 200)
        result = response.json()

        assert "id" in result
        assert result["pipeline_id"] == pipeline_with_steps["id"]
        assert result["status"] == "running"
        assert result["trigger_type"] == "manual"
        assert result["steps_total"] == 2

    async def test_run_pipeline_with_trigger_params(self, client, pipeline_with_steps, clean_job_queue):
        """Running a pipeline with custom trigger parameters."""
        payload = pipeline_run_create_payload(
            trigger_type="webhook",
            trigger_ref="abc123",
        )
        response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json=payload,
        )
        result = response.json()

        assert result["trigger_type"] == "webhook"
        assert result["trigger_ref"] == "abc123"

    async def test_run_pipeline_not_found(self, client):
        """Returns 404 for non-existent pipeline."""
        response = await client.post(
            "/api/pipelines/nonexistent-pipeline/run",
            json={},
        )
        assert_not_found(response, "Pipeline")

    async def test_run_empty_pipeline_fails(self, client, empty_pipeline):
        """Running pipeline with no steps returns error."""
        response = await client.post(
            f"/api/pipelines/{empty_pipeline['id']}/run",
            json={},
        )
        assert_status_code(response, 400)
        assert "no steps" in response.json()["detail"].lower()

    async def test_run_pipeline_repo_not_ingested(self, client):
        """Running pipeline on non-ingested repo returns error."""
        # Create a repo without ingesting
        repo_response = await client.post(
            "/api/repos",
            json={"name": "NotIngestedRepo"},
        )
        repo = repo_response.json()

        # Create a pipeline
        steps = [pipeline_step_payload(name="Test", step_type="script")]
        pipeline_response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Test", steps=steps),
        )
        pipeline = pipeline_response.json()

        # Try to run it
        response = await client.post(
            f"/api/pipelines/{pipeline['id']}/run",
            json={},
        )
        assert_status_code(response, 400)
        assert "ingested" in response.json()["detail"].lower()


class TestListPipelineRuns:
    """Tests for GET /api/pipelines/{id}/runs and /api/pipeline-runs endpoints."""

    async def test_list_pipeline_runs_empty(self, client, pipeline_with_steps):
        """Returns empty list when pipeline has no runs."""
        response = await client.get(f"/api/pipelines/{pipeline_with_steps['id']}/runs")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_pipeline_runs_with_data(self, client, pipeline_with_steps, clean_job_queue):
        """Returns all runs for a pipeline."""
        # Create runs
        await client.post(f"/api/pipelines/{pipeline_with_steps['id']}/run", json={})
        await client.post(f"/api/pipelines/{pipeline_with_steps['id']}/run", json={})

        response = await client.get(f"/api/pipelines/{pipeline_with_steps['id']}/runs")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_pipeline_runs_pipeline_not_found(self, client):
        """Returns 404 for non-existent pipeline."""
        response = await client.get("/api/pipelines/nonexistent/runs")
        assert_not_found(response, "Pipeline")

    async def test_list_all_pipeline_runs(self, client, pipeline_with_steps, clean_job_queue):
        """GET /api/pipeline-runs returns all runs."""
        await client.post(f"/api/pipelines/{pipeline_with_steps['id']}/run", json={})

        response = await client.get("/api/pipeline-runs")
        assert_status_code(response, 200)
        runs = response.json()
        assert len(runs) >= 1

    async def test_list_pipeline_runs_filter_by_pipeline(self, client, pipeline_with_steps, clean_job_queue):
        """GET /api/pipeline-runs with pipeline_id filter."""
        await client.post(f"/api/pipelines/{pipeline_with_steps['id']}/run", json={})

        response = await client.get(f"/api/pipeline-runs?pipeline_id={pipeline_with_steps['id']}")
        assert_status_code(response, 200)
        runs = response.json()
        assert all(r["pipeline_id"] == pipeline_with_steps["id"] for r in runs)

    async def test_list_pipeline_runs_filter_by_status(self, client, pipeline_with_steps, clean_job_queue):
        """GET /api/pipeline-runs with status filter."""
        await client.post(f"/api/pipelines/{pipeline_with_steps['id']}/run", json={})

        response = await client.get("/api/pipeline-runs?status=running")
        assert_status_code(response, 200)
        runs = response.json()
        # All returned runs should have the requested status
        assert all(r["status"] == "running" for r in runs)

    async def test_list_pipeline_runs_with_limit(self, client, pipeline_with_steps, clean_job_queue):
        """GET /api/pipeline-runs respects limit parameter."""
        # Create multiple runs
        for _ in range(3):
            await client.post(f"/api/pipelines/{pipeline_with_steps['id']}/run", json={})

        response = await client.get("/api/pipeline-runs?limit=2")
        assert_status_code(response, 200)
        runs = response.json()
        assert len(runs) <= 2


class TestGetPipelineRun:
    """Tests for GET /api/pipeline-runs/{run_id} endpoint."""

    async def test_get_pipeline_run_exists(self, client, pipeline_with_steps, clean_job_queue):
        """Returns pipeline run when it exists."""
        # Create a run
        run_response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        run_id = run_response.json()["id"]

        response = await client.get(f"/api/pipeline-runs/{run_id}")
        assert_status_code(response, 200)
        assert_json_contains(response, {
            "id": run_id,
            "pipeline_id": pipeline_with_steps["id"],
        })

    async def test_get_pipeline_run_not_found(self, client):
        """Returns 404 for non-existent pipeline run."""
        response = await client.get("/api/pipeline-runs/nonexistent-run-id")
        assert_not_found(response, "Pipeline run")

    async def test_get_pipeline_run_includes_step_runs(self, client, pipeline_with_steps, clean_job_queue):
        """Pipeline run response includes step_runs."""
        run_response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        run_id = run_response.json()["id"]

        response = await client.get(f"/api/pipeline-runs/{run_id}")
        result = response.json()

        assert "step_runs" in result
        assert isinstance(result["step_runs"], list)

    async def test_get_pipeline_run_has_all_fields(self, client, pipeline_with_steps, clean_job_queue):
        """Pipeline run response includes all expected fields."""
        run_response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        run_id = run_response.json()["id"]

        response = await client.get(f"/api/pipeline-runs/{run_id}")
        result = response.json()

        assert "id" in result
        assert "pipeline_id" in result
        assert "status" in result
        assert "trigger_type" in result
        assert "trigger_ref" in result
        assert "current_step" in result
        assert "steps_completed" in result
        assert "steps_total" in result
        assert "started_at" in result
        assert "completed_at" in result
        assert "created_at" in result
        assert "step_runs" in result


class TestCancelPipelineRun:
    """Tests for POST /api/pipeline-runs/{run_id}/cancel endpoint."""

    async def test_cancel_running_pipeline(self, client, pipeline_with_steps, clean_job_queue):
        """Cancelling a running pipeline marks it as cancelled."""
        # Create a run
        run_response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        run_id = run_response.json()["id"]

        # Cancel it
        response = await client.post(f"/api/pipeline-runs/{run_id}/cancel")
        assert_status_code(response, 200)
        result = response.json()

        assert result["status"] == "cancelled"
        assert result["completed_at"] is not None

    async def test_cancel_pipeline_run_not_found(self, client):
        """Returns 404 for non-existent pipeline run."""
        response = await client.post("/api/pipeline-runs/nonexistent/cancel")
        assert_not_found(response, "Pipeline run")

    async def test_cancel_completed_pipeline_fails(self, client, pipeline_with_steps, clean_job_queue):
        """Cancelling a completed pipeline returns error."""
        # Create and cancel a run
        run_response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        run_id = run_response.json()["id"]

        # Cancel once
        await client.post(f"/api/pipeline-runs/{run_id}/cancel")

        # Try to cancel again
        response = await client.post(f"/api/pipeline-runs/{run_id}/cancel")
        assert_status_code(response, 400)
        assert "cannot be cancelled" in response.json()["detail"].lower()


class TestGetStepLogs:
    """Tests for GET /api/pipeline-runs/{run_id}/steps/{step_index}/logs endpoint."""

    async def test_get_step_logs_exists(self, client, pipeline_with_steps, clean_job_queue):
        """Returns logs for an existing step run."""
        # Create a run (this will create step runs)
        run_response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        run_id = run_response.json()["id"]

        response = await client.get(f"/api/pipeline-runs/{run_id}/steps/0/logs")
        assert_status_code(response, 200)
        result = response.json()

        assert "step_index" in result
        assert "step_name" in result
        assert "logs" in result
        assert "status" in result
        assert result["step_index"] == 0

    async def test_get_step_logs_not_found(self, client, pipeline_with_steps, clean_job_queue):
        """Returns 404 for non-existent step run."""
        # Create a run
        run_response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        run_id = run_response.json()["id"]

        # Try to get logs for non-existent step
        response = await client.get(f"/api/pipeline-runs/{run_id}/steps/999/logs")
        assert_not_found(response, "Step run")

    async def test_get_step_logs_run_not_found(self, client):
        """Returns 404 for non-existent pipeline run."""
        response = await client.get("/api/pipeline-runs/nonexistent/steps/0/logs")
        assert_not_found(response, "Step run")


class TestPipelineRunStateTransitions:
    """Tests for pipeline run state transitions."""

    async def test_new_run_starts_in_running_state(self, client, pipeline_with_steps, clean_job_queue):
        """New pipeline run starts in 'running' state."""
        response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        result = response.json()
        assert result["status"] == "running"
        assert result["started_at"] is not None

    async def test_cancelled_run_has_completed_at(self, client, pipeline_with_steps, clean_job_queue):
        """Cancelled pipeline run has completed_at timestamp."""
        run_response = await client.post(
            f"/api/pipelines/{pipeline_with_steps['id']}/run",
            json={},
        )
        run_id = run_response.json()["id"]

        cancel_response = await client.post(f"/api/pipeline-runs/{run_id}/cancel")
        result = cancel_response.json()

        assert result["status"] == "cancelled"
        assert result["completed_at"] is not None


class TestPipelineRunOrdering:
    """Tests for pipeline run ordering."""

    async def test_runs_ordered_by_created_at_desc(self, client, pipeline_with_steps, clean_job_queue):
        """Pipeline runs are returned in descending order by created_at."""
        # Create multiple runs
        run_ids = []
        for _ in range(3):
            response = await client.post(
                f"/api/pipelines/{pipeline_with_steps['id']}/run",
                json={},
            )
            run_ids.append(response.json()["id"])

        # Fetch runs
        response = await client.get(f"/api/pipelines/{pipeline_with_steps['id']}/runs")
        runs = response.json()

        # Most recent run should be first
        assert runs[0]["id"] == run_ids[-1]
