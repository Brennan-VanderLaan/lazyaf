"""
Integration tests for Repos Ingest API endpoint.

These tests verify the full request/response cycle for the new
/api/repos/ingest endpoint which creates a repo and initializes
internal git storage.

Endpoints tested:
- POST /api/repos/ingest
- GET /api/repos/{id}/clone-url
- DELETE /api/repos/{id} (git cleanup)
"""
import sys
import shutil
import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories import repo_create_payload
from shared.assertions import (
    assert_status_code,
    assert_created_response,
    assert_deleted_response,
    assert_not_found,
    assert_json_contains,
)


# -----------------------------------------------------------------------------
# Fixtures
# -----------------------------------------------------------------------------

@pytest.fixture
def temp_git_repos_dir():
    """Create a temporary directory for git repos during tests.

    Uses resolve() to get the full path and avoid Windows 8.3 short name issues.
    """
    temp_dir = Path(tempfile.mkdtemp()).resolve()
    yield temp_dir
    shutil.rmtree(temp_dir, ignore_errors=True)


@pytest_asyncio.fixture
async def git_repo_manager(temp_git_repos_dir):
    """Override git_repo_manager to use temp directory.

    This fixture must swap the singleton in all places it's referenced:
    1. app.services.git_server module
    2. git_backend.repo_manager
    3. app.routers.git module (captured at import time)
    4. app.routers.repos module (captured at import time)
    """
    from app.services.git_server import GitRepoManager, git_backend
    import app.services.git_server as git_module
    import app.routers.git as git_router
    import app.routers.repos as repos_router

    # Create a new manager with temp dir
    temp_manager = GitRepoManager(repos_dir=temp_git_repos_dir)

    # Save originals
    original_service_manager = git_module.git_repo_manager
    original_backend_manager = git_backend.repo_manager
    original_router_manager = git_router.git_repo_manager
    original_repos_router_manager = repos_router.git_repo_manager

    # Replace all references
    git_module.git_repo_manager = temp_manager
    git_backend.repo_manager = temp_manager
    git_router.git_repo_manager = temp_manager
    repos_router.git_repo_manager = temp_manager

    yield temp_manager

    # Restore all originals
    git_module.git_repo_manager = original_service_manager
    git_backend.repo_manager = original_backend_manager
    git_router.git_repo_manager = original_router_manager
    repos_router.git_repo_manager = original_repos_router_manager


def ingest_payload(
    name: str | None = None,
    remote_url: str | None = None,
    default_branch: str = "main",
) -> dict:
    """Create a payload for POST /api/repos/ingest."""
    from faker import Faker
    fake = Faker()
    return {
        "name": name or fake.word().capitalize() + "Repo",
        "remote_url": remote_url,
        "default_branch": default_branch,
    }


# -----------------------------------------------------------------------------
# Ingest Endpoint Tests
# -----------------------------------------------------------------------------

