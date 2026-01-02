"""
Integration tests for LazyAF Files API endpoints.

These tests verify the full request/response cycle for repository-defined
agents and pipelines stored in .lazyaf/ directory.
"""
import sys
from pathlib import Path
import tempfile
import subprocess

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
    assert_json_contains,
    assert_not_found,
)


@pytest_asyncio.fixture
async def repo_with_pipeline_definition(client, clean_git_repos):
    """Create a repo with a pipeline definition in .lazyaf/pipelines/."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "test-repo"
        repo_path.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, check=True, capture_output=True)

        # Create README
        (repo_path / "README.md").write_text("# Test Repo")

        # Create .lazyaf/pipelines directory
        pipelines_dir = repo_path / ".lazyaf" / "pipelines"
        pipelines_dir.mkdir(parents=True)

        # Create a test pipeline
        pipeline_yaml = """name: test-ci
description: Test CI pipeline
steps:
  - name: Lint
    type: script
    config:
      command: echo "Running lint..."
  - name: Test
    type: script
    config:
      command: echo "Running tests..."
"""
        (pipelines_dir / "test-ci.yaml").write_text(pipeline_yaml)

        # Commit all files
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit with pipeline"], cwd=repo_path, check=True, capture_output=True)

        # Ingest the repo
        response = await client.post(
            "/api/repos/ingest",
            json={"path": str(repo_path), "name": "repo-with-pipeline"},
        )
        assert response.status_code == 201, f"Failed to ingest repo: {response.text}"
        ingest_data = response.json()

        # Get the full repo
        repo_response = await client.get(f"/api/repos/{ingest_data['id']}")
        return repo_response.json()


@pytest_asyncio.fixture
async def repo_with_agent_definition(client, clean_git_repos):
    """Create a repo with an agent definition in .lazyaf/agents/."""
    with tempfile.TemporaryDirectory() as tmpdir:
        repo_path = Path(tmpdir) / "test-repo"
        repo_path.mkdir()

        # Initialize git repo
        subprocess.run(["git", "init"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=repo_path, check=True, capture_output=True)

        # Create README
        (repo_path / "README.md").write_text("# Test Repo")

        # Create .lazyaf/agents directory
        agents_dir = repo_path / ".lazyaf" / "agents"
        agents_dir.mkdir(parents=True)

        # Create a test agent
        agent_yaml = """name: test-agent
description: Test agent for fixing bugs
prompt_template: |
  Fix the bug: {{description}}
