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

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

# After the path setup, matching the pattern in tdd/integration/api/*.
from shared.git_seed import seed_branch  # noqa: E402


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

    async def test_cards_lifecycle_actions(self, client, clean_git_repos, clean_runner_registry):
        """Card lifecycle actions respond correctly."""
        # Must use ingested repo to start cards
        repo_response = await client.post(
            "/api/repos/ingest",
            json={"name": "lifecycle-repo"},
        )
        repo_id = repo_response.json()["id"]
        # A card cannot start on a repo with no default branch to branch FROM.
        default_branch = (await client.get(f"/api/repos/{repo_id}")).json()[
            "default_branch"
        ]
        seed_branch(repo_id, default_branch, path="README.md", content=b"seed\n")

        card_response = await client.post(
            f"/api/repos/{repo_id}/cards",
            json={"title": "Lifecycle card"},
        )
        card_id = card_response.json()["id"]

        # Start (requires ingested repo)
        start_response = await client.post(f"/api/cards/{card_id}/start")
        assert start_response.status_code == 200
        assert start_response.json()["status"] == "in_progress"

        # PATCH is the board's drag handler, NOT a lifecycle backdoor. Since
        # 12.7 it refuses `in_progress` and `done` in both directions
        # (MANUAL_STATUSES in app/routers/cards.py, QA finding T2): a card
        # reaches Done by being merged, not by being written.
        blocked = await client.patch(
            f"/api/cards/{card_id}",
            json={"status": "done"},
        )
        assert blocked.status_code == 400, blocked.text
        assert "done" in blocked.json()["detail"]

        # The way out of `in_progress` is the endpoint that cancels the work.
        reject_response = await client.post(f"/api/cards/{card_id}/reject")
        assert reject_response.status_code == 200
        assert reject_response.json()["status"] == "todo"

    async def test_runners_list(self, client):
        """Runners list endpoint responds."""
        response = await client.get("/api/runners")
        assert response.status_code == 200
        assert isinstance(response.json(), list)

    async def test_runners_list_is_the_whole_surface(self):
        """12.6: the runners API is READ-ONLY over the registry.

        `POST /register` (and heartbeat / job / complete / logs /
        docker-command) belonged to the polling stack. A runner enrols over
        `/ws/runner` now, so the only HTTP left is the snapshot the panel
        renders - and asserting the register route is GONE is what stops it
        being quietly re-added as "just a convenience".
        """
        from app.routers import runners as runners_router

        paths = {
            route.path
            for route in runners_router.router.routes
        }
        assert paths == {"/api/runners", "/api/runners/{runner_id}"}, paths
        methods = {
            method
            for route in runners_router.router.routes
            for method in route.methods
        }
        assert methods == {"GET"}, methods


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
