"""
Integration tests for Repos API endpoints.

These tests verify the full request/response cycle through the FastAPI
application with a real (in-memory) database.
"""
import sys
from pathlib import Path

import pytest

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from shared.factories import repo_create_payload, repo_update_payload
from shared.assertions import (
    assert_status_code,
    assert_created_response,
    assert_updated_response,
    assert_deleted_response,
    assert_not_found,
    assert_json_list_length,
    assert_json_contains,
)


class TestListRepos:
    """Tests for GET /api/repos endpoint."""

    async def test_list_repos_empty(self, client):
        """Returns empty list when no repos exist."""
        response = await client.get("/api/repos")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_repos_with_data(self, client):
        """Returns all repos when they exist."""
        # Create two repos
        await client.post("/api/repos", json=repo_create_payload(name="Repo1"))
        await client.post("/api/repos", json=repo_create_payload(name="Repo2"))

        response = await client.get("/api/repos")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_repos_returns_repo_fields(self, client):
        """Returns repos with all expected fields."""
        payload = repo_create_payload(
            name="TestRepo",
            remote_url="https://github.com/org/test.git",
        )
        await client.post("/api/repos", json=payload)

        response = await client.get("/api/repos")
        repos = response.json()
        assert len(repos) == 1
        repo = repos[0]
        assert "id" in repo
        assert repo["name"] == "TestRepo"
        assert repo["remote_url"] == "https://github.com/org/test.git"
        assert "is_ingested" in repo
        assert "internal_git_url" in repo
        assert "created_at" in repo


class TestCreateRepo:
    """Tests for POST /api/repos endpoint."""

    async def test_create_repo_minimal(self, client):
        """Creates repo with minimal required fields."""
        payload = repo_create_payload(name="MinimalRepo")

        response = await client.post("/api/repos", json=payload)
        result = assert_created_response(response, {"name": "MinimalRepo"})
        assert result["default_branch"] == "main"
        assert result["is_ingested"] is False
        assert "internal_git_url" in result

    async def test_create_repo_full(self, client):
        """Creates repo with all fields specified."""
        payload = {
            "name": "FullRepo",
            "remote_url": "https://github.com/org/full.git",
            "default_branch": "dev",
        }

        response = await client.post("/api/repos", json=payload)
        result = assert_created_response(response, {"name": "FullRepo"})
        assert result["remote_url"] == "https://github.com/org/full.git"
        assert result["default_branch"] == "dev"
        assert "internal_git_url" in result

    async def test_create_repo_generates_uuid(self, client):
        """Creates repo with auto-generated UUID."""
        payload = repo_create_payload()

        response = await client.post("/api/repos", json=payload)
        result = response.json()
        assert "id" in result
        assert len(result["id"]) == 36  # UUID format

    async def test_create_repo_missing_name_fails(self, client):
        """Fails to create repo without name."""
        payload = {"remote_url": "https://github.com/org/test.git"}

        response = await client.post("/api/repos", json=payload)
        assert_status_code(response, 422)  # Validation error

    async def test_create_repo_name_only_succeeds(self, client):
        """Creates repo with only name (all other fields have defaults)."""
        payload = {"name": "NameOnlyRepo"}

        response = await client.post("/api/repos", json=payload)
        assert_status_code(response, 201)
        result = response.json()
        assert result["name"] == "NameOnlyRepo"
        assert result["default_branch"] == "main"


