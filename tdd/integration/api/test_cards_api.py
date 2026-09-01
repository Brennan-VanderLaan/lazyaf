"""
Integration tests for Cards API endpoints.

These tests verify the full request/response cycle for card management,
including status transitions and card lifecycle operations.
"""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, update as sql_update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

# Add backend and tdd to path for imports
backend_path = Path(__file__).parent.parent.parent.parent / "backend"
tdd_path = Path(__file__).parent.parent.parent.parent / "tdd"
sys.path.insert(0, str(backend_path))
sys.path.insert(0, str(tdd_path))

from app.database import get_db
from app.main import app
from app.models import Card, Job, PipelineRun, Repo, StepRun, TestRef, TestRun

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
    """Create an ingested repo for card lifecycle tests that require starting jobs.

    Seeds a REAL commit on the default branch. `POST /start` refuses a repo
    whose default branch does not exist, because agent work branches FROM it
    and the workspace clones it - so a repo with no such branch used to be
    accepted, dirty the card, and fail seconds later inside workspace
    population. An ingested-but-empty repo is a legitimate state, it is just
    not one a card can start on, so a fixture for STARTING cards has to look
    like a repo somebody pushed to.
    """
    response = await client.post(
        "/api/repos/ingest",
        json=repo_ingest_payload(name="IngestedCardTestRepo"),
    )
    body = response.json()
    # The ingest response does not carry default_branch; read it back
    # rather than assuming "main", which is the exact assumption that
    # produced the bug this fixture now guards against.
    default_branch = (await client.get(f"/api/repos/{body['id']}")).json()["default_branch"]
    seed_branch(body["id"], default_branch, path="README.md", content=b"seed\n")
    return body


# ---------------------------------------------------------------------------
# Staging a lifecycle state (12.7 / QA T2)
#
# PATCH can no longer fabricate `in_progress` or `done` - that IS the fix
# (see MANUAL_STATUSES in app/routers/cards.py). A test precondition must
# never be built out of the guard it is testing, so states are staged
# through the ORM and through the real git server instead.
# ---------------------------------------------------------------------------


async def stage_card(db_session, card_id, **columns):
    """Put a card into a given state without going through the API."""
    await db_session.execute(
        sql_update(Card).where(Card.id == card_id).values(**columns)
    )
    await db_session.commit()


def seed_branch(repo_id, branch, *, parent=None, path="work.txt", content=b"agent\n"):
    """Put a REAL commit on `branch` in the internal git server.

    Real dulwich objects, so `approve` runs the real merge instead of a
    mocked one: a card that reaches `done` in these tests reached it by
    merging something that existed.
    """
    from dulwich.objects import Blob, Commit, Tree

    from app.services.git_server import git_repo_manager

    repo = git_repo_manager.get_repo(repo_id)
    assert repo is not None, f"repo {repo_id} is not on the internal git server"

    blob = Blob.from_string(content)
    tree = Tree()
    tree.add(path.encode(), 0o100644, blob.id)
    commit = Commit()
    commit.tree = tree.id
    commit.author = commit.committer = b"LazyAF QA <qa@lazyaf.test>"
    commit.commit_time = commit.author_time = 1756000000
    commit.commit_timezone = commit.author_timezone = 0
    commit.encoding = b"UTF-8"
    commit.message = f"work on {branch}".encode()
    if parent:
        commit.parents = [parent.encode("ascii")]

    repo.object_store.add_object(blob)
    repo.object_store.add_object(tree)
    repo.object_store.add_object(commit)
    repo.refs[f"refs/heads/{branch}".encode()] = commit.id
    return commit.id.decode("ascii")


async def card_in_review(client, db_session, repo_id, *, title="Reviewed work"):
    """A card in the state a finished agent run leaves behind.

    `in_review`, with a branch that really exists on the internal git server
    and is a fast-forward ahead of the repo's default branch - i.e. a card
    there is something to approve.
    """
    response = await client.post(
        f"/api/repos/{repo_id}/cards", json=card_create_payload(title=title)
    )
    assert response.status_code == 201, response.text
    card_id = response.json()["id"]

    repo = await db_session.get(Repo, repo_id)
    base = seed_branch(repo_id, repo.default_branch, path="README.md", content=b"base\n")
    branch = f"lazyaf/{card_id[:8]}"
    seed_branch(repo_id, branch, parent=base)

    await stage_card(db_session, card_id, status="in_review", branch_name=branch)
    return card_id


@pytest_asyncio.fixture
async def concurrent_client(async_engine):
    """A client that gives every request its OWN database session.

    The shared-session `client` fixture cannot express a race: two requests
    on one AsyncSession interleave inside one transaction, which is not what
    the running stack does. This one hands each request a fresh session on
    the same file-backed engine, so N simultaneous POSTs contend for the
    SQLite write lock exactly as they do in production - which is the only
    way to test that the card claim is atomic (QA finding T6).
    """
    factory = async_sessionmaker(
        async_engine, class_=AsyncSession, expire_on_commit=False
    )

    async def override_get_db():
        async with factory() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


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
        """Updates card status (a move the board is allowed to make by hand).

        `in_progress` and `done` are NOT such moves any more - they belong to
        start/approve. See TestPatchStatusCannotBypassTheLifecycle.
        """
        create_response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(),
        )
        card_id = create_response.json()["id"]

        response = await client.patch(
            f"/api/cards/{card_id}",
            json={"status": "in_review"},
        )
        assert_status_code(response, 200)
        assert response.json()["status"] == "in_review"

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

    async def test_start_card(self, client, ingested_repo, clean_runner_registry):
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

    async def test_start_card_already_started(self, client, ingested_repo, clean_runner_registry):
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

    async def test_approve_card(self, client, ingested_repo, db_session):
        """POST /api/cards/{id}/approve MERGES the branch, then marks it done."""
        card_id = await card_in_review(client, db_session, ingested_repo["id"])

        response = await client.post(
            f"/api/cards/{card_id}/approve",
            json={"target_branch": None},
        )
        assert_status_code(response, 200)
        result = response.json()
        assert result["card"]["status"] == "done"
        assert result["merge_result"]["success"] is True, result["merge_result"]

        repo = await db_session.get(Repo, ingested_repo["id"])
        from app.services.git_server import git_repo_manager

        assert git_repo_manager.get_branch_commit(
            repo.id, repo.default_branch
        ) == git_repo_manager.get_branch_commit(
            repo.id, result["card"]["branch_name"]
        ), "the card is 'done' but its branch never landed on the default branch"

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

    async def test_retry_failed_card(self, client, ingested_repo, clean_runner_registry):
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

    async def test_retry_in_review_card(self, client, ingested_repo, clean_runner_registry):
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