class TestIngestEndpoint:
    """Tests for POST /api/repos/ingest endpoint."""

    async def test_ingest_returns_201(self, client, git_repo_manager):
        """Returns 201 Created on success."""
        payload = ingest_payload(name="TestRepo")
        response = await client.post("/api/repos/ingest", json=payload)
        assert_status_code(response, 201)

    async def test_ingest_returns_repo_id(self, client, git_repo_manager):
        """Response includes the repo ID."""
        payload = ingest_payload(name="TestRepo")
        response = await client.post("/api/repos/ingest", json=payload)
        result = response.json()

        assert "id" in result
        assert len(result["id"]) == 36  # UUID format

    async def test_ingest_returns_name(self, client, git_repo_manager):
        """Response includes the repo name."""
        payload = ingest_payload(name="MyProject")
        response = await client.post("/api/repos/ingest", json=payload)
        result = response.json()

        assert result["name"] == "MyProject"

    async def test_ingest_returns_internal_git_url(self, client, git_repo_manager):
        """Response includes internal_git_url."""
        payload = ingest_payload(name="TestRepo")
        response = await client.post("/api/repos/ingest", json=payload)
        result = response.json()

        assert "internal_git_url" in result
        assert result["internal_git_url"].startswith("/git/")
        assert result["internal_git_url"].endswith(".git")

    async def test_ingest_returns_clone_url(self, client, git_repo_manager):
        """Response includes full clone URL."""
        payload = ingest_payload(name="TestRepo")
        response = await client.post("/api/repos/ingest", json=payload)
        result = response.json()

        assert "clone_url" in result
        # Full URL should include base URL
        assert result["clone_url"].startswith("http")
        assert "/git/" in result["clone_url"]
        assert result["clone_url"].endswith(".git")

    async def test_ingest_creates_bare_repo(self, client, git_repo_manager):
        """Ingest creates a bare git repo in storage."""
        payload = ingest_payload(name="TestRepo")
        response = await client.post("/api/repos/ingest", json=payload)
        result = response.json()
        repo_id = result["id"]

        # Verify bare repo was created
        assert git_repo_manager.repo_exists(repo_id) is True

    async def test_ingest_bare_repo_has_git_structure(self, client, git_repo_manager, temp_git_repos_dir):
        """Created bare repo has valid git structure."""
        payload = ingest_payload(name="TestRepo")
        response = await client.post("/api/repos/ingest", json=payload)
        result = response.json()
        repo_id = result["id"]

        repo_path = temp_git_repos_dir / f"{repo_id}.git"
        assert (repo_path / "HEAD").exists()
        assert (repo_path / "objects").is_dir()
        assert (repo_path / "refs").is_dir()

    async def test_ingest_sets_is_ingested_true(self, client, git_repo_manager):
        """Ingest sets is_ingested to True on the repo."""
        payload = ingest_payload(name="TestRepo")
        response = await client.post("/api/repos/ingest", json=payload)
        repo_id = response.json()["id"]

        # Fetch the repo and check is_ingested
        get_response = await client.get(f"/api/repos/{repo_id}")
        result = get_response.json()

        assert result["is_ingested"] is True

    async def test_ingest_with_remote_url(self, client, git_repo_manager):
        """Ingest accepts remote_url for later landing."""
        payload = ingest_payload(
            name="TestRepo",
            remote_url="https://github.com/org/project.git"
        )
        response = await client.post("/api/repos/ingest", json=payload)
        repo_id = response.json()["id"]

        # Verify remote_url is stored
        get_response = await client.get(f"/api/repos/{repo_id}")
        result = get_response.json()

        assert result["remote_url"] == "https://github.com/org/project.git"

    async def test_ingest_with_custom_default_branch(self, client, git_repo_manager):
        """Ingest accepts custom default branch."""
        payload = ingest_payload(name="TestRepo", default_branch="develop")
        response = await client.post("/api/repos/ingest", json=payload)
        repo_id = response.json()["id"]

        get_response = await client.get(f"/api/repos/{repo_id}")
        result = get_response.json()

        assert result["default_branch"] == "develop"

    async def test_ingest_missing_name_fails(self, client, git_repo_manager):
        """Ingest fails without name."""
        payload = {"remote_url": "https://github.com/org/test.git"}
        response = await client.post("/api/repos/ingest", json=payload)
        assert_status_code(response, 422)

    async def test_ingest_multiple_repos(self, client, git_repo_manager):
        """Can ingest multiple repos."""
        repo_ids = []
        for i in range(3):
            payload = ingest_payload(name=f"Repo{i}")
            response = await client.post("/api/repos/ingest", json=payload)
            repo_ids.append(response.json()["id"])

        # All should exist
        for repo_id in repo_ids:
            assert git_repo_manager.repo_exists(repo_id) is True


# -----------------------------------------------------------------------------
# Clone URL Endpoint Tests
# -----------------------------------------------------------------------------

class TestCloneUrlEndpoint:
    """Tests for GET /api/repos/{id}/clone-url endpoint."""

    async def test_returns_200_for_existing_repo(self, client, git_repo_manager):
        """Returns 200 for existing repo."""
        # Create repo via ingest
        payload = ingest_payload(name="TestRepo")
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        repo_id = ingest_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}/clone-url")
        assert_status_code(response, 200)

    async def test_returns_404_for_nonexistent_repo(self, client, git_repo_manager):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/nonexistent-id-12345/clone-url")
        assert_status_code(response, 404)

    async def test_returns_clone_url(self, client, git_repo_manager):
        """Response includes clone_url."""
        payload = ingest_payload(name="TestRepo")
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        repo_id = ingest_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}/clone-url")
        result = response.json()

        assert "clone_url" in result
        assert repo_id in result["clone_url"]
        assert result["clone_url"].endswith(".git")

    async def test_returns_is_ingested_status(self, client, git_repo_manager):
        """Response includes is_ingested status."""
        payload = ingest_payload(name="TestRepo")
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        repo_id = ingest_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}/clone-url")
        result = response.json()

        assert "is_ingested" in result
        assert result["is_ingested"] is True

    async def test_clone_url_for_non_ingested_repo(self, client, db_session, git_repo_manager):
        """Clone URL works for non-ingested repo (created via old endpoint)."""
        # Create repo via old endpoint (not ingest)
        payload = repo_create_payload(name="OldStyleRepo")
        create_response = await client.post("/api/repos", json=payload)
        repo_id = create_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}/clone-url")
        result = response.json()

        assert result["is_ingested"] is False
        assert "clone_url" in result


