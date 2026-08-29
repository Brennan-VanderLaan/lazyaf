"""
Integration tests for card <-> spec layer links (Phase 12.2.5).

Cards gain nullable feature_id / user_story_id and a promote-to-feature
endpoint.

The spec router is registered in main.py; if that registration is ever
dropped, these tests fail loudly with 404s (no local re-registration).
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

from shared.factories import repo_create_payload, card_create_payload
from shared.assertions import (
    assert_status_code,
    assert_created_response,
    assert_not_found,
)


@pytest_asyncio.fixture
async def repo(client):
    response = await client.post(
        "/api/repos",
        json=repo_create_payload(name="CardSpecLinkRepo"),
    )
    return response.json()


@pytest_asyncio.fixture
async def card(client, repo):
    response = await client.post(
        f"/api/repos/{repo['id']}/cards",
        json=card_create_payload(
            title="Add revoke endpoint",
            description="Users can revoke API keys",
        ),
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def feature(client):
    response = await client.post(
        "/api/features",
        json={"title": "Key management"},
    )
    return response.json()


@pytest_asyncio.fixture
async def story(client, feature):
    response = await client.post(
        f"/api/features/{feature['id']}/stories",
        json={"title": "Revoke a key"},
    )
    return response.json()


class TestCardSpecLinks:
    """Tests for feature_id / user_story_id on cards."""

    async def test_card_read_exposes_link_fields_as_null(self, client, card):
        """New cards expose feature_id/user_story_id as null."""
        response = await client.get(f"/api/cards/{card['id']}")
        result = response.json()
        assert result["feature_id"] is None
        assert result["user_story_id"] is None

    async def test_card_can_link_to_feature(self, client, card, feature):
        response = await client.patch(
            f"/api/cards/{card['id']}",
            json={"feature_id": feature["id"]},
        )
        assert_status_code(response, 200)
        assert response.json()["feature_id"] == feature["id"]

        # Persisted, not just echoed
        fetched = (await client.get(f"/api/cards/{card['id']}")).json()
        assert fetched["feature_id"] == feature["id"]

    async def test_card_can_link_to_story(self, client, card, feature, story):
        response = await client.patch(
            f"/api/cards/{card['id']}",
            json={"feature_id": feature["id"], "user_story_id": story["id"]},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["user_story_id"] == story["id"]
        assert result["feature_id"] == feature["id"]

    async def test_card_link_unknown_feature_404(self, client, card):
        response = await client.patch(
            f"/api/cards/{card['id']}",
            json={"feature_id": "no-such-feature"},
        )
        assert_not_found(response, "Feature")

    async def test_card_link_unknown_story_404(self, client, card):
        response = await client.patch(
            f"/api/cards/{card['id']}",
            json={"user_story_id": "no-such-story"},
        )
        assert_not_found(response, "User story")

    async def test_card_unlink_with_explicit_null(self, client, card, feature):
        await client.patch(
            f"/api/cards/{card['id']}", json={"feature_id": feature["id"]}
        )
        response = await client.patch(
            f"/api/cards/{card['id']}", json={"feature_id": None}
        )
        assert_status_code(response, 200)
        assert response.json()["feature_id"] is None

    async def test_card_update_without_link_fields_leaves_links_alone(
        self, client, card, feature
    ):
        """A PATCH that omits link fields must not clear existing links."""
        await client.patch(
            f"/api/cards/{card['id']}", json={"feature_id": feature["id"]}
        )
        response = await client.patch(
            f"/api/cards/{card['id']}", json={"title": "Renamed card"}
        )
        result = response.json()
        assert result["title"] == "Renamed card"
        assert result["feature_id"] == feature["id"]


class TestPromoteCardToFeature:
    """Tests for POST /api/cards/{card_id}/promote-to-feature."""

    async def test_promote_card_creates_feature(self, client, repo, card):
        response = await client.post(
            f"/api/cards/{card['id']}/promote-to-feature"
        )
        feature = assert_created_response(response)

        # Feature mirrors the card and scopes to the card's repo
        assert feature["title"] == card["title"]
        assert feature["description"] == card["description"]
        assert feature["repo_ids"] == [repo["id"]]
        assert feature["status"] == "draft"

        # Original card is linked to the new feature
        fetched = (await client.get(f"/api/cards/{card['id']}")).json()
        assert fetched["feature_id"] == feature["id"]

    async def test_promoted_feature_queryable_via_features_api(self, client, card):
        feature = (
            await client.post(f"/api/cards/{card['id']}/promote-to-feature")
        ).json()
        response = await client.get(f"/api/features/{feature['id']}")
        assert_status_code(response, 200)
        assert response.json()["title"] == card["title"]

    async def test_promote_card_already_linked_400(self, client, card, feature):
        await client.patch(
            f"/api/cards/{card['id']}", json={"feature_id": feature["id"]}
        )
        response = await client.post(
            f"/api/cards/{card['id']}/promote-to-feature"
        )
        assert_status_code(response, 400)
        assert "already linked" in response.json()["detail"]

    async def test_promote_card_not_found(self, client):
        response = await client.post("/api/cards/no-such-card/promote-to-feature")
        assert_not_found(response, "Card")

    async def test_delete_feature_unlinks_promoted_card(self, client, card):
        """Deleting a feature nulls the card link instead of dangling it."""
        feature = (
            await client.post(f"/api/cards/{card['id']}/promote-to-feature")
        ).json()
        delete_response = await client.delete(f"/api/features/{feature['id']}")
        assert_status_code(delete_response, 204)

        fetched = (await client.get(f"/api/cards/{card['id']}")).json()
        assert fetched["feature_id"] is None

    async def test_delete_story_unlinks_card(self, client, card, feature, story):
        await client.patch(
            f"/api/cards/{card['id']}",
            json={"feature_id": feature["id"], "user_story_id": story["id"]},
        )
        delete_response = await client.delete(f"/api/user-stories/{story['id']}")
        assert_status_code(delete_response, 204)

        fetched = (await client.get(f"/api/cards/{card['id']}")).json()
        assert fetched["user_story_id"] is None
        # Feature link is untouched by story deletion
        assert fetched["feature_id"] == feature["id"]