async def legacy_deprecated_card(
    db_session, repo_id, step_type, *, title, step_config=None
):
    """A script/docker card as it exists in an OLD database.

    Creating one through the API is refused now
    (`_reject_deprecated_step_type_on_create`), so the only way one exists is
    to predate that guard. Staged through the ORM for the same reason
    `stage_card` exists: a test precondition must never be built out of the
    guard it is testing.
    """
    card = Card(
        id=str(uuid4()),
        repo_id=repo_id,
        title=title,
        description="",
        status="todo",
        step_type=step_type,
        step_config=json.dumps(
            step_config or {"command": "echo hi", "image": "alpine:3"}
        ),
    )
    db_session.add(card)
    await db_session.commit()
    return card.id


class TestScriptDockerCardsRejected:
    """12.4 fallout: script/docker cards have no execution path.

    Phase 12.4 deleted script/docker execution from every runner entrypoint,
    and cards run ONLY on the runner queue (LocalExecutor is driven per
    PipelineRun/StepRun, which a card does not have). Starting one used to
    enqueue a job that a runner picked up and instantly rejected - the card
    flipped in_progress and then failed with a message about a routing bug.

    The contract now, in two layers:

      * CREATE refuses with a 422 naming the replacement - the board can no
        longer be given a card that Start is guaranteed to reject (12.7);
      * START/RETRY still refuse with a 400, because cards created before
        that guard exist and must not re-enter the in_progress -> failed loop.
    """

    @pytest.mark.parametrize(
        "step_type,step_config",
        [
            ("script", {"command": "pytest -q"}),
            ("docker", {"image": "python:3.12", "command": "pytest -q"}),
        ],
    )
    async def test_start_rejects_script_and_docker_cards(
        self,
        client,
        db_session,
        ingested_repo,
        clean_runner_registry,
        step_type,
        step_config,
    ):
        card_id = await legacy_deprecated_card(
            db_session,
            ingested_repo["id"],
            step_type,
            title=f"A {step_type} card",
            step_config=step_config,
        )

        response = await client.post(f"/api/cards/{card_id}/start")

        assert_status_code(response, 400)
        detail = response.json()["detail"]
        assert step_type in detail
        assert "12.4" in detail
        # Points at the supported alternative rather than just saying "no".
        assert "pipeline" in detail.lower()

    @pytest.mark.parametrize("step_type", ["script", "docker"])
    async def test_rejected_card_stays_in_todo(
        self, client, db_session, ingested_repo, clean_runner_registry, step_type
    ):
        """No silent in_progress -> failed loop: the card never moves."""
        card_id = await legacy_deprecated_card(
            db_session,
            ingested_repo["id"],
            step_type,
            title=f"Untouched {step_type} card",
        )

        await client.post(f"/api/cards/{card_id}/start")

        card = (await client.get(f"/api/cards/{card_id}")).json()
        assert card["status"] == "todo"
        assert card["job_id"] is None
        assert card["branch_name"] is None

    @pytest.mark.parametrize("step_type", ["script", "docker"])
    async def test_retry_rejects_script_and_docker_cards(
        self, client, db_session, ingested_repo, clean_runner_registry, step_type
    ):
        """Retry closes the same loop - a failed script card cannot be
        re-enqueued into the same rejection."""
        card_id = await legacy_deprecated_card(
            db_session,
            ingested_repo["id"],
            step_type,
            title=f"Failed {step_type} card",
        )
        await client.patch(f"/api/cards/{card_id}", json={"status": "failed"})

        response = await client.post(f"/api/cards/{card_id}/retry")

        assert_status_code(response, 400)
        assert step_type in response.json()["detail"]

        card = (await client.get(f"/api/cards/{card_id}")).json()
        assert card["status"] == "failed"

    async def test_agent_cards_still_start(
        self, client, ingested_repo, clean_runner_registry
    ):
        """The guard is narrow: agent cards are unaffected."""
        payload = card_create_payload(title="An agent card")
        payload["step_type"] = "agent"
        card_id = (
            await client.post(
                f"/api/repos/{ingested_repo['id']}/cards", json=payload
            )
        ).json()["id"]

        response = await client.post(f"/api/cards/{card_id}/start")

        assert_status_code(response, 200)
        assert response.json()["status"] == "in_progress"

    @pytest.mark.parametrize("step_type", ["script", "docker"])
    async def test_create_rejects_script_and_docker_cards(
        self, client, repo, step_type
    ):
        """12.7: refuse at CREATION, not three clicks later at Start.

        The New Card form used to offer "Shell Script" / "Docker Container",
        the API took them with a 201, and the card only turned out to be
        unrunnable when the user pressed Start. The product invited the
        mistake; now the edge refuses it (R1).
        """
        payload = card_create_payload(title=f"A {step_type} card")
        payload["step_type"] = step_type
        payload["step_config"] = {"command": "make test", "image": "alpine:3"}

        response = await client.post(
            f"/api/repos/{repo['id']}/cards", json=payload
        )

        assert_status_code(response, 422)
        detail = response.json()["detail"]
        assert step_type in detail
        # Names the REPLACEMENT, not just "no".
        assert "step_type='agent'" in detail
        assert "pipeline" in detail.lower()

    @pytest.mark.parametrize("step_type", ["script", "docker"])
    async def test_rejected_create_writes_no_card(self, client, repo, step_type):
        """The refusal is the whole outcome: no half-made card on the board."""
        payload = card_create_payload(title=f"A {step_type} card")
        payload["step_type"] = step_type

        await client.post(f"/api/repos/{repo['id']}/cards", json=payload)

        cards = (await client.get(f"/api/repos/{repo['id']}/cards")).json()
        assert cards == []

    async def test_agent_cards_are_still_creatable(self, client, repo):
        """The guard is narrow: the supported step type is untouched."""
        response = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="An agent card"),
        )

        assert_status_code(response, 201)
        assert response.json()["step_type"] == "agent"

    @pytest.mark.parametrize("step_type", ["script", "docker"])
    async def test_legacy_script_docker_cards_still_round_trip(
        self, client, db_session, repo, step_type
    ):
        """Deprecated, not deleted: a card created before the guard keeps
        reading back and stays editable, so a user can see it and convert it."""
        card_id = await legacy_deprecated_card(
            db_session, repo["id"], step_type, title=f"Legacy {step_type} card"
        )

        read = await client.get(f"/api/cards/{card_id}")
        assert_status_code(read, 200)
        assert read.json()["step_type"] == step_type

        # Editing it (including converting it to agent) is not refused.
        renamed = await client.patch(
            f"/api/cards/{card_id}", json={"title": "Renamed legacy card"}
        )
        assert_status_code(renamed, 200)
        converted = await client.patch(
            f"/api/cards/{card_id}", json={"step_type": "agent"}
        )
        assert_status_code(converted, 200)
        assert converted.json()["step_type"] == "agent"


