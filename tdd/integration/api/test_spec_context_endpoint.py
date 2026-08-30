"""
Integration: `GET /api/cards/{card_id}/spec-context`, the look-before-you-spend
surface for the 12.6.6 curated brief.

Two reasons this endpoint exists, both load-bearing:

- **R1.** A curated brief whose only observable form is a container's stdout,
  after a paid run has already burned, is dark.
- **The 12.6.5 exit gate.** "One experiment comparing with and without
  curation" needs a human to be able to see *what* was curated when a variant
  underperforms.

The test that matters most here is
`test_the_preview_is_what_dispatch_would_send`: it is what stops this endpoint
becoming a second, prettier, subtly different assembler (R3).

REGISTRATION. This lane does not own `backend/app/main.py`, so the router is
not yet in the app's include list. These tests therefore mount the PRODUCTION
router object (`app.routers.spec_context.router` - not a re-declaration of the
routes) onto a bare app that shares the test session. The integrator's line is:

    from app.routers import ... spec_context          # main.py import block
    app.include_router(spec_context.router)           # next to spec.router

Once that lands, this file keeps passing unchanged and the endpoint is also
reachable through the real app; `test_the_route_is_the_documented_path` is the
assertion that pins the path both sides have to agree on.
"""
import sys
from pathlib import Path

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

REPO_ROOT = Path(__file__).resolve().parents[3]
for _path in (REPO_ROOT / "backend", REPO_ROOT / "tdd", REPO_ROOT):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

from shared.factories import card_create_payload, repo_create_payload  # noqa: E402

from app.database import get_db  # noqa: E402
from app.routers import spec_context as spec_context_router  # noqa: E402
from app.services.control_layer.workspace import (  # noqa: E402
    SPEC_CONTEXT_MAX_BYTES,
    SPEC_CONTEXT_MAX_TOKENS,
    SPEC_CONTEXT_PATH,
)
from app.services.spec_context import build_spec_context  # noqa: E402

PREVIEW_PATH = "/api/cards/{card_id}/spec-context"


@pytest_asyncio.fixture
async def preview_client(db_session):
    """The PRODUCTION router, on a bare app, sharing the test session."""

    async def override_get_db():
        yield db_session

    app = FastAPI()
    app.include_router(spec_context_router.router)
    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


@pytest_asyncio.fixture
async def repo(client):
    response = await client.post(
        "/api/repos", json=repo_create_payload(name="SpecPreviewRepo")
    )
    assert response.status_code == 201, response.text
    return response.json()


@pytest_asyncio.fixture
async def spec(client):
    feature = (
        await client.post(
            "/api/features",
            json={
                "title": "Per-repo API rate limiting",
                "description": "Protect the public API from runaway clients.",
            },
        )
    ).json()
    story = (
        await client.post(
            f"/api/features/{feature['id']}/stories",
            json={
                "title": "Operator sets a per-repo request budget",
                "narrative": "As an operator I want to cap requests per repo.",
            },
        )
    ).json()
    criteria = []
    for text, required in (
        ("A repo over its budget receives HTTP 429.", True),
        ("Rate-limit headers are emitted on every response.", False),
    ):
        response = await client.post(
            f"/api/user-stories/{story['id']}/criteria",
            json={"text": text, "required": required},
        )
        assert response.status_code == 201, response.text
        criteria.append(response.json())
    return {"feature": feature, "story": story, "criteria": criteria}


@pytest_asyncio.fixture
async def linked_card(client, repo, spec):
    card = (
        await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(
                title="Implement the per-repo budget", description=""
            ),
        )
    ).json()
    patched = await client.patch(
        f"/api/cards/{card['id']}", json={"user_story_id": spec["story"]["id"]}
    )
    assert patched.status_code == 200, patched.text
    return patched.json()


@pytest_asyncio.fixture
async def unlinked_card(client, repo):
    response = await client.post(
        f"/api/repos/{repo['id']}/cards",
        json=card_create_payload(title="Bump the linter", description=""),
    )
    assert response.status_code == 201, response.text
    return response.json()


class TestTheRoute:
    def test_the_route_is_the_documented_path(self):
        """The one string main.py's registration makes reachable."""
        paths = [route.path for route in spec_context_router.router.routes]
        assert paths == [PREVIEW_PATH]
        methods = spec_context_router.router.routes[0].methods
        assert methods == {"GET"}


