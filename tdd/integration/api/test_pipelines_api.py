"""
Integration tests for Pipelines API endpoints.

These tests verify the full request/response cycle for pipeline management,
including CRUD operations, validation, and error handling.
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
    repo_create_payload,
    pipeline_create_payload,
    pipeline_update_payload,
    pipeline_step_payload,
)
from shared.assertions import (
    assert_status_code,
    assert_created_response,
    assert_updated_response,
    assert_deleted_response,
    assert_not_found,
    assert_json_list_length,
    assert_json_contains,
)


@pytest_asyncio.fixture
async def repo(client):
    """Create a repo for pipeline tests."""
    response = await client.post(
        "/api/repos",
        json=repo_create_payload(name="PipelineTestRepo"),
    )
    return response.json()


@pytest_asyncio.fixture
async def pipeline(client, repo):
    """Create a pipeline for tests that need one."""
    steps = [
        pipeline_step_payload(name="Test", step_type="script", config={"command": "npm test"}),
    ]
    response = await client.post(
        f"/api/repos/{repo['id']}/pipelines",
        json=pipeline_create_payload(name="Test Pipeline", steps=steps),
    )
    return response.json()


class TestListPipelines:
    """Tests for GET /api/pipelines and /api/repos/{repo_id}/pipelines endpoints."""

    async def test_list_pipelines_empty(self, client, repo):
        """Returns empty list when repo has no pipelines."""
        response = await client.get(f"/api/repos/{repo['id']}/pipelines")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_pipelines_with_data(self, client, repo):
        """Returns all pipelines for a repo."""
        # Create pipelines
        await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Pipeline 1"),
        )
        await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Pipeline 2"),
        )

        response = await client.get(f"/api/repos/{repo['id']}/pipelines")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_pipelines_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/nonexistent-repo/pipelines")
        assert_not_found(response, "Repo")

    async def test_list_all_pipelines(self, client, repo):
        """GET /api/pipelines returns all pipelines across repos."""
        # Create a pipeline
        await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Test Pipeline"),
        )

        response = await client.get("/api/pipelines")
        assert_status_code(response, 200)
        pipelines = response.json()
        assert len(pipelines) >= 1

    async def test_list_all_pipelines_filter_by_repo(self, client, repo):
        """GET /api/pipelines with repo_id filter."""
        # Create a pipeline
        await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=pipeline_create_payload(name="Filtered Pipeline"),
        )

        response = await client.get(f"/api/pipelines?repo_id={repo['id']}")
        assert_status_code(response, 200)
        pipelines = response.json()
        assert all(p["repo_id"] == repo["id"] for p in pipelines)


class TestCreatePipeline:
    """Tests for POST /api/repos/{repo_id}/pipelines endpoint."""

    async def test_create_pipeline_minimal(self, client, repo):
        """Creates pipeline with minimal required fields."""
        payload = pipeline_create_payload(name="Minimal Pipeline")

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = assert_created_response(response, {"name": "Minimal Pipeline"})
        assert result["repo_id"] == repo["id"]
        assert result["steps"] == []
        assert result["is_template"] is False

    async def test_create_pipeline_with_steps(self, client, repo):
        """Creates pipeline with step definitions."""
        steps = [
            pipeline_step_payload(
                name="Lint",
                step_type="script",
                config={"command": "npm run lint"},
            ),
            pipeline_step_payload(
                name="Test",
                step_type="script",
                config={"command": "npm test"},
            ),
        ]
        payload = pipeline_create_payload(name="CI Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = response.json()
        assert result["name"] == "CI Pipeline"
        assert len(result["steps"]) == 2
        assert result["steps"][0]["name"] == "Lint"
        assert result["steps"][1]["name"] == "Test"

    async def test_create_pipeline_with_docker_step(self, client, repo):
        """Creates pipeline with docker step type."""
        steps = [
            pipeline_step_payload(
                name="Build",
                step_type="docker",
                config={"image": "node:20", "command": "npm run build"},
            ),
        ]
        payload = pipeline_create_payload(name="Docker Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = response.json()
        assert result["steps"][0]["type"] == "docker"
        assert result["steps"][0]["config"]["image"] == "node:20"

    async def test_create_pipeline_with_agent_step(self, client, repo):
        """Creates pipeline with agent step type."""
        steps = [
            pipeline_step_payload(
                name="Implement Feature",
                step_type="agent",
                config={
                    "runner_type": "claude-code",
                    "title": "Add login",
                    "description": "Implement OAuth login",
                },
            ),
        ]
        payload = pipeline_create_payload(name="Agent Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = response.json()
        assert result["steps"][0]["type"] == "agent"
        assert result["steps"][0]["config"]["runner_type"] == "claude-code"

    async def test_create_pipeline_with_branching(self, client, repo):
        """Creates pipeline with custom on_success/on_failure actions."""
        steps = [
            pipeline_step_payload(
                name="Test",
                step_type="script",
                config={"command": "npm test"},
                on_success="merge:main",
                on_failure="trigger:fix-card-123",
            ),
        ]
        payload = pipeline_create_payload(name="Branching Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = response.json()
        assert result["steps"][0]["on_success"] == "merge:main"
        assert result["steps"][0]["on_failure"] == "trigger:fix-card-123"

    async def test_create_pipeline_as_template(self, client, repo):
        """Creates pipeline as a template."""
        payload = pipeline_create_payload(name="Template Pipeline", is_template=True)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = response.json()
        assert result["is_template"] is True

    async def test_create_pipeline_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.post(
            "/api/repos/nonexistent-repo/pipelines",
            json=pipeline_create_payload(),
        )
        assert_not_found(response, "Repo")

    async def test_create_pipeline_missing_name_fails(self, client, repo):
        """Fails without required name field."""
        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json={"description": "No name"},
        )
        assert_status_code(response, 422)


class TestGetPipeline:
    """Tests for GET /api/pipelines/{pipeline_id} endpoint."""

    async def test_get_pipeline_exists(self, client, pipeline):
        """Returns pipeline when it exists."""
        response = await client.get(f"/api/pipelines/{pipeline['id']}")
        assert_status_code(response, 200)
        assert_json_contains(response, {"id": pipeline["id"], "name": pipeline["name"]})

    async def test_get_pipeline_not_found(self, client):
        """Returns 404 for non-existent pipeline."""
        response = await client.get("/api/pipelines/nonexistent-pipeline-id")
        assert_not_found(response, "Pipeline")

    async def test_get_pipeline_returns_all_fields(self, client, pipeline):
        """Returns pipeline with complete field set."""
        response = await client.get(f"/api/pipelines/{pipeline['id']}")
        result = response.json()

        assert "id" in result
        assert "repo_id" in result
        assert "name" in result
        assert "description" in result
        assert "steps" in result
        assert "is_template" in result
        assert "created_at" in result
        assert "updated_at" in result


class TestUpdatePipeline:
    """Tests for PATCH /api/pipelines/{pipeline_id} endpoint."""

    async def test_update_pipeline_name(self, client, pipeline):
        """Updates pipeline name only."""
        response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={"name": "Updated Name"},
        )
        result = assert_updated_response(response, {"name": "Updated Name"})
        assert result["id"] == pipeline["id"]

    async def test_update_pipeline_description(self, client, pipeline):
        """Updates pipeline description."""
        response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={"description": "Updated description"},
        )
        assert response.json()["description"] == "Updated description"

    async def test_update_pipeline_steps(self, client, pipeline):
        """Updates pipeline steps."""
        new_steps = [
            pipeline_step_payload(name="New Step", step_type="script", config={"command": "echo hello"}),
        ]
        response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={"steps": new_steps},
        )
        result = response.json()
        assert len(result["steps"]) == 1
        assert result["steps"][0]["name"] == "New Step"

    async def test_update_pipeline_is_template(self, client, pipeline):
        """Updates pipeline is_template flag."""
        response = await client.patch(
            f"/api/pipelines/{pipeline['id']}",
            json={"is_template": True},
        )
        assert response.json()["is_template"] is True

    async def test_update_pipeline_not_found(self, client):
        """Returns 404 for non-existent pipeline."""
        response = await client.patch(
            "/api/pipelines/nonexistent-id",
            json={"name": "New Name"},
        )
        assert_not_found(response, "Pipeline")


class TestDeletePipeline:
    """Tests for DELETE /api/pipelines/{pipeline_id} endpoint."""

    async def test_delete_pipeline_exists(self, client, pipeline):
        """Deletes pipeline when it exists."""
        response = await client.delete(f"/api/pipelines/{pipeline['id']}")
        assert_deleted_response(response)

        # Verify pipeline is gone
        get_response = await client.get(f"/api/pipelines/{pipeline['id']}")
        assert_not_found(get_response, "Pipeline")

    async def test_delete_pipeline_not_found(self, client):
        """Returns 404 for non-existent pipeline."""
        response = await client.delete("/api/pipelines/nonexistent-id")
        assert_not_found(response, "Pipeline")


class TestPipelineStepsValidation:
    """Tests for pipeline step validation."""

    async def test_step_with_custom_timeout(self, client, repo):
        """Steps can have custom timeout values."""
        steps = [
            pipeline_step_payload(name="Long Step", step_type="script", timeout=600),
        ]
        payload = pipeline_create_payload(name="Timeout Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = response.json()
        assert result["steps"][0]["timeout"] == 600

    async def test_step_defaults_are_applied(self, client, repo):
        """Steps without explicit values get defaults."""
        steps = [
            {"name": "Basic Step", "type": "script", "config": {"command": "echo test"}},
        ]
        payload = pipeline_create_payload(name="Defaults Pipeline", steps=steps)

        response = await client.post(
            f"/api/repos/{repo['id']}/pipelines",
            json=payload,
        )
        result = response.json()
        step = result["steps"][0]
        assert step["on_success"] == "next"
        assert step["on_failure"] == "stop"
        assert step["timeout"] == 300


class TestPipelineIsolation:
    """Tests verifying pipelines are properly isolated between repos."""

    async def test_pipelines_isolated_by_repo(self, client):
        """Pipelines are only returned for their specific repo."""
        # Create two repos
        resp1 = await client.post("/api/repos", json=repo_create_payload(name="Repo1"))
        resp2 = await client.post("/api/repos", json=repo_create_payload(name="Repo2"))
        repo1_id = resp1.json()["id"]
        repo2_id = resp2.json()["id"]

        # Create pipelines in each repo
        await client.post(
            f"/api/repos/{repo1_id}/pipelines",
            json=pipeline_create_payload(name="Repo1 Pipeline"),
        )
        await client.post(
            f"/api/repos/{repo2_id}/pipelines",
            json=pipeline_create_payload(name="Repo2 Pipeline"),
        )

        # Verify isolation
        response = await client.get(f"/api/repos/{repo1_id}/pipelines")
        pipelines = response.json()
        assert len(pipelines) == 1
        assert pipelines[0]["name"] == "Repo1 Pipeline"