# =============================================================================
# Phase 12.5: the ad-hoc card-work RUN behind a card
#
# Since 12.5 starting a card creates an ephemeral single-agent-step
# PipelineRun and the Job row is its twin (app/services/agent_run.py). Every
# test below is about the RUN - the thing that holds a container and spends
# provider budget - rather than about the Job row that reports on it, because
# each of these defects shipped past a test that only checked the report.
# =============================================================================


class ParkedStubExecutor:
    """Control-mode LocalExecutor stand-in that parks mid-step.

    Agent steps REQUIRE a control-layer image (``_prepare_control_mode``
    refuses anything else), so unlike the T1 stub in tdd/conftest.py this one
    declares the capability - which means the backend really mints a
    StepExecution row and a step JWT, and the test gets to play the container
    against it (``act_as_container`` below). It then parks until the test
    releases it, which is what makes cancellation observable.
    """

    def __init__(self):
        self.calls = []
        self.dispatched = asyncio.Event()
        self.release = asyncio.Event()
        self.cancelled_keys = []
        # Real containers do not always die the instant they are killed.
        # Setting this False keeps the step task parked after the cancel, so
        # assertions about what CANCEL wrote are not racing a straggler that
        # is busy overwriting it.
        self.release_on_cancel = True

    async def image_supports_control_layer(self, image):
        return True

    async def find_missing_images(self, images):
        return []

    async def execute_step(self, step_config, execution_context):
        self.calls.append((dict(step_config), dict(execution_context)))
        yield {"type": "status", "status": "preparing"}
        yield {"type": "status", "status": "running"}
        self.dispatched.set()
        await self.release.wait()
        yield {
            "type": "result",
            "status": "completed",
            "exit_code": 0,
            "error": None,
            "log_tail": [],
        }

    async def cancel_step(self, execution_key):
        self.cancelled_keys.append(execution_key)
        if self.release_on_cancel:
            self.release.set()
        return True

    async def cancel_all(self):
        self.release.set()

    def reset(self):
        self.calls.clear()

    @property
    def context(self):
        assert self.calls, "the step was never dispatched to the executor"
        return self.calls[-1][1]


class MissingImageStubExecutor(ParkedStubExecutor):
    """Preflight fails: start_pipeline completes the run before returning."""

    async def find_missing_images(self, images):
        return sorted(images)

    async def execute_step(self, step_config, execution_context):
        raise AssertionError("preflight failed - nothing should dispatch")
        yield  # pragma: no cover - keeps this an async generator


def _install_local_executor(stub):
    from app.services.pipeline_executor import pipeline_executor

    previous = pipeline_executor._local_executor
    pipeline_executor._local_executor = stub
    try:
        yield stub
    finally:
        stub.release.set()
        pipeline_executor._local_executor = previous


@pytest.fixture
def parked_executor():
    yield from _install_local_executor(ParkedStubExecutor())


@pytest.fixture
def missing_image_executor():
    yield from _install_local_executor(MissingImageStubExecutor())


@pytest.fixture
def trigger_spy(monkeypatch):
    """Record every card_complete gate call without firing real pipelines."""
    from app.services.trigger_service import trigger_service

    calls = []

    async def spy(db, card, old_status, new_status):
        calls.append((card.id, old_status, new_status))

    monkeypatch.setattr(trigger_service, "on_card_status_change", spy)
    return calls


async def settle(cycles=40):
    """Let the dispatched step task run to wherever it is going."""
    for _ in range(cycles):
        await asyncio.sleep(0.01)


AGENT_LOG_LINE = "[agent] rewriting the module\n"


async def _fresh(db_session):
    """Drop this session's snapshot so the next read sees other sessions.

    The test client and the step task hold different sessions on the same
    engine; the client's is ``expire_on_commit=False`` and, once it has read
    anything, sits inside a transaction whose SQLite snapshot predates the
    step task's commits. Rolling back ends that transaction and expires the
    identity map, so the next GET is a real read of committed state rather
    than a replay of what this session saw before the run finished.
    """
    await db_session.rollback()
    db_session.expire_all()