class TestGetRepo:
    """Tests for GET /api/repos/{repo_id} endpoint."""

    async def test_get_repo_exists(self, client):
        """Returns repo when it exists."""
        create_payload = repo_create_payload(name="GetTestRepo")
        create_response = await client.post("/api/repos", json=create_payload)
        repo_id = create_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}")
        assert_status_code(response, 200)
        assert_json_contains(response, {"id": repo_id, "name": "GetTestRepo"})

    async def test_get_repo_not_found(self, client):
        """Returns 404 when repo does not exist."""
        response = await client.get("/api/repos/nonexistent-id-12345")
        assert_not_found(response, "Repo")

    async def test_get_repo_returns_all_fields(self, client):
        """Returns repo with complete field set."""
        create_payload = {
            "name": "CompleteRepo",
            "remote_url": "https://github.com/org/complete.git",
            "default_branch": "develop",
        }
        create_response = await client.post("/api/repos", json=create_payload)
        repo_id = create_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}")
        result = response.json()
        assert result["name"] == "CompleteRepo"
        assert result["remote_url"] == "https://github.com/org/complete.git"
        assert result["default_branch"] == "develop"
        assert "is_ingested" in result
        assert "internal_git_url" in result
        assert "created_at" in result


class TestUpdateRepo:
    """Tests for PATCH /api/repos/{repo_id} endpoint."""

    async def test_update_repo_name(self, client):
        """Updates repo name only."""
        create_response = await client.post(
            "/api/repos",
            json=repo_create_payload(name="OriginalName"),
        )
        repo_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/repos/{repo_id}",
            json=repo_update_payload(name="UpdatedName"),
        )
        result = assert_updated_response(response, {"name": "UpdatedName"})
        assert result["id"] == repo_id

    async def test_update_repo_multiple_fields(self, client):
        """Updates multiple repo fields at once."""
        create_response = await client.post(
            "/api/repos",
            json=repo_create_payload(),
        )
        repo_id = create_response.json()["id"]

        update_data = {
            "name": "NewName",
            "remote_url": "https://github.com/org/updated.git",
            "default_branch": "develop",
        }
        response = await client.patch(f"/api/repos/{repo_id}", json=update_data)
        result = response.json()
        assert result["name"] == "NewName"
        assert result["remote_url"] == "https://github.com/org/updated.git"
        assert result["default_branch"] == "develop"

    async def test_update_repo_not_found(self, client):
        """Returns 404 when updating non-existent repo."""
        response = await client.patch(
            "/api/repos/nonexistent-id",
            json=repo_update_payload(name="NewName"),
        )
        assert_not_found(response, "Repo")

    async def test_update_repo_empty_body(self, client):
        """Accepts empty update body (no changes)."""
        create_response = await client.post(
            "/api/repos",
            json=repo_create_payload(name="UnchangedRepo"),
        )
        repo_id = create_response.json()["id"]

        response = await client.patch(f"/api/repos/{repo_id}", json={})
        assert_status_code(response, 200)
        assert response.json()["name"] == "UnchangedRepo"


class TestDeleteRepo:
    """Tests for DELETE /api/repos/{repo_id} endpoint."""

    async def test_delete_repo_exists(self, client):
        """Deletes repo when it exists."""
        create_response = await client.post(
            "/api/repos",
            json=repo_create_payload(),
        )
        repo_id = create_response.json()["id"]

        response = await client.delete(f"/api/repos/{repo_id}")
        assert_deleted_response(response)

        # Verify repo is gone
        get_response = await client.get(f"/api/repos/{repo_id}")
        assert_not_found(get_response, "Repo")

    async def test_delete_repo_not_found(self, client):
        """Returns 404 when deleting non-existent repo."""
        response = await client.delete("/api/repos/nonexistent-id")
        assert_not_found(response, "Repo")

    async def test_delete_repo_removes_from_list(self, client):
        """Deleted repo no longer appears in list."""
        # Create two repos
        resp1 = await client.post("/api/repos", json=repo_create_payload(name="Keep"))
        resp2 = await client.post("/api/repos", json=repo_create_payload(name="Delete"))

        # Delete one
        await client.delete(f"/api/repos/{resp2.json()['id']}")

        # Verify only one remains
        list_response = await client.get("/api/repos")
        repos = list_response.json()
        assert len(repos) == 1
        assert repos[0]["name"] == "Keep"
