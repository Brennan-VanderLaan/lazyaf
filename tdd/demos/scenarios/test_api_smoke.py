"""
Demo: API Smoke Tests

Quick sanity checks to verify all API endpoints are responding.
Run these tests to ensure the API is operational before running
the full test suite.

Run with: pytest tdd/demos/scenarios/test_api_smoke.py -v
"""
import sys
from pathlib import Path

import pytest

# Add backend to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
sys.path.insert(0, str(backend_path))


@pytest.mark.demo
class TestAPISmokeTests:
    """
    Smoke tests for all API endpoints.

    These tests verify basic connectivity and response codes
    without deep validation of business logic.
    """

    async def test_health_check(self, client):
        """API health check responds."""
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    async def test_root_endpoint(self, client):
        """Root endpoint responds with welcome."""
        response = await client.get("/")
        assert response.status_code == 200
        assert "message" in response.json()

    async def test_repos_list(self, client):
        """Repos list endpoint responds."""
        response = await client.get("/api/repos")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_repos_create(self, client):
        """Repos create endpoint accepts valid data."""
        response = await client.post(
            "/api/repos",
            json={"name": "smoke-repo"},
        )
        assert response.status_code == 201
        assert "id" in response.json()

    async def test_repos_get(self, client):
        """Repos get endpoint retrieves created repo."""
        create_response = await client.post(
            "/api/repos",
            json={"name": "get-repo"},
        )
        repo_id = create_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}")
        assert response.status_code == 200

    async def test_repos_update(self, client):
        """Repos update endpoint accepts changes."""
        create_response = await client.post(
            "/api/repos",
            json={"name": "update-repo"},
        )
        repo_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/repos/{repo_id}",
            json={"name": "updated-repo"},
        )
        assert response.status_code == 200

    async def test_repos_delete(self, client):
        """Repos delete endpoint removes repo."""
        create_response = await client.post(
            "/api/repos",
            json={"name": "delete-repo"},
        )
        repo_id = create_response.json()["id"]

        response = await client.delete(f"/api/repos/{repo_id}")
        assert response.status_code == 204

    async def test_cards_list(self, client):
        """Cards list endpoint responds for valid repo."""
        repo_response = await client.post(
            "/api/repos",
            json={"name": "cards-repo"},
        )
        repo_id = repo_response.json()["id"]

        response = await client.get(f"/api/repos/{repo_id}/cards")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_cards_create(self, client):
        """Cards create endpoint accepts valid data."""
        repo_response = await client.post(
            "/api/repos",
            json={"name": "card-create-repo"},
        )
        repo_id = repo_response.json()["id"]

        response = await client.post(
            f"/api/repos/{repo_id}/cards",
            json={"title": "Smoke test card"},
        )
        assert response.status_code == 201
        assert "id" in response.json()

    async def test_cards_get(self, client):
        """Cards get endpoint retrieves created card."""
        repo_response = await client.post(
            "/api/repos",
            json={"name": "card-get-repo"},
        )
        repo_id = repo_response.json()["id"]

        card_response = await client.post(
            f"/api/repos/{repo_id}/cards",
            json={"title": "Get test card"},
        )
        card_id = card_response.json()["id"]

        response = await client.get(f"/api/cards/{card_id}")
        assert response.status_code == 200

    async def test_cards_lifecycle_actions(self, client, clean_git_repos, clean_job_queue):
        """Card lifecycle actions respond correctly."""
        # Must use ingested repo to start cards
        repo_response = await client.post(
            "/api/repos/ingest",
            json={"name": "lifecycle-repo"},
        )
        repo_id = repo_response.json()["id"]

        card_response = await client.post(
            f"/api/repos/{repo_id}/cards",
            json={"title": "Lifecycle card"},
        )
        card_id = card_response.json()["id"]

        # Start (requires ingested repo)
        start_response = await client.post(f"/api/cards/{card_id}/start")
        assert start_response.status_code == 200

        # Approve (now returns {card, merge_result})
        approve_response = await client.post(
            f"/api/cards/{card_id}/approve",
            json={"target_branch": None},
        )
        assert approve_response.status_code == 200
        assert approve_response.json()["card"]["status"] == "done"

    async def test_runners_list(self, client):
        """Runners list endpoint responds."""
        response = await client.get("/api/runners")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_runners_register(self, client):
        """Runners register endpoint accepts request."""
        response = await client.post(
            "/api/runners/register",
            json={"name": "test-runner"},
        )
        assert response.status_code == 200
        data = response.json()
        assert "runner_id" in data
        assert "name" in data


@pytest.mark.demo
class TestAPIErrorHandling:
    """Smoke tests for API error handling."""

    async def test_404_on_missing_repo(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/does-not-exist")
        assert response.status_code == 404
        assert "detail" in response.json()

    async def test_404_on_missing_card(self, client):
        """Returns 404 for non-existent card."""
        response = await client.get("/api/cards/does-not-exist")
        assert response.status_code == 404
        assert "detail" in response.json()

    async def test_404_on_missing_job(self, client):
        """Returns 404 for non-existent job."""
        response = await client.get("/api/jobs/does-not-exist")
        assert response.status_code == 404
        assert "detail" in response.json()

    async def test_422_on_invalid_repo_data(self, client):
        """Returns 422 for invalid repo data."""
        response = await client.post(
            "/api/repos",
            json={"invalid": "data"},
        )
        assert response.status_code == 422

    async def test_422_on_invalid_card_data(self, client):
        """Returns 422 for invalid card data."""
        repo_response = await client.post(
            "/api/repos",
            json={"name": "error-repo"},
        )
        repo_id = repo_response.json()["id"]

        response = await client.post(
            f"/api/repos/{repo_id}/cards",
            json={},  # Missing required title
        )
        assert response.status_code == 422