async def read_card(client, db_session, card_id):
    """GET a card, seeing what the STEP TASK committed."""
    await _fresh(db_session)
    response = await client.get(f"/api/cards/{card_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def read_job(client, db_session, job_id):
    """GET a job, seeing what the step task committed (see read_card)."""
    await _fresh(db_session)
    response = await client.get(f"/api/jobs/{job_id}")
    assert response.status_code == 200, response.text
    return response.json()


async def await_card_status(client, db_session, card_id, expected, timeout=10.0):
    """Poll until the card reaches `expected`, then return it.

    The run completes on a background step task, so the card's terminal
    status arrives whenever that task gets there. Polling keeps the
    assertion about the OUTCOME instead of about how many event-loop turns
    the settle helper happened to buy.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    card = await read_card(client, db_session, card_id)
    while card["status"] != expected and asyncio.get_event_loop().time() < deadline:
        await asyncio.sleep(0.02)
        card = await read_card(client, db_session, card_id)
    assert card["status"] == expected, (
        f"card stayed {card['status']!r}; expected {expected!r} within {timeout}s"
    )
    return card


async def await_card_gate(trigger_spy, expected_status, timeout=5.0):
    """Wait for the card_complete gate to fire for `expected_status`.

    The card's status is COMMITTED before the gate is awaited, so a test that
    polls the status and then reads the spy is racing the last two lines of
    the completion handler.
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while (
        not any(call[2] == expected_status for call in trigger_spy)
        and asyncio.get_event_loop().time() < deadline
    ):
        await asyncio.sleep(0.02)
    assert [call[2] for call in trigger_spy] == [expected_status], (
        f"card_complete gate calls were {trigger_spy!r}; expected exactly one "
        f"for {expected_status!r}"
    )


async def act_as_container(client, executor, log_line=AGENT_LOG_LINE):
    """Do what the control runtime in the container would do.

    Reports "running" and streams one log line, both through the REAL
    /api/steps routes with the REAL step JWT the backend minted at dispatch.
    Without the status report, the executor's finish-time reconciliation
    fails the step for never having reported - correctly, since an image with
    no working /control runtime must never read green.
    """
    context = executor.context
    headers = {"Authorization": f"Bearer {context['step_auth_token']}"}
    step_id = context["step_execution_id"]

    response = await client.post(
        f"/api/steps/{step_id}/status", headers=headers, json={"status": "running"}
    )
    assert response.status_code == 200, response.text

    response = await client.post(
        f"/api/steps/{step_id}/logs",
        headers=headers,
        json={"lines": [{"content": log_line}]},
    )
    assert response.status_code == 200, response.text
    return step_id


async def await_dispatch(executor, timeout=10.0):
    """Wait for the step to reach the executor, loudly.

    A bare ``Event.wait()`` on a step that never dispatches hangs the whole
    suite with no message; this fails the one test that broke.
    """
    try:
        await asyncio.wait_for(executor.dispatched.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        raise AssertionError(
            f"the card's agent step never reached the executor within "
            f"{timeout}s (calls={executor.calls})"
        )


async def start_agent_card(client, repo_id, title="Agent work"):
    response = await client.post(
        f"/api/repos/{repo_id}/cards",
        json={
            "title": title,
            "description": "Do the thing in the title.",
            "runner_type": "mock",
            "step_type": "agent",
        },
    )
    assert response.status_code == 201, response.text
    card = response.json()
    started = await client.post(f"/api/cards/{card['id']}/start")
    assert started.status_code == 200, started.text
    return started.json()


async def adhoc_run_for(db_session, card_id):
    result = await db_session.execute(
        select(PipelineRun).where(PipelineRun.trigger_ref == card_id)
    )
    return result.scalars().one()


async def step_run_for(db_session, run_id):
    result = await db_session.execute(
        select(StepRun).where(StepRun.pipeline_run_id == run_id)
    )
    return result.scalars().first()


async def seed_test_results(
    db_session, repo_id, run_id, step_run_id, *, passed=(), failed=(), skipped=()
):
    """Write the TestRun rows the 12.2.6 tie-back would have written.

    This is the persisted evidence the card test gate reads - whether it was
    produced by the agent step itself or by a post-agent verification step of
    the same run, the rows look identical.
    """
    for status, test_ids in (
        ("passed", passed),
        ("failed", failed),
        ("skipped", skipped),
    ):
        for test_id in test_ids:
            ref = TestRef(id=str(uuid4()), lazyaf_test_id=test_id, repo_id=repo_id)
            db_session.add(ref)
            db_session.add(
                TestRun(
                    id=str(uuid4()),
                    test_ref_id=ref.id,
                    pipeline_run_id=run_id,
                    step_run_id=step_run_id,
                    commit_sha="",
                    status=status,
                )
            )
    await db_session.commit()


class TestCancelJobCancelsTheRun:
    """POST /api/jobs/{id}/cancel used to be a TODO with a status write.

    It flipped the Job row to failed and left the agent container running - a
    cancel that cancels nothing, with the UI reporting it stopped. Nobody
    watches a job that says it is over, so the container ran on to its
    1800-second deadline spending real provider budget.
    """

    async def test_cancel_reaches_the_executor_and_cancels_the_run(
        self, client, ingested_repo, db_session, parked_executor
    ):
        # Keep the "container" alive past the kill so these assertions are
        # about what CANCEL wrote, not about whether the straggler step task
        # got there first.
        parked_executor.release_on_cancel = False

        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        run = await adhoc_run_for(db_session, card["id"])

        response = await client.post(f"/api/jobs/{card['job_id']}/cancel")
        assert response.status_code == 200, response.text

        assert parked_executor.cancelled_keys, (
            "cancelling a card job never reached the executor - the agent "
            "container is still running"
        )
        await db_session.refresh(run)
        assert run.status == "cancelled"

        step_run = await step_run_for(db_session, run.id)
        await db_session.refresh(step_run)
        assert step_run.status == "cancelled"

    async def test_cancel_lands_the_card_failed_with_a_terminal_job(
        self, client, ingested_repo, db_session, parked_executor
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)

        response = await client.post(f"/api/jobs/{card['job_id']}/cancel")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "failed"
        assert response.json()["error"] == "Cancelled by user"

        card_after = await client.get(f"/api/cards/{card['id']}")
        assert card_after.json()["status"] == "failed", (
            "a cancelled card must not sit in in_progress forever - nothing "
            "is left to land it, and retry only accepts failed/in_review"
        )

    async def test_a_cancelled_run_never_walks_the_card_into_in_review(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        """The full sequence: cancel, then let the step task finish anyway."""
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)

        await client.post(f"/api/jobs/{card['job_id']}/cancel")
        parked_executor.release.set()  # the container "exits 0" after the kill
        await settle()

        card_after = await read_card(client, db_session, card["id"])
        assert card_after["status"] == "failed"

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "failed"
        assert job_after["error"] == "Cancelled by user"

        # Give a gate call that should NOT happen time to happen: the card
        # status commits before the gate is awaited, so reading the spy the
        # instant the status lands would pass whether or not it fires.
        await settle()
        assert trigger_spy == [], (
            "a cancelled run fired the card_complete gate - that is the "
            "self-triggering loop with an extra step in front of it"
        )

    async def test_on_run_complete_refuses_to_succeed_a_cancelled_run(
        self, client, ingested_repo, db_session, trigger_spy
    ):
        """Defence in depth, on rows alone - no executor, no race.

        The run column is asserted directly here because in the live path a
        straggler step task can OVERWRITE ``cancelled`` with its own verdict
        before this hook runs (its session's snapshot predates the cancel
        commit). That is why the card is guarded by its own state as well;
        this test pins the other half.
        """
        from app.models import Card, Pipeline, RunStatus
        from app.services import agent_run

        card_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json={"title": "Cancelled work", "runner_type": "mock",
                  "step_type": "agent"},
        )
        card_id = card_response.json()["id"]
        card = await db_session.get(Card, card_id)

        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=ingested_repo["id"],
            name=agent_run.adhoc_pipeline_name("card_work", card_id),
            steps="[]",
            triggers="[]",
        )
        job = Job(id=str(uuid4()), card_id=card_id, status="running")
        db_session.add_all([pipeline, job])
        card.status = "in_progress"
        card.job_id = job.id
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.CANCELLED.value,
            trigger_type=agent_run.TRIGGER_CARD_WORK,
            trigger_ref=card_id,
        )
        db_session.add(run)
        await db_session.commit()

        await agent_run.on_run_complete(db_session, run, success=True)

        await db_session.refresh(card)
        assert card.status == "failed", (
            "a CANCELLED run reported success and the card was promoted"
        )
        await db_session.refresh(job)
        assert job.status == "failed"
        assert trigger_spy == []

    async def test_a_pipeline_step_job_does_not_take_its_pipeline_down(
        self, client, ingested_repo, db_session, monkeypatch
    ):
        """Only an AD-HOC card-work run is owned by one job.

        A job that is a step of a real multi-step pipeline does not own that
        pipeline; cancelling one card's job must not cancel the run every
        other step of it is still using.
        """
        from app.models import Card, Pipeline, RunStatus
        from app.services.pipeline_executor import pipeline_executor

        cancelled = []

        async def spy(db, run):
            cancelled.append(run.id)
            return run

        monkeypatch.setattr(pipeline_executor, "cancel_run", spy)

        card_response = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json={"title": "Step card", "runner_type": "mock",
                  "step_type": "agent"},
        )
        card_id = card_response.json()["id"]
        card = await db_session.get(Card, card_id)

        pipeline = Pipeline(
            id=str(uuid4()),
            repo_id=ingested_repo["id"],
            name="a-real-pipeline",
            steps="[]",
            triggers="[]",
        )
        run = PipelineRun(
            id=str(uuid4()),
            pipeline_id=pipeline.id,
            status=RunStatus.RUNNING.value,
            trigger_type="manual",
        )
        step_run = StepRun(
            id=str(uuid4()),
            pipeline_run_id=run.id,
            step_index=0,
            step_name="Step",
            status=RunStatus.RUNNING.value,
        )
        job = Job(
            id=str(uuid4()),
            card_id=card_id,
            status="running",
            step_run_id=step_run.id,
        )
        card.status = "in_progress"
        card.job_id = job.id
        db_session.add_all([pipeline, run, step_run, job])
        await db_session.commit()

        response = await client.post(f"/api/jobs/{job.id}/cancel")
        assert response.status_code == 200, response.text
        assert cancelled == [], (
            "cancelling one step card's job cancelled the whole pipeline run"
        )

        await db_session.refresh(run)
        assert run.status == "running"

    async def test_a_cancel_that_cannot_cancel_surfaces(
        self, client, ingested_repo, db_session, parked_executor, monkeypatch
    ):
        from app.services.pipeline_executor import pipeline_executor

        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)

        async def boom(db, run):
            raise RuntimeError("docker daemon went away")

        monkeypatch.setattr(pipeline_executor, "cancel_run", boom)

        response = await client.post(f"/api/jobs/{card['job_id']}/cancel")
        assert response.status_code == 503, response.text

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "running", (
            "a failed cancel must leave the job live so it can be retried - "
            "reporting 'failed' hides a container that is still running"
        )