class TestLinkedCard:
    async def test_it_returns_the_bundle_and_its_provenance(
        self, preview_client, linked_card, spec
    ):
        response = await preview_client.get(
            f"/api/cards/{linked_card['id']}/spec-context"
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["card_id"] == linked_card["id"]
        assert "A repo over its budget receives HTTP 429." in body["markdown"]
        assert body["source"]["user_story_id"] == spec["story"]["id"]
        assert body["source"]["feature_id"] == spec["feature"]["id"]
        assert body["criteria_count"] == 2
        assert body["test_ref_count"] == 0
        assert body["estimated_tokens"] > 0
        assert body["truncated"] is False
        assert body["dropped"] == []

    async def test_it_reports_the_budget_the_assembler_truncates_against(
        self, preview_client, linked_card
    ):
        """A number with no scale is not an answer: an operator has to be able
        to see how close a bundle is to the cap without knowing the constant."""
        body = (
            await preview_client.get(
                f"/api/cards/{linked_card['id']}/spec-context"
            )
        ).json()
        assert body["budget_tokens"] == SPEC_CONTEXT_MAX_TOKENS
        assert body["budget_bytes"] == SPEC_CONTEXT_MAX_BYTES
        assert body["container_path"] == SPEC_CONTEXT_PATH
        assert body["estimated_tokens"] <= body["budget_tokens"]

    async def test_the_preview_is_what_dispatch_would_send(
        self, preview_client, db_session, repo, linked_card
    ):
        """THE anti-drift pin (R3). If this endpoint ever grows curation logic
        of its own it becomes a second assembler, and the number on the 12.6.5
        leaderboard stops meaning what the preview shows."""
        body = (
            await preview_client.get(
                f"/api/cards/{linked_card['id']}/spec-context"
            )
        ).json()
        dispatched = await build_spec_context(
            db_session, card_id=linked_card["id"], repo_id=repo["id"]
        )
        assert body["markdown"] == dispatched["markdown"]
        assert body["criteria_count"] == dispatched["criteria_count"]
        assert body["estimated_tokens"] == dispatched["estimated_tokens"]

    async def test_it_is_read_only(
        self, preview_client, db_session, linked_card
    ):
        """The bundle is derived at dispatch and stored nowhere; asking to see
        it must not create anything to go stale."""
        from sqlalchemy import func, select

        from app.models import Card

        before = (
            await db_session.execute(select(func.count()).select_from(Card))
        ).scalar_one()
        await preview_client.get(
            f"/api/cards/{linked_card['id']}/spec-context"
        )
        after = (
            await db_session.execute(select(func.count()).select_from(Card))
        ).scalar_one()
        assert before == after


class TestUnlinkedCard:
    async def test_no_links_is_a_successful_answer_not_a_404(
        self, preview_client, unlinked_card
    ):
        """"This card has no spec context" is a fact about a real card. 404 is
        reserved for a card that does not exist - collapsing the two makes
        "did the link get dropped?" unanswerable from the API."""
        response = await preview_client.get(
            f"/api/cards/{unlinked_card['id']}/spec-context"
        )
        assert response.status_code == 200, response.text
        body = response.json()

        assert body["markdown"] is None
        assert body["card_id"] == unlinked_card["id"]
        assert body["source"]["user_story_id"] is None
        assert body["source"]["feature_id"] is None
        assert body["criteria_count"] == 0
        assert body["test_ref_count"] == 0
        assert body["estimated_tokens"] == 0
        assert body["truncated"] is False
        assert body["dropped"] == []

    async def test_unlinking_a_card_empties_its_preview(
        self, preview_client, client, linked_card
    ):
        assert (
            await preview_client.get(
                f"/api/cards/{linked_card['id']}/spec-context"
            )
        ).json()["markdown"] is not None

        patched = await client.patch(
            f"/api/cards/{linked_card['id']}", json={"user_story_id": None}
        )
        assert patched.status_code == 200, patched.text

        assert (
            await preview_client.get(
                f"/api/cards/{linked_card['id']}/spec-context"
            )
        ).json()["markdown"] is None


class TestUnknownCard:
    async def test_an_unknown_card_is_a_404(self, preview_client):
        response = await preview_client.get(
            "/api/cards/no-such-card/spec-context"
        )
        assert response.status_code == 404, response.text
        assert "not found" in response.json()["detail"].lower()

    @pytest.mark.parametrize(
        "card_id", ["../etc/passwd", "'; DROP TABLE cards; --", "%00", "x" * 300]
    )
    async def test_a_hostile_card_id_is_a_clean_404(
        self, preview_client, card_id
    ):
        response = await preview_client.get(
            f"/api/cards/{card_id}/spec-context"
        )
        assert response.status_code in (404, 422), response.text
