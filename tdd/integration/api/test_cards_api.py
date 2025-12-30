"""
Integration tests for Cards API endpoints.

These tests verify the full request/response cycle for card management,
including status transitions and card lifecycle operations.
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

from shared.factories import repo_create_payload, repo_ingest_payload, card_create_payload, card_update_payload
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
    """Create a repo for card tests."""
    response = await client.post(
        "/api/repos",
        json=repo_create_payload(name="CardTestRepo"),
    )
    return response.json()


@pytest_asyncio.fixture
async def ingested_repo(client, clean_git_repos):
    """Create an ingested repo for card lifecycle tests that require starting jobs."""
    response = await client.post(
        "/api/repos/ingest",
        json=repo_ingest_payload(name="IngestedCardTestRepo"),
    )
    return response.json()


class TestListCards:
    """Tests for GET /api/repos/{repo_id}/cards endpoint."""

    async def test_list_cards_empty(self, client, repo):
        """Returns empty list when repo has no cards."""
        response = await client.get(f"/api/repos/{repo['id']}/cards")
        assert_status_code(response, 200)
        assert_json_list_length(response, 0)

    async def test_list_cards_with_data(self, client, repo):
        """Returns all cards for a repo."""
        # Create cards
        await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Card 1"),
        )
        await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Card 2"),
        )

        response = await client.get(f"/api/repos/{repo['id']}/cards")
        assert_status_code(response, 200)
        assert_json_list_length(response, 2)

    async def test_list_cards_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.get("/api/repos/nonexistent-repo/cards")
        assert_not_found(response, "Repo")

    async def test_list_cards_only_for_repo(self, client):
        """Returns only cards belonging to specified repo."""
        # Create two repos
        resp1 = await client.post("/api/repos", json=repo_create_payload(name="Repo1"))
        resp2 = await client.post("/api/repos", json=repo_create_payload(name="Repo2"))
        repo1_id = resp1.json()["id"]
        repo2_id = resp2.json()["id"]

        # Create cards in each repo
        await client.post(
            f"/api/repos/{repo1_id}/cards",
            json=card_create_payload(title="Repo1 Card"),
        )
        await client.post(
            f"/api/repos/{repo2_id}/cards",
            json=card_create_payload(title="Repo2 Card"),
        )

        # Verify isolation
        response = await client.get(f"/api/repos/{repo1_id}/cards")
        cards = response.json()
        assert len(cards) == 1
        assert cards[0]["title"] == "Repo1 Card"


class TestCreateCard:
    """Tests for POST /api/repos/{repo_id}/cards endpoint."""

    async def test_create_card_minimal(self, client, repo):
        """Creates card with minimal required fields."""
        payload = card_create_payload(title="Minimal Card")

        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=payload,
        )
        result = assert_created_response(response, {"title": "Minimal Card"})
        assert result["repo_id"] == repo["id"]
        assert result["status"] == "todo"

    async def test_create_card_full(self, client, repo):
        """Creates card with all fields."""
        payload = {
            "title": "Full Card",
            "description": "Detailed description of the feature",
        }

        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=payload,
        )
        result = response.json()
        assert result["title"] == "Full Card"
        assert result["description"] == "Detailed description of the feature"

    async def test_create_card_defaults(self, client, repo):
        """Creates card with expected default values."""
        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(),
        )
        result = response.json()
        assert result["status"] == "todo"
        assert result["branch_name"] is None
        assert result["pr_url"] is None
        assert result["job_id"] is None

    async def test_create_card_repo_not_found(self, client):
        """Returns 404 for non-existent repo."""
        response = await client.post(
            "/api/repos/nonexistent-repo/cards",
            json=card_create_payload(),
        )
        assert_not_found(response, "Repo")

    async def test_create_card_missing_title_fails(self, client, repo):
        """Fails without required title field."""
        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json={"description": "No title"},
        )
        assert_status_code(response, 422)


class TestGetCard:
    """Tests for GET /api/cards/{card_id} endpoint."""

    async def test_get_card_exists(self, client, repo):
        """Returns card when it exists."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="GetTest Card"),
        )
        card_id = create_response.json()["id"]

        response = await client.get(f"/api/cards/{card_id}")
        assert_status_code(response, 200)
        assert_json_contains(response, {"id": card_id, "title": "GetTest Card"})

    async def test_get_card_not_found(self, client):
        """Returns 404 for non-existent card."""
        response = await client.get("/api/cards/nonexistent-card-id")
        assert_not_found(response, "Card")

    async def test_get_card_returns_all_fields(self, client, repo):
        """Returns card with complete field set."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json={"title": "Complete Card", "description": "Full description"},
        )
        card_id = create_response.json()["id"]

        response = await client.get(f"/api/cards/{card_id}")
        result = response.json()
        assert "id" in result
        assert "repo_id" in result
        assert "title" in result
        assert "description" in result
        assert "status" in result
        assert "created_at" in result
        assert "updated_at" in result


class TestUpdateCard:
    """Tests for PATCH /api/cards/{card_id} endpoint."""

    async def test_update_card_title(self, client, repo):
        """Updates card title only."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Original Title"),
        )
        card_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/cards/{card_id}",
            json={"title": "Updated Title"},
        )
        result = assert_updated_response(response, {"title": "Updated Title"})
        assert result["id"] == card_id

    async def test_update_card_status(self, client, repo):
        """Updates card status."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(),
        )
        card_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/cards/{card_id}",
            json={"status": "in_progress"},
        )
        assert response.json()["status"] == "in_progress"

    async def test_update_card_description(self, client, repo):
        """Updates card description."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(description="Original"),
        )
        card_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/cards/{card_id}",
            json={"description": "Updated description"},
        )
        assert response.json()["description"] == "Updated description"

    async def test_update_card_not_found(self, client):
        """Returns 404 for non-existent card."""
        response = await client.patch(
            "/api/cards/nonexistent-id",
            json={"title": "New Title"},
        )
        assert_not_found(response, "Card")