class TestSynchronousStartFailure:
    """``start_pipeline`` does not always return a RUNNING run.

    Image preflight fails INLINE - _complete_pipeline -> on_run_complete
    lands the card and the Job before start_adhoc_agent_run has returned - so
    a caller that writes "running" afterwards resurrects a run that already
    failed and strands the card in in_progress with nothing left to land it.
    """

    async def test_card_ends_failed_with_a_terminal_job(
        self, client, ingested_repo, db_session, missing_image_executor
    ):
        card = await start_agent_card(client, ingested_repo["id"])

        card_after = await await_card_status(
            client, db_session, card["id"], "failed"
        )
        assert card_after["status"] == "failed"

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "failed", (
            "the job was flipped back to running over its own terminal status"
        )
        assert job_after["completed_at"] is not None

        run = await adhoc_run_for(db_session, card["id"])
        assert run.status == "failed"


class TestCardOutcomeRespectsTests:
    """A card whose suite is red must not be offered for merge.

    The legacy runner path had this gate (routers/jobs.py: "if tests were run
    and failed, mark card as failed instead of in_review"). Losing it on the
    12.5 path does two things at once: it offers red work for review, and -
    because reaching in_review is what fires the card_complete triggers - it
    hands a red branch to the verification pipeline as if it were done.
    """

    async def _finish_run(self, db_session, card, executor, **results):
        run = await adhoc_run_for(db_session, card["id"])
        step_run = await step_run_for(db_session, run.id)
        await seed_test_results(
            db_session,
            card["repo_id"],
            run.id,
            step_run.id if step_run else None,
            **results,
        )
        executor.release.set()
        await settle()
        return run

    async def test_red_suite_holds_the_card_out_of_review(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        await self._finish_run(
            db_session,
            card,
            parked_executor,
            passed=("suite::ok",),
            failed=("suite::regression",),
        )

        card_after = await await_card_status(
            client, db_session, card["id"], "failed"
        )
        assert card_after["status"] != "in_review", (
            "the agent step succeeded but the repo suite is red - the card "
            "must not reach in_review"
        )
        # Give a gate call that should NOT happen time to happen: the card
        # status commits before the gate is awaited, so reading the spy the
        # instant the status lands would pass whether or not it fires.
        await settle()
        assert trigger_spy == [], (
            "a red card fired the card_complete gate - the verification "
            "pipeline was handed a red branch as if it were done"
        )

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "failed"
        assert job_after["tests_run"] is True
        assert job_after["tests_passed"] is False
        assert job_after["test_pass_count"] == 1
        assert job_after["test_fail_count"] == 1

    async def test_a_red_verification_step_of_the_same_run_holds_the_card(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        """The gate is RUN-scoped, not step-scoped.

        The executor's own gate demotes a STEP that reported failing tests
        (``_finish_local_step_locked`` reads TestRun by step_run_id), which
        covers the agent step's own suite run. A post-agent verification step
        reports under its OWN step run, so its red results are invisible to
        the finishing agent step's gate: the run passes and the card would be
        promoted. The card's gate reads every TestRun of the RUN, so it holds
        the card whichever step of the run went red.
        """
        from app.models import RunStatus

        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)

        run = await adhoc_run_for(db_session, card["id"])
        verification = StepRun(
            id=str(uuid4()),
            pipeline_run_id=run.id,
            step_index=1,
            step_name="Verify",
            status=RunStatus.PASSED.value,
            executor="local",
        )
        db_session.add(verification)
        await db_session.commit()
        await seed_test_results(
            db_session,
            card["repo_id"],
            run.id,
            verification.id,
            passed=("suite::ok",),
            failed=("suite::verification-regression",),
        )

        parked_executor.release.set()
        card_after = await await_card_status(
            client, db_session, card["id"], "failed"
        )
        assert card_after["status"] != "in_review"
        # Give a gate call that should NOT happen time to happen: the card
        # status commits before the gate is awaited, so reading the spy the
        # instant the status lands would pass whether or not it fires.
        await settle()
        assert trigger_spy == [], (
            "a card whose verification step went red fired the card_complete "
            "gate"
        )

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["tests_run"] is True
        assert job_after["tests_passed"] is False
        assert job_after["test_fail_count"] == 1

    async def test_green_suite_lands_in_review_with_the_counts(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        await self._finish_run(
            db_session,
            card,
            parked_executor,
            passed=("suite::a", "suite::b"),
            skipped=("suite::c",),
        )

        await await_card_status(client, db_session, card["id"], "in_review")
        await await_card_gate(trigger_spy, "in_review")

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "completed"
        assert job_after["tests_run"] is True
        assert job_after["tests_passed"] is True
        assert job_after["test_pass_count"] == 2
        assert job_after["test_skip_count"] == 1
        assert job_after["test_fail_count"] == 0

    async def test_no_manifest_is_neither_a_pass_nor_a_fail(
        self, client, ingested_repo, db_session, parked_executor, trigger_spy
    ):
        """No evidence gates nothing, but never claims a green suite."""
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        parked_executor.release.set()
        await settle()

        await await_card_status(client, db_session, card["id"], "in_review")

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["tests_run"] is False
        assert job_after["tests_passed"] is None


class TestCardJobLogsAreNotDark:
    """The card modal reads JOB logs; the 12.5 path writes StepRun logs.

    Until 12.6 rebuilds that panel, a card work run that leaves Job.logs
    empty means the user sees a blank pane for the whole run and for its
    entire history afterwards.
    """

    async def test_a_completed_run_leaves_non_empty_job_logs(
        self, client, ingested_repo, db_session, parked_executor
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        parked_executor.release.set()
        await await_card_status(client, db_session, card["id"], "in_review")

        response = await client.get(f"/api/jobs/{card['job_id']}/logs")
        assert response.status_code == 200, response.text
        logs = response.json()["logs"]
        assert logs, "a completed card work run left the card log pane blank"
        assert "rewriting the module" in logs

        job = await db_session.get(Job, card["job_id"])
        await db_session.refresh(job)
        assert job.logs, "Job.logs was never mirrored off the run"

    async def test_logs_are_served_live_while_the_run_is_still_going(
        self, client, ingested_repo, db_session, parked_executor
    ):
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        await act_as_container(client, parked_executor)
        await settle(cycles=10)  # let the log flush land

        response = await client.get(f"/api/jobs/{card['job_id']}/logs")
        assert response.status_code == 200, response.text
        assert "rewriting the module" in response.json()["logs"], (
            "the card modal polls this endpoint every 3s while a job runs; "
            "before completion Job.logs is empty, so it has to fall back to "
            "the StepRun the job is linked to"
        )


# =============================================================================
# The card lifecycle guards (QA findings T2 + T6)
#
# Every gesture that changes a card's status now validates the card's CURRENT
# status and refuses with a 400 that names it, and every one of them lands as
# a conditional UPDATE so two of them cannot both win. These tests are the
# reproductions from the adversarial QA pass, turned into locks.
# =============================================================================


class TestApproveRequiresSomethingToMerge:
    """`approve` used to have no guard of any kind.

    A card in `todo` - never started, no branch, no job, no diff - was moved
    straight to `done` with 200 OK. Nothing was merged, no error was shown,
    and the card_complete triggers fired on work that never happened. That is
    the one defect that puts a WRONG FACT on the board: work that did not
    happen, displayed exactly like work that did.
    """

    async def test_approve_refuses_a_card_that_never_ran(self, client, repo):
        create = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Never ran"),
        )
        card_id = create.json()["id"]

        response = await client.post(f"/api/cards/{card_id}/approve", json={})

        assert_status_code(response, 400)
        detail = response.json()["detail"]
        assert "todo" in detail and "in_review" in detail, detail

        after = await client.get(f"/api/cards/{card_id}")
        assert after.json()["status"] == "todo", (
            "a card that never ran reached 'done' in one click"
        )

    async def test_approve_refuses_an_in_review_card_with_no_branch(
        self, client, repo, db_session
    ):
        """The board's other door into this: drag TO DO -> IN REVIEW, Approve.

        `in_review` with no branch is exactly what a manual move produces,
        and approve's old `if card.branch_name and repo.is_ingested:` skipped
        the whole merge for it and marked the card done anyway.
        """
        create = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Dragged into review"),
        )
        card_id = create.json()["id"]
        await stage_card(db_session, card_id, status="in_review")

        response = await client.post(f"/api/cards/{card_id}/approve", json={})

        assert_status_code(response, 400)
        assert "no branch" in response.json()["detail"]
        after = await client.get(f"/api/cards/{card_id}")
        assert after.json()["status"] == "in_review"

    async def test_approve_refuses_when_the_repo_is_not_ingested(
        self, client, repo, db_session
    ):
        """No git storage, no merge - and therefore no 'done' (R1)."""
        create = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Un-ingested"),
        )
        card_id = create.json()["id"]
        await stage_card(
            db_session, card_id, status="in_review", branch_name="lazyaf/abc12345"
        )

        response = await client.post(f"/api/cards/{card_id}/approve", json={})

        assert_status_code(response, 400)
        assert "not ingested" in response.json()["detail"]

    async def test_a_second_approve_is_refused_and_fires_no_second_gate(
        self, client, ingested_repo, db_session, trigger_spy
    ):
        """Double-clicking Approve used to start two verification runs."""
        card_id = await card_in_review(client, db_session, ingested_repo["id"])

        first = await client.post(f"/api/cards/{card_id}/approve", json={})
        assert_status_code(first, 200)

        second = await client.post(f"/api/cards/{card_id}/approve", json={})
        assert_status_code(second, 400)
        assert "done" in second.json()["detail"]

        assert [call[2] for call in trigger_spy] == ["done"], (
            f"the card_complete gate fired {len(trigger_spy)} times for one "
            "approved card"
        )

    async def test_a_failed_merge_leaves_the_card_in_review(
        self, client, ingested_repo, db_session
    ):
        """The claim is rolled back with the transaction that failed to merge."""
        create = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="Branch that vanished"),
        )
        card_id = create.json()["id"]
        repo = await db_session.get(Repo, ingested_repo["id"])
        seed_branch(repo.id, repo.default_branch, path="README.md")
        # A branch name that exists on the CARD but not in the git server.
        await stage_card(
            db_session, card_id, status="in_review", branch_name="lazyaf/deadbeef"
        )

        response = await client.post(f"/api/cards/{card_id}/approve", json={})

        assert_status_code(response, 400)
        assert "Merge failed" in response.json()["detail"]
        after = await client.get(f"/api/cards/{card_id}")
        assert after.json()["status"] == "in_review", (
            "a merge that failed still moved the card to done"
        )