# -----------------------------------------------------------------------------
# Delete Endpoint Git Cleanup Tests
# -----------------------------------------------------------------------------

class TestDeleteGitCleanup:
    """Tests for DELETE /api/repos/{id} git storage cleanup."""

    async def test_delete_removes_git_storage(self, client, git_repo_manager):
        """Delete removes git storage for ingested repo."""
        # Create via ingest
        payload = ingest_payload(name="TestRepo")
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        repo_id = ingest_response.json()["id"]

        # Verify git repo exists
        assert git_repo_manager.repo_exists(repo_id) is True

        # Delete
        response = await client.delete(f"/api/repos/{repo_id}")
        assert_deleted_response(response)

        # Verify git repo is gone
        assert git_repo_manager.repo_exists(repo_id) is False

    async def test_delete_non_ingested_repo_succeeds(self, client, git_repo_manager):
        """Delete works for non-ingested repos (no git storage to clean)."""
        # Create via old endpoint
        payload = repo_create_payload(name="OldStyleRepo")
        create_response = await client.post("/api/repos", json=payload)
        repo_id = create_response.json()["id"]

        # Delete should not fail even though no git storage
        response = await client.delete(f"/api/repos/{repo_id}")
        assert_deleted_response(response)

    async def test_delete_removes_from_db_and_git(self, client, git_repo_manager):
        """Delete removes both DB record and git storage."""
        payload = ingest_payload(name="TestRepo")
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        repo_id = ingest_response.json()["id"]

        await client.delete(f"/api/repos/{repo_id}")

        # Check DB
        get_response = await client.get(f"/api/repos/{repo_id}")
        assert_not_found(get_response, "Repo")

        # Check git
        assert git_repo_manager.repo_exists(repo_id) is False


# -----------------------------------------------------------------------------
# Git Endpoints Integration Tests
# -----------------------------------------------------------------------------

class TestIngestGitIntegration:
    """Tests verifying ingest creates properly functional git repos."""

    async def test_ingested_repo_accessible_via_git_endpoints(self, client, git_repo_manager):
        """Ingested repo is accessible via /git/ endpoints."""
        payload = ingest_payload(name="TestRepo")
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        repo_id = ingest_response.json()["id"]

        # Should be able to access info/refs
        git_response = await client.get(
            f"/git/{repo_id}.git/info/refs",
            params={"service": "git-upload-pack"}
        )
        assert_status_code(git_response, 200)

    async def test_ingested_repo_head_accessible(self, client, git_repo_manager):
        """Ingested repo HEAD is accessible."""
        payload = ingest_payload(name="TestRepo")
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        repo_id = ingest_response.json()["id"]

        response = await client.get(f"/git/{repo_id}.git/HEAD")
        assert_status_code(response, 200)
        assert response.text.startswith("ref: refs/heads/")

    async def test_clone_url_matches_git_endpoint(self, client, git_repo_manager):
        """Clone URL returned by ingest matches git endpoint pattern."""
        payload = ingest_payload(name="TestRepo")
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        result = ingest_response.json()

        clone_url = result["clone_url"]
        repo_id = result["id"]

        # Extract path from clone_url and verify it works
        # clone_url is like "http://test/git/{id}.git"
        assert f"/git/{repo_id}.git" in clone_url


# -----------------------------------------------------------------------------
# Edge Cases
# -----------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases and error scenarios."""

    async def test_ingest_creates_unique_ids(self, client, git_repo_manager):
        """Each ingest creates a unique repo ID."""
        ids = set()
        for _ in range(5):
            payload = ingest_payload(name="TestRepo")
            response = await client.post("/api/repos/ingest", json=payload)
            ids.add(response.json()["id"])

        assert len(ids) == 5

    async def test_ingest_same_name_allowed(self, client, git_repo_manager):
        """Can ingest multiple repos with same name (different IDs)."""
        payload1 = ingest_payload(name="SameName")
        payload2 = ingest_payload(name="SameName")

        response1 = await client.post("/api/repos/ingest", json=payload1)
        response2 = await client.post("/api/repos/ingest", json=payload2)

        assert_status_code(response1, 201)
        assert_status_code(response2, 201)
        assert response1.json()["id"] != response2.json()["id"]

    async def test_clone_url_format_consistent(self, client, git_repo_manager):
        """Clone URL format is consistent across requests."""
        payload = ingest_payload(name="TestRepo")
        ingest_response = await client.post("/api/repos/ingest", json=payload)
        repo_id = ingest_response.json()["id"]

        # Get clone URL twice
        url_response1 = await client.get(f"/api/repos/{repo_id}/clone-url")
        url_response2 = await client.get(f"/api/repos/{repo_id}/clone-url")

        assert url_response1.json()["clone_url"] == url_response2.json()["clone_url"]