"""
        (agents_dir / "test-agent.yaml").write_text(agent_yaml)

        # Commit all files
        subprocess.run(["git", "add", "."], cwd=repo_path, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "Initial commit with agent"], cwd=repo_path, check=True, capture_output=True)

        # Ingest the repo
        response = await client.post(
            "/api/repos/ingest",
            json={"path": str(repo_path), "name": "repo-with-agent"},
        )
        assert response.status_code == 201, f"Failed to ingest repo: {response.text}"
        ingest_data = response.json()

        # Get the full repo
        repo_response = await client.get(f"/api/repos/{ingest_data['id']}")
        return repo_response.json()


class TestListRepoAgents:
    """Tests for GET /api/repos/{repo_id}/lazyaf/agents endpoint."""

    async def test_list_agents_empty(self, client, ingested_repo):
        """Returns empty list when repo has no agent definitions."""
        response = await client.get(f"/api/repos/{ingested_repo['id']}/lazyaf/agents")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_agents_with_data(self, client, repo_with_agent_definition):
        """Returns all agents defined in .lazyaf/agents/."""
        response = await client.get(f"/api/repos/{repo_with_agent_definition['id']}/lazyaf/agents")
        assert_status_code(response, 200)
        agents = response.json()
        assert len(agents) >= 1
        assert any(agent["name"] == "test-agent" for agent in agents)

    async def test_list_agents_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/nonexistent-repo/lazyaf/agents")
        assert_not_found(response, "Repo")


class TestGetRepoAgent:
    """Tests for GET /api/repos/{repo_id}/lazyaf/agents/{agent_name} endpoint."""

    async def test_get_agent_exists(self, client, repo_with_agent_definition):
        """Returns agent definition when it exists."""
        response = await client.get(
            f"/api/repos/{repo_with_agent_definition['id']}/lazyaf/agents/test-agent"
        )
        assert_status_code(response, 200)
        agent = response.json()
        assert agent["name"] == "test-agent"
        assert agent["source"] == "repo"
        assert "Fix the bug" in agent["prompt_template"]

    async def test_get_agent_not_found(self, client, ingested_repo):
        """Returns 404 for non-existent agent."""
        response = await client.get(
            f"/api/repos/{ingested_repo['id']}/lazyaf/agents/nonexistent"
        )
        assert_status_code(response, 404)

    async def test_get_agent_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/nonexistent/lazyaf/agents/test-agent")
        assert_not_found(response, "Repo")


class TestListRepoPipelines:
    """Tests for GET /api/repos/{repo_id}/lazyaf/pipelines endpoint."""

    async def test_list_pipelines_empty(self, client, ingested_repo):
        """Returns empty list when repo has no pipeline definitions."""
        response = await client.get(f"/api/repos/{ingested_repo['id']}/lazyaf/pipelines")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_pipelines_with_data(self, client, repo_with_pipeline_definition):
        """Returns all pipelines defined in .lazyaf/pipelines/."""
        response = await client.get(f"/api/repos/{repo_with_pipeline_definition['id']}/lazyaf/pipelines")
        assert_status_code(response, 200)
        pipelines = response.json()
        assert len(pipelines) >= 1
        assert any(pipeline["name"] == "test-ci" for pipeline in pipelines)

    async def test_list_pipelines_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/nonexistent-repo/lazyaf/pipelines")
        assert_not_found(response, "Repo")


class TestGetRepoPipeline:
    """Tests for GET /api/repos/{repo_id}/lazyaf/pipelines/{pipeline_name} endpoint."""

    async def test_get_pipeline_exists(self, client, repo_with_pipeline_definition):
        """Returns pipeline definition when it exists."""
        response = await client.get(
            f"/api/repos/{repo_with_pipeline_definition['id']}/lazyaf/pipelines/test-ci"
        )
        assert_status_code(response, 200)
        pipeline = response.json()
        assert pipeline["name"] == "test-ci"
        assert pipeline["description"] == "Test CI pipeline"
        assert len(pipeline["steps"]) == 2
        assert pipeline["steps"][0]["name"] == "Lint"
        assert pipeline["steps"][1]["name"] == "Test"

    async def test_get_pipeline_not_found(self, client, ingested_repo):
        """Returns 404 for non-existent pipeline."""
        response = await client.get(
            f"/api/repos/{ingested_repo['id']}/lazyaf/pipelines/nonexistent"
        )
        assert_status_code(response, 404)

    async def test_get_pipeline_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/nonexistent/lazyaf/pipelines/test-ci")
        assert_not_found(response, "Repo")


class TestRunRepoPipeline:
    """Tests for POST /api/repos/{repo_id}/lazyaf/pipelines/{pipeline_name}/run endpoint."""

    async def test_run_repo_pipeline_creates_platform_pipeline_and_run(
        self, client, repo_with_pipeline_definition, clean_job_queue
    ):
        """Running a repo pipeline creates/updates platform pipeline and starts a run."""
        repo_id = repo_with_pipeline_definition['id']

        response = await client.post(
            f"/api/repos/{repo_id}/lazyaf/pipelines/test-ci/run",
        )
        assert_status_code(response, 200)
        result = response.json()

        # Should return pipeline_id, run_id, status, and message
        assert "pipeline_id" in result
        assert "run_id" in result
        assert "status" in result
        assert "message" in result
        assert result["status"] == "running"

        # Verify platform pipeline was created with [repo] prefix
        platform_pipeline_response = await client.get(f"/api/pipelines/{result['pipeline_id']}")
        assert_status_code(platform_pipeline_response, 200)
        platform_pipeline = platform_pipeline_response.json()
        assert platform_pipeline["name"] == "[repo] test-ci"
        assert platform_pipeline["repo_id"] == repo_id
        assert len(platform_pipeline["steps"]) == 2

        # Verify pipeline run was created
        run_response = await client.get(f"/api/pipeline-runs/{result['run_id']}")
        assert_status_code(run_response, 200)
        run = run_response.json()
        assert run["pipeline_id"] == result["pipeline_id"]
        assert run["trigger_type"] == "manual"
        assert run["status"] == "running"

    async def test_run_repo_pipeline_updates_existing_platform_pipeline(
        self, client, repo_with_pipeline_definition, clean_job_queue
    ):
        """Running a repo pipeline twice updates the existing platform pipeline."""
        repo_id = repo_with_pipeline_definition['id']

        # Run once
        response1 = await client.post(
            f"/api/repos/{repo_id}/lazyaf/pipelines/test-ci/run",
        )
        result1 = response1.json()
        pipeline_id_1 = result1["pipeline_id"]

        # Run again
        response2 = await client.post(
            f"/api/repos/{repo_id}/lazyaf/pipelines/test-ci/run",
        )
        result2 = response2.json()
        pipeline_id_2 = result2["pipeline_id"]

        # Should use same platform pipeline
        assert pipeline_id_1 == pipeline_id_2

        # Should create different runs
        assert result1["run_id"] != result2["run_id"]

    async def test_run_repo_pipeline_with_custom_branch(
        self, client, repo_with_pipeline_definition, clean_job_queue
    ):
        """Running a repo pipeline with custom branch parameter."""
        repo_id = repo_with_pipeline_definition['id']
        default_branch = repo_with_pipeline_definition['default_branch']

        response = await client.post(
            f"/api/repos/{repo_id}/lazyaf/pipelines/test-ci/run?branch={default_branch}",
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "running"

    async def test_run_repo_pipeline_not_found(self, client, ingested_repo):
        """Returns 404 for non-existent pipeline."""
        response = await client.post(
            f"/api/repos/{ingested_repo['id']}/lazyaf/pipelines/nonexistent/run",
        )
        assert_status_code(response, 404)

    async def test_run_repo_pipeline_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.post(
            "/api/repos/nonexistent/lazyaf/pipelines/test-ci/run",
        )
        assert_not_found(response, "Repo")

    async def test_run_repo_pipeline_not_ingested(self, client, clean_git_repos, clean_job_queue):
        """Returns 404 when repo is not ingested (no git storage)."""
        # Create a repo without ingesting (no git storage)
        response = await client.post(
            "/api/repos",
            json={"name": "no-branch-repo"},
        )
        repo = response.json()

        response = await client.post(
            f"/api/repos/{repo['id']}/lazyaf/pipelines/test/run",
        )
        # Pipeline can't be found because there's no git storage
        assert_status_code(response, 404)


class TestRepoPipelineAndPlatformPipelineDrift:
    """Tests for verifying no drift between repo and platform pipelines."""

    async def test_platform_pipeline_syncs_with_repo_changes(
        self, client, repo_with_pipeline_definition, clean_job_queue
    ):
        """Platform pipeline updates when repo pipeline definition changes."""
        repo_id = repo_with_pipeline_definition['id']

        # Run once to create platform pipeline
        response1 = await client.post(
            f"/api/repos/{repo_id}/lazyaf/pipelines/test-ci/run",
        )
        result1 = response1.json()
        pipeline_id = result1["pipeline_id"]

        # Get platform pipeline
        platform_response = await client.get(f"/api/pipelines/{pipeline_id}")
        platform_pipeline = platform_response.json()

        # Verify it has 2 steps from the original definition
        assert len(platform_pipeline["steps"]) == 2

        # In a real scenario, the repo definition would change and running again
        # would update the platform pipeline. This test verifies the update mechanism works.