class TestRejectStopsTheWork:
    """`reject` used to unwind the card and leave the agent running.

    It sent an in_progress card back to `todo` and nulled `branch_name`
    without cancelling anything: the container kept committing to a branch no
    card pointed at, the Job sat at `running` forever (the run's completion
    handler refuses to land a card that has left in_progress), and `start`
    accepted the card again - two agents, one repo.
    """

    async def test_reject_refuses_a_card_with_nothing_to_reject(self, client, repo):
        create = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Nothing to reject"),
        )
        card_id = create.json()["id"]

        response = await client.post(f"/api/cards/{card_id}/reject")

        assert_status_code(response, 400)
        assert "todo" in response.json()["detail"]

    async def test_reject_refuses_a_done_card(self, client, repo, db_session):
        create = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Already merged"),
        )
        card_id = create.json()["id"]
        await stage_card(db_session, card_id, status="done", branch_name="lazyaf/x")

        response = await client.post(f"/api/cards/{card_id}/reject")

        assert_status_code(response, 400)
        after = await client.get(f"/api/cards/{card_id}")
        assert after.json()["status"] == "done"
        assert after.json()["branch_name"] == "lazyaf/x", (
            "reject cleared the branch of a card it refused to reject"
        )

    async def test_reject_of_a_running_card_cancels_the_run(
        self, client, ingested_repo, db_session, parked_executor
    ):
        parked_executor.release_on_cancel = False
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        run = await adhoc_run_for(db_session, card["id"])

        response = await client.post(f"/api/cards/{card['id']}/reject")
        assert response.status_code == 200, response.text
        assert response.json()["status"] == "todo"
        assert response.json()["branch_name"] is None

        assert parked_executor.cancelled_keys, (
            "rejecting a running card never reached the executor - the agent "
            "container is still running against a branch nothing points at"
        )
        await db_session.refresh(run)
        assert run.status == "cancelled"

    async def test_reject_lands_the_job_it_cancelled(
        self, client, ingested_repo, db_session, parked_executor
    ):
        """The card modal polls this Job every 3 seconds."""
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)

        response = await client.post(f"/api/cards/{card['id']}/reject")
        assert response.status_code == 200, response.text

        job_after = await read_job(client, db_session, card["job_id"])
        assert job_after["status"] == "failed", (
            "the Job of a rejected card is stuck at 'running' - the modal "
            "spinner never resolves"
        )
        assert job_after["completed_at"] is not None
        assert "rejected" in job_after["error"]

    async def test_a_rejected_card_does_not_leave_a_second_agent_running(
        self, client, ingested_repo, db_session, parked_executor
    ):
        """Reject then start: exactly one live run, ever."""
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)
        first_run = await adhoc_run_for(db_session, card["id"])

        assert (await client.post(f"/api/cards/{card['id']}/reject")).status_code == 200
        await db_session.refresh(first_run)
        assert first_run.status == "cancelled"

        restart = await client.post(f"/api/cards/{card['id']}/start")
        assert restart.status_code == 200, restart.text

        await _fresh(db_session)
        result = await db_session.execute(
            select(PipelineRun).where(PipelineRun.trigger_ref == card["id"])
        )
        live = [
            run
            for run in result.scalars().all()
            if run.status in ("pending", "running")
        ]
        assert len(live) == 1, (
            f"{len(live)} live runs for one card - a rejected card's agent was "
            "left running beside its replacement"
        )