class TestDeleteCard:
    """Tests for DELETE /api/cards/{card_id} endpoint."""

    async def test_delete_card_exists(self, client, repo):
        """Deletes card when it exists."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(),
        )
        card_id = create_response.json()["id"]

        response = await client.delete(f"/api/cards/{card_id}")
        assert_deleted_response(response)

        # Verify card is gone
        get_response = await client.get(f"/api/cards/{card_id}")
        assert_not_found(get_response, "Card")

    async def test_delete_card_not_found(self, client):
        """Returns 404 for non-existent card."""
        response = await client.delete("/api/cards/nonexistent-id")
        assert_not_found(response, "Card")


class TestCardLifecycleActions:
    """Tests for card lifecycle endpoints: start, approve, reject."""

    async def test_start_card(self, client, ingested_repo, clean_job_queue):
        """POST /api/cards/{id}/start moves card to in_progress."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Feature to Start"),
        )
        card_id = create_response.json()["id"]

        response = await client.post(f"/api/cards/{card_id}/start")
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "in_progress"
        assert result["job_id"] is not None
        assert result["branch_name"] is not None

    async def test_start_card_not_found(self, client):
        """Returns 404 when starting non-existent card."""
        response = await client.post("/api/cards/nonexistent/start")
        assert_not_found(response, "Card")

    async def test_start_card_already_started(self, client, ingested_repo, clean_job_queue):
        """Returns 400 when starting card that is not in todo status."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Already Started"),
        )
        card_id = create_response.json()["id"]

        # Start once
        await client.post(f"/api/cards/{card_id}/start")

        # Try to start again
        response = await client.post(f"/api/cards/{card_id}/start")
        assert_status_code(response, 400)
        assert "todo" in response.json()["detail"].lower()

    async def test_approve_card(self, client, repo):
        """POST /api/cards/{id}/approve moves card to done."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Feature to Approve"),
        )
        card_id = create_response.json()["id"]

        # Move to in_review first
        await client.patch(f"/api/cards/{card_id}", json={"status": "in_review"})

        response = await client.post(f"/api/cards/{card_id}/approve")
        assert_status_code(response, 200)
        assert response.json()["status"] == "done"

    async def test_approve_card_not_found(self, client):
        """Returns 404 when approving non-existent card."""
        response = await client.post("/api/cards/nonexistent/approve")
        assert_not_found(response, "Card")

    async def test_reject_card(self, client, repo):
        """POST /api/cards/{id}/reject resets card to todo."""
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Feature to Reject"),
        )
        card_id = create_response.json()["id"]

        # Move to in_review with branch and PR
        await client.patch(
            f"/api/cards/{card_id}",
            json={"status": "in_review"},
        )

        response = await client.post(f"/api/cards/{card_id}/reject")
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "todo"
        assert result["branch_name"] is None
        assert result["pr_url"] is None

    async def test_reject_card_not_found(self, client):
        """Returns 404 when rejecting non-existent card."""
        response = await client.post("/api/cards/nonexistent/reject")
        assert_not_found(response, "Card")

    async def test_retry_failed_card(self, client, ingested_repo, clean_job_queue):
        """POST /api/cards/{id}/retry retries a failed card."""
        # Create card and move to failed status
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Feature to Retry"),
        )
        card_id = create_response.json()["id"]

        # Set card to failed status
        await client.patch(f"/api/cards/{card_id}", json={"status": "failed"})

        # Retry the card
        response = await client.post(f"/api/cards/{card_id}/retry")
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "in_progress"
        assert result["job_id"] is not None
        assert result["branch_name"] is not None

    async def test_retry_in_review_card(self, client, ingested_repo, clean_job_queue):
        """POST /api/cards/{id}/retry can retry a card in review."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Feature to Re-Review"),
        )
        card_id = create_response.json()["id"]

        # Move to in_review status
        await client.patch(f"/api/cards/{card_id}", json={"status": "in_review"})

        # Retry the card
        response = await client.post(f"/api/cards/{card_id}/retry")
        assert_status_code(response, 200)
        result = response.json()
        assert result["status"] == "in_progress"

    async def test_retry_todo_card_fails(self, client, ingested_repo):
        """Cannot retry a card in todo status."""
        create_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Todo Card"),
        )
        card_id = create_response.json()["id"]

        response = await client.post(f"/api/cards/{card_id}/retry")
        assert_status_code(response, 400)
        assert "failed" in response.json()["detail"].lower() or "in_review" in response.json()["detail"].lower()

    async def test_retry_card_not_found(self, client):
        """Returns 404 when retrying non-existent card."""
        response = await client.post("/api/cards/nonexistent/retry")
        assert_not_found(response, "Card")