class TestPatchStatusCannotBypassTheLifecycle:
    """PATCH /api/cards/{id} is the kanban board's drag handler.

    Every status it can write, it writes with no side effect. The two it
    cannot write stand for something outside the row - a live agent run
    (`in_progress`) and a merged branch (`done`) - and each has an endpoint
    that does the real work.
    """

    async def test_patch_cannot_mark_a_card_done(self, client, repo, db_session):
        create = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Dragged to done"),
        )
        card_id = create.json()["id"]
        await stage_card(db_session, card_id, status="in_review")

        response = await client.patch(f"/api/cards/{card_id}", json={"status": "done"})

        assert_status_code(response, 400)
        assert "/approve" in response.json()["detail"]
        after = await client.get(f"/api/cards/{card_id}")
        assert after.json()["status"] == "in_review"

    async def test_patch_cannot_start_a_card(self, client, repo):
        create = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Dragged to in progress"),
        )
        card_id = create.json()["id"]

        response = await client.patch(
            f"/api/cards/{card_id}", json={"status": "in_progress"}
        )

        assert_status_code(response, 400)
        assert "/start" in response.json()["detail"]

    async def test_patch_cannot_move_a_running_card(
        self, client, ingested_repo, db_session, parked_executor
    ):
        """Dragging a running card anywhere strands its run and its job."""
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)

        for target in ("done", "in_review", "todo", "failed"):
            response = await client.patch(
                f"/api/cards/{card['id']}", json={"status": target}
            )
            assert response.status_code == 400, (
                f"PATCH moved a RUNNING card to {target!r}: {response.text}"
            )

        job = await read_job(client, db_session, card["job_id"])
        assert job["status"] in ("queued", "running")

    async def test_patch_cannot_reopen_a_done_card(self, client, repo, db_session):
        create = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Merged already"),
        )
        card_id = create.json()["id"]
        await stage_card(db_session, card_id, status="done")

        response = await client.patch(f"/api/cards/{card_id}", json={"status": "todo"})

        assert_status_code(response, 400)
        assert "terminal" in response.json()["detail"]

    @pytest.mark.parametrize(
        "moves",
        [
            ("in_review", "todo"),
            ("failed", "todo"),
            ("todo", "failed"),
        ],
    )
    async def test_patch_still_allows_the_board_its_own_moves(
        self, client, repo, moves
    ):
        """Statuses that carry no side effect stay draggable."""
        create = await client.post(
            f"/api/repos/{repo['id']}/cards",
            json=card_create_payload(title="Board move"),
        )
        card_id = create.json()["id"]

        for target in moves:
            response = await client.patch(
                f"/api/cards/{card_id}", json={"status": target}
            )
            assert_status_code(response, 200)
            assert response.json()["status"] == target

    async def test_patching_a_card_to_its_own_status_is_not_a_transition(
        self, client, ingested_repo, parked_executor
    ):
        """An idempotent PATCH must not be refused as an illegal move."""
        card = await start_agent_card(client, ingested_repo["id"])
        await await_dispatch(parked_executor)

        response = await client.patch(
            f"/api/cards/{card['id']}",
            json={"status": "in_progress", "title": "Renamed mid-run"},
        )

        assert_status_code(response, 200)
        assert response.json()["title"] == "Renamed mid-run"


class TestResolveConflictsIsGuardedToo:
    """resolve-conflicts MERGES, so it needs the same gate approve has.

    It used to force-merge caller-supplied file contents from any status,
    including a card that was already `done`.

    Note: this covers the STATE half only. That the endpoint also merges when
    no conflict exists at all is a separate finding (QA T15) against the same
    handler.
    """

    @pytest.mark.parametrize("status", ["todo", "in_progress", "done", "failed"])
    async def test_resolve_conflicts_refuses_a_card_that_is_not_in_review(
        self, client, ingested_repo, db_session, status
    ):
        create = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title=f"Not in review ({status})"),
        )
        card_id = create.json()["id"]
        await stage_card(
            db_session, card_id, status=status, branch_name="lazyaf/abc12345"
        )

        response = await client.post(
            f"/api/cards/{card_id}/resolve-conflicts",
            json={"resolutions": [{"path": "invented.txt", "content": "by the caller"}]},
        )

        assert_status_code(response, 400)
        assert status in response.json()["detail"]

    async def test_resolve_conflicts_refuses_a_card_with_no_branch(
        self, client, ingested_repo, db_session
    ):
        create = await client.post(
            f"/api/repos/{ingested_repo['id']}/cards",
            json=card_create_payload(title="No branch"),
        )
        card_id = create.json()["id"]
        await stage_card(db_session, card_id, status="in_review")

        response = await client.post(
            f"/api/cards/{card_id}/resolve-conflicts",
            json={"resolutions": [{"path": "x.txt", "content": "y"}]},
        )

        assert_status_code(response, 400)
        assert "no branch" in response.json()["detail"]


class TestStartingACardIsAtomic:
    """The status check and the status write are ONE statement now.

    They used to be six awaits apart with no row lock, so five
    barrier-released starts produced five Jobs, five ad-hoc runs and five
    `lazyaf/*` branches, and the card kept only the last job_id. A
    double-click at a demo is the likeliest gesture there is.

    These tests use `concurrent_client`, which gives every request its own
    session: on the shared session of the ordinary `client` fixture there is
    no race to lose.
    """

    async def _card(self, client, repo_id, title="Concurrent"):
        response = await client.post(
            f"/api/repos/{repo_id}/cards",
            json={
                "title": title,
                "description": "raced",
                "runner_type": "mock",
                "step_type": "agent",
            },
        )
        assert response.status_code == 201, response.text
        return response.json()["id"]

    async def _ingested_repo(self, client):
        response = await client.post(
            "/api/repos/ingest", json=repo_ingest_payload(name="RaceRepo")
        )
        assert response.status_code in (200, 201), response.text
        body = response.json()
        # Seed the default branch: /start refuses a repo that has no branch to
        # branch FROM, so without this all five racers get a 400 and the test
        # measures the guard instead of the claim.
        default_branch = (await client.get(f"/api/repos/{body['id']}")).json()[
            "default_branch"
        ]
        seed_branch(body["id"], default_branch, path="README.md", content=b"seed\n")
        return body

    async def _counts(self, async_engine, card_id):
        factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            jobs = (
                await session.execute(select(Job).where(Job.card_id == card_id))
            ).scalars().all()
            runs = (
                await session.execute(
                    select(PipelineRun).where(PipelineRun.trigger_ref == card_id)
                )
            ).scalars().all()
        return len(jobs), len(runs)

    async def test_five_simultaneous_starts_produce_one_job_and_one_run(
        self, concurrent_client, clean_git_repos, clean_runner_registry, async_engine
    ):
        repo = await self._ingested_repo(concurrent_client)
        card_id = await self._card(concurrent_client, repo["id"])

        responses = await asyncio.gather(
            *(
                concurrent_client.post(f"/api/cards/{card_id}/start")
                for _ in range(5)
            )
        )
        codes = sorted(r.status_code for r in responses)
        await settle()

        jobs, runs = await self._counts(async_engine, card_id)
        assert codes == [200, 400, 400, 400, 400], (
            f"{codes.count(200)} of 5 simultaneous starts were accepted"
        )
        assert (jobs, runs) == (1, 1), (
            f"one card, {jobs} job(s) and {runs} run(s): each orphan is a "
            "container, a branch and a bill nothing points at"
        )

    async def test_five_simultaneous_retries_produce_one_new_run(
        self, concurrent_client, clean_git_repos, clean_runner_registry, async_engine
    ):
        repo = await self._ingested_repo(concurrent_client)
        card_id = await self._card(concurrent_client, repo["id"], title="Retry race")

        factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            await stage_card(session, card_id, status="failed")

        responses = await asyncio.gather(
            *(
                concurrent_client.post(f"/api/cards/{card_id}/retry")
                for _ in range(5)
            )
        )
        codes = sorted(r.status_code for r in responses)
        await settle()

        jobs, runs = await self._counts(async_engine, card_id)
        assert codes == [200, 400, 400, 400, 400], (
            f"{codes.count(200)} of 5 simultaneous retries were accepted"
        )
        assert (jobs, runs) == (1, 1)

    async def test_approve_and_reject_cannot_both_win(
        self, concurrent_client, clean_git_repos, async_engine
    ):
        """Whoever loses is refused, instead of the card being decided by
        whichever request happened to commit last."""
        repo = await self._ingested_repo(concurrent_client)
        card_id = await self._card(concurrent_client, repo["id"], title="Approve race")

        factory = async_sessionmaker(
            async_engine, class_=AsyncSession, expire_on_commit=False
        )
        async with factory() as session:
            repo_row = await session.get(Repo, repo["id"])
            base = seed_branch(repo["id"], repo_row.default_branch, path="README.md")
            branch = f"lazyaf/{card_id[:8]}"
            seed_branch(repo["id"], branch, parent=base)
            await stage_card(
                session, card_id, status="in_review", branch_name=branch
            )

        approve, reject = await asyncio.gather(
            concurrent_client.post(f"/api/cards/{card_id}/approve", json={}),
            concurrent_client.post(f"/api/cards/{card_id}/reject"),
        )

        accepted = [r for r in (approve, reject) if r.status_code < 400]
        assert len(accepted) == 1, (
            f"approve={approve.status_code} reject={reject.status_code}: both "
            "were accepted on one card"
        )

        final = await concurrent_client.get(f"/api/cards/{card_id}")
        body = final.json()
        if approve.status_code == 200:
            assert body["status"] == "done" and body["branch_name"] == branch
        else:
            assert body["status"] == "todo" and body["branch_name"] is None
